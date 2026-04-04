from __future__ import annotations

import importlib
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def import_or_fail(testcase: unittest.TestCase, module_name: str):
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        testcase.fail(f"expected module {module_name!r} to exist: {exc}")


class WizLocalTests(unittest.TestCase):
    def test_build_inventory_joins_docs_folders_tags_and_attachments(self) -> None:
        module = import_or_fail(self, "wiz_to_obsidian.wiz_local")

        kb = {"kbGuid": "kb-1", "name": "Main KB"}
        folders = [
            {
                "location": "/Projects/",
                "name": "Projects",
                "children": [
                    {
                        "location": "/Projects/2026/",
                        "name": "2026",
                        "children": [],
                    }
                ],
            }
        ]
        docs = [
            {
                "docGuid": "doc-1",
                "title": "Quarterly Plan",
                "category": "/Projects/2026/",
                "type": "lite/markdown",
                "tags": ["planning", "work"],
                "dateCreated": "2026-03-01T09:00:00Z",
                "dateModified": "2026-03-02T10:00:00Z",
                "attachmentCount": 1,
            }
        ]
        attachments = [
            {
                "docGuid": "doc-1",
                "attGuid": "att-1",
                "name": "spec.pdf",
                "dataSize": 1024,
            }
        ]

        inventory = module.build_inventory_from_records(
            kb=kb,
            folders=folders,
            docs=docs,
            attachments=attachments,
            body_by_doc={"doc-1": module.NoteBody(markdown="# Plan")},
            resource_bytes_by_key={"doc-1/cover.png": b"binary"},
        )

        self.assertEqual(1, len(inventory.notes))
        note = inventory.notes[0]
        self.assertEqual("Main KB", note.kb_name)
        self.assertEqual(("Projects", "2026"), note.folder_parts)
        self.assertEqual(("planning", "work"), note.tags)
        self.assertEqual("# Plan", note.body.markdown)
        self.assertEqual(1, len(note.attachments))
        self.assertEqual(1, inventory.resource_count)
        self.assertEqual(datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc), note.created_at)

    def test_build_inventory_deduplicates_docs_by_doc_guid(self) -> None:
        module = import_or_fail(self, "wiz_to_obsidian.wiz_local")

        inventory = module.build_inventory_from_records(
            kb={"kbGuid": "kb-1", "name": "Main KB"},
            folders=[],
            docs=[
                {
                    "docGuid": "doc-1",
                    "title": "Original Title",
                    "category": "/",
                    "type": "collaboration",
                    "abstractText": "older",
                },
                {
                    "docGuid": "doc-1",
                    "title": "Original Title",
                    "category": "/",
                    "type": "collaboration",
                    "abstractText": "newer",
                },
            ],
            attachments=[],
            body_by_doc={"doc-1": module.NoteBody(markdown="# Note")},
            resource_bytes_by_key={},
        )

        self.assertEqual(1, len(inventory.notes))
        self.assertEqual("doc-1", inventory.notes[0].doc_guid)
        self.assertEqual("# Note", inventory.notes[0].body.markdown)

    def test_scan_local_wiz_reads_account_docs_editor_data_and_resources(self) -> None:
        module = import_or_fail(self, "wiz_to_obsidian.wiz_local")

        class FakeSource:
            def __init__(self, stores):
                self._stores = stores

            def iter_store_values(self, db_name: str, store_name: str, *, skip_bad: bool = False):
                yield from self._stores.get((db_name, store_name), [])

        stores = {
            ("wiz-account", "accounts"): [
                {
                    "userGuid": "user-1",
                    "kbGuid": "kb-1",
                    "displayName": "Cloudy",
                    "userId": "user@example.com",
                }
            ],
            ("wiz-user-1", "kbs"): [{"kbGuid": "kb-1", "name": "Main KB", "type": "person"}],
            ("wiz-user-1", "folders"): [
                {"location": "/Projects/", "name": "Projects", "children": []}
            ],
            ("wiz-user-1", "docs"): [
                {
                    "docGuid": "doc-1",
                    "title": "Roadmap",
                    "category": "/Projects/",
                    "type": "collaboration",
                    "tags": ["planning"],
                    "created": 1711795200000,
                    "dataModified": 1711881600000,
                },
                {
                    "docGuid": "doc-2",
                    "title": "Imported Page",
                    "category": "/Projects/",
                    "type": "document",
                    "tags": [],
                    "created": 1711795200000,
                    "dataModified": 1711881600000,
                },
            ],
            ("wiz-user-1", "attachments"): [
                {
                    "docGuid": "doc-1",
                    "attGuid": "att-1",
                    "name": "spec.pdf",
                    "dataSize": 2048,
                }
            ],
            ("wiz-user-1", "data"): [
                {
                    "kbGuid": "kb-1",
                    "docGuid": "doc-1",
                    "dataId": "cover.png",
                    "dataType": "resource",
                    "data": b"img",
                },
                {
                    "kbGuid": "kb-1",
                    "docGuid": "doc-1",
                    "dataId": "spec.pdf",
                    "dataType": "attachment",
                    "data": b"pdf!",
                },
                {
                    "kbGuid": "kb-1",
                    "docGuid": "doc-2",
                    "dataId": "index.html",
                    "dataType": "html",
                    "data": b"<p>Hello</p>",
                },
            ],
            ("wiz-editor-ot", "docs"): [
                {
                    "id": "kb-1:doc-1",
                    "data": json.dumps(
                        {
                            "blocks": [
                                {"id": "h1", "type": "text", "heading": 1, "text": [{"insert": "Roadmap"}]},
                                {
                                    "id": "img1",
                                    "type": "embed",
                                    "embedType": "image",
                                    "embedData": {"src": "cover.png"},
                                },
                            ]
                        }
                    ).encode("utf-8"),
                }
            ],
        }

        inventory = module.scan_local_wiz(source=FakeSource(stores))

        self.assertEqual(2, len(inventory.notes))
        self.assertEqual(1, inventory.resource_count)
        self.assertEqual(1, inventory.attachment_count)

        note_by_guid = {note.doc_guid: note for note in inventory.notes}
        self.assertIn("![](wiz-resource://doc-1/cover.png)", note_by_guid["doc-1"].body.markdown)
        self.assertEqual("<p>Hello</p>", note_by_guid["doc-2"].body.html)
        self.assertEqual(
            datetime(2024, 3, 30, 10, 40, tzinfo=timezone.utc),
            note_by_guid["doc-1"].created_at,
        )

    def test_scan_local_wiz_recovers_declared_html_charset(self) -> None:
        module = import_or_fail(self, "wiz_to_obsidian.wiz_local")

        class FakeSource:
            def __init__(self, stores):
                self._stores = stores

            def iter_store_values(self, db_name: str, store_name: str, *, skip_bad: bool = False):
                yield from self._stores.get((db_name, store_name), [])

        html_bytes = (
            '<html><head><meta charset="gbk"></head><body><p>中文内容</p></body></html>'.encode("gb18030")
        )
        stores = {
            ("wiz-account", "accounts"): [
                {
                    "userGuid": "user-1",
                    "kbGuid": "kb-1",
                    "displayName": "Cloudy",
                    "userId": "user@example.com",
                }
            ],
            ("wiz-user-1", "kbs"): [{"kbGuid": "kb-1", "name": "Main KB", "type": "person"}],
            ("wiz-user-1", "folders"): [],
            ("wiz-user-1", "docs"): [
                {
                    "docGuid": "doc-charset",
                    "title": "Imported Page",
                    "category": "/",
                    "type": "document",
                }
            ],
            ("wiz-user-1", "attachments"): [],
            ("wiz-user-1", "data"): [
                {
                    "kbGuid": "kb-1",
                    "docGuid": "doc-charset",
                    "dataId": "index.html",
                    "dataType": "html",
                    "data": html_bytes,
                }
            ],
            ("wiz-editor-ot", "docs"): [],
        }

        inventory = module.scan_local_wiz(source=FakeSource(stores))

        self.assertEqual(1, len(inventory.notes))
        self.assertIn("中文内容", inventory.notes[0].body.html)


if __name__ == "__main__":
    unittest.main()
