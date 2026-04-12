from __future__ import annotations

import importlib
import json
import tempfile
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


class ExporterTests(unittest.TestCase):
    @staticmethod
    def _placeholder_note_html(doc_guid: str) -> str:
        return (
            "<!DOCTYPE html><html><body>"
            "当前客户端版本较低，无法编辑协作笔记<br>"
            "The current client version is too low to edit collaborative notes<br>"
            f'<a href="https://as.wiz.cn/note-plus/note/kb-1/{doc_guid}">查看笔记</a>'
            "</body></html>"
        )

    def test_export_inventory_writes_notes_assets_and_report(self) -> None:
        models = import_or_fail(self, "wiz_to_obsidian.models")
        exporter = import_or_fail(self, "wiz_to_obsidian.exporter")

        note = models.WizNote(
            kb_name="Main KB",
            kb_guid="kb-1",
            doc_guid="doc-1",
            title="Quarterly Plan",
            folder_parts=("Projects",),
            tags=("planning",),
            note_type="collaboration",
            created_at=datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc),
            updated_at=datetime(2026, 3, 2, 10, 0, tzinfo=timezone.utc),
            body=models.NoteBody(
                markdown="\n".join(
                    [
                        "# Plan",
                        "![](wiz-resource://doc-1/cover.png)",
                    ]
                )
            ),
            attachments=(
                models.AttachmentRecord(
                    att_guid="att-1",
                    doc_guid="doc-1",
                    name="spec.pdf",
                    size=4,
                ),
            ),
        )
        inventory = models.Inventory(
            notes=(note,),
            resource_bytes_by_key={"wiz-resource://doc-1/cover.png": b"img"},
            attachment_bytes_by_key={"wiz-attachment://doc-1/spec.pdf": b"pdf!"},
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            result = exporter.export_inventory(inventory=inventory, output_dir=output_dir)

            note_path = output_dir / "Projects" / "Quarterly Plan.md"
            resource_path = output_dir / "_wiz" / "resources" / "doc-1" / "cover.png"
            attachment_path = output_dir / "_wiz" / "attachments" / "doc-1" / "spec.pdf"
            report_path = output_dir / "_wiz" / "report.json"

            self.assertTrue(note_path.exists())
            self.assertEqual(b"img", resource_path.read_bytes())
            self.assertEqual(b"pdf!", attachment_path.read_bytes())

            note_text = note_path.read_text(encoding="utf-8")
            self.assertIn("![](../_wiz/resources/doc-1/cover.png)", note_text)
            self.assertIn("[spec.pdf](../_wiz/attachments/doc-1/spec.pdf)", note_text)

            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(1, report["summary"]["exported_notes"])
            self.assertEqual(1, report["summary"]["exported_resources"])
            self.assertEqual(1, report["summary"]["exported_attachments"])
            self.assertEqual(report_path, result.report_path)

    def test_export_inventory_rewrites_plain_html_resource_names_for_cached_files(self) -> None:
        models = import_or_fail(self, "wiz_to_obsidian.models")
        exporter = import_or_fail(self, "wiz_to_obsidian.exporter")

        note = models.WizNote(
            kb_name="Main KB",
            kb_guid="kb-1",
            doc_guid="doc-html",
            title="Imported Page",
            folder_parts=("Inbox",),
            note_type="document",
            body=models.NoteBody(html='<p>Hello</p><img src="cover.png">'),
        )
        inventory = models.Inventory(
            notes=(note,),
            resource_bytes_by_key={"wiz-resource://doc-html/cover.png": b"img"},
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            result = exporter.export_inventory(inventory=inventory, output_dir=Path(temp_dir))
            note_text = (Path(temp_dir) / "Inbox" / "Imported Page.md").read_text(encoding="utf-8")

            self.assertIn("![](../_wiz/resources/doc-html/cover.png)", note_text)
            self.assertEqual(1, result.report["summary"]["exported_resources"])

    def test_export_inventory_reports_progress_for_each_note(self) -> None:
        models = import_or_fail(self, "wiz_to_obsidian.models")
        exporter = import_or_fail(self, "wiz_to_obsidian.exporter")

        inventory = models.Inventory(
            notes=(
                models.WizNote(
                    kb_name="Main KB",
                    kb_guid="kb-1",
                    doc_guid="doc-1",
                    title="Alpha",
                    folder_parts=("Inbox",),
                    body=models.NoteBody(markdown="# A"),
                ),
                models.WizNote(
                    kb_name="Main KB",
                    kb_guid="kb-1",
                    doc_guid="doc-2",
                    title="Beta",
                    folder_parts=("Inbox",),
                    body=models.NoteBody(markdown="# B"),
                ),
            )
        )

        progress_messages: list[str] = []
        with tempfile.TemporaryDirectory() as temp_dir:
            exporter.export_inventory(
                inventory=inventory,
                output_dir=Path(temp_dir),
                progress=progress_messages.append,
            )

        self.assertEqual(
            [
                "1/2 Inbox/Alpha.md",
                "2/2 Inbox/Beta.md",
            ],
            progress_messages,
        )

    def test_export_inventory_rewrites_index_files_resource_paths_in_html(self) -> None:
        models = import_or_fail(self, "wiz_to_obsidian.models")
        exporter = import_or_fail(self, "wiz_to_obsidian.exporter")

        note = models.WizNote(
            kb_name="Main KB",
            kb_guid="kb-1",
            doc_guid="doc-wechat",
            title="Wechat Clip",
            folder_parts=("Inbox",),
            note_type="document",
            body=models.NoteBody(html='<p><img src="index_files/cover.png"></p>'),
        )
        inventory = models.Inventory(
            notes=(note,),
            resource_bytes_by_key={"wiz-resource://doc-wechat/cover.png": b"img"},
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            exporter.export_inventory(inventory=inventory, output_dir=Path(temp_dir))
            note_text = (Path(temp_dir) / "Inbox" / "Wechat Clip.md").read_text(encoding="utf-8")

            self.assertIn("![](../_wiz/resources/doc-wechat/cover.png)", note_text)
            self.assertNotIn("index_files/", note_text)

    def test_export_inventory_rewrites_legacy_markdown_asset_paths(self) -> None:
        models = import_or_fail(self, "wiz_to_obsidian.models")
        exporter = import_or_fail(self, "wiz_to_obsidian.exporter")

        note = models.WizNote(
            kb_name="Main KB",
            kb_guid="kb-1",
            doc_guid="doc-legacy",
            title="Legacy Markdown",
            folder_parts=("Inbox",),
            note_type="lite/markdown",
            body=models.NoteBody(
                markdown="\n".join(
                    [
                        "# Imported",
                        "![diagram](index_files/cover.png)",
                        "[spec](doc-legacy_files/spec.pdf)",
                    ]
                )
            ),
            attachments=(
                models.AttachmentRecord(
                    att_guid="att-1",
                    doc_guid="doc-legacy",
                    name="spec.pdf",
                    size=4,
                ),
            ),
        )
        inventory = models.Inventory(
            notes=(note,),
            resource_bytes_by_key={"wiz-resource://doc-legacy/cover.png": b"img"},
            attachment_bytes_by_key={"wiz-attachment://doc-legacy/spec.pdf": b"pdf!"},
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            exporter.export_inventory(inventory=inventory, output_dir=Path(temp_dir))
            note_text = (Path(temp_dir) / "Inbox" / "Legacy Markdown.md").read_text(encoding="utf-8")

            self.assertIn("![diagram](../_wiz/resources/doc-legacy/cover.png)", note_text)
            self.assertIn("[spec](../_wiz/attachments/doc-legacy/spec.pdf)", note_text)
            self.assertNotIn("index_files/", note_text)
            self.assertNotIn("doc-legacy_files/", note_text)

    def test_export_inventory_reports_missing_legacy_markdown_asset_paths(self) -> None:
        models = import_or_fail(self, "wiz_to_obsidian.models")
        exporter = import_or_fail(self, "wiz_to_obsidian.exporter")

        note = models.WizNote(
            kb_name="Main KB",
            kb_guid="kb-1",
            doc_guid="doc-legacy",
            title="Legacy Markdown",
            note_type="lite/markdown",
            body=models.NoteBody(markdown="![diagram](index_files/cover.png)"),
        )
        inventory = models.Inventory(notes=(note,))

        with tempfile.TemporaryDirectory() as temp_dir:
            result = exporter.export_inventory(inventory=inventory, output_dir=Path(temp_dir))

            self.assertEqual(
                ["index_files/cover.png"],
                result.report["missing_resources"],
            )

    def test_export_inventory_replaces_missing_legacy_asset_links_with_warning_callout(self) -> None:
        models = import_or_fail(self, "wiz_to_obsidian.models")
        exporter = import_or_fail(self, "wiz_to_obsidian.exporter")

        note = models.WizNote(
            kb_name="Main KB",
            kb_guid="kb-1",
            doc_guid="doc-missing",
            title="Broken Asset",
            note_type="lite/markdown",
            body=models.NoteBody(
                markdown="\n".join(
                    [
                        "# Imported",
                        "![diagram](index_files/cover.png)",
                        "[spec](doc-missing_files/spec.pdf)",
                    ]
                )
            ),
        )
        inventory = models.Inventory(notes=(note,))

        with tempfile.TemporaryDirectory() as temp_dir:
            exporter.export_inventory(inventory=inventory, output_dir=Path(temp_dir))
            note_text = (Path(temp_dir) / "Broken Asset.md").read_text(encoding="utf-8")

            self.assertIn("[!warning] Missing migrated asset", note_text)
            self.assertIn("original: index_files/cover.png", note_text)
            self.assertIn("original: doc-missing_files/spec.pdf", note_text)
            self.assertNotIn("![diagram](index_files/cover.png)", note_text)
            self.assertNotIn("[spec](doc-missing_files/spec.pdf)", note_text)

    def test_export_inventory_replaces_missing_escaped_legacy_asset_links_with_warning_callout(self) -> None:
        models = import_or_fail(self, "wiz_to_obsidian.models")
        exporter = import_or_fail(self, "wiz_to_obsidian.exporter")

        note = models.WizNote(
            kb_name="Main KB",
            kb_guid="kb-1",
            doc_guid="doc-missing",
            title="Broken Escaped Asset",
            note_type="lite/markdown",
            body=models.NoteBody(markdown="![](doc-missing\\_files/cover.png)"),
        )
        inventory = models.Inventory(notes=(note,))

        with tempfile.TemporaryDirectory() as temp_dir:
            exporter.export_inventory(inventory=inventory, output_dir=Path(temp_dir))
            note_text = (Path(temp_dir) / "Broken Escaped Asset.md").read_text(encoding="utf-8")

            self.assertIn("[!warning] Missing migrated asset", note_text)
            self.assertIn("original: doc-missing_files/cover.png", note_text)
            self.assertNotIn("doc-missing\\_files/cover.png", note_text)

    def test_export_inventory_separates_adjacent_missing_asset_warnings(self) -> None:
        models = import_or_fail(self, "wiz_to_obsidian.models")
        exporter = import_or_fail(self, "wiz_to_obsidian.exporter")

        note = models.WizNote(
            kb_name="Main KB",
            kb_guid="kb-1",
            doc_guid="doc-missing",
            title="Broken Adjacent Assets",
            note_type="lite/markdown",
            body=models.NoteBody(
                markdown="![](doc-missing_files/one.png)![](doc-missing_files/two.png)"
            ),
        )
        inventory = models.Inventory(notes=(note,))

        with tempfile.TemporaryDirectory() as temp_dir:
            exporter.export_inventory(inventory=inventory, output_dir=Path(temp_dir))
            note_text = (Path(temp_dir) / "Broken Adjacent Assets.md").read_text(encoding="utf-8")

            self.assertIn("original: doc-missing_files/one.png\n\n> [!warning] Missing migrated asset", note_text)

    def test_export_inventory_ignores_assets_only_referenced_in_html_head(self) -> None:
        models = import_or_fail(self, "wiz_to_obsidian.models")
        exporter = import_or_fail(self, "wiz_to_obsidian.exporter")

        note = models.WizNote(
            kb_name="Main KB",
            kb_guid="kb-1",
            doc_guid="doc-head",
            title="Wechat Clip",
            note_type="document",
            body=models.NoteBody(
                html=(
                    "<html><head><link rel=\"stylesheet\" href=\"index_files/see_more.css\"></head>"
                    "<body><p>Hello</p></body></html>"
                )
            ),
        )
        inventory = models.Inventory(notes=(note,))

        with tempfile.TemporaryDirectory() as temp_dir:
            result = exporter.export_inventory(inventory=inventory, output_dir=Path(temp_dir))

            self.assertEqual([], result.report["missing_resources"])

    def test_export_inventory_uses_first_available_pipe_separated_resource_candidate(self) -> None:
        models = import_or_fail(self, "wiz_to_obsidian.models")
        exporter = import_or_fail(self, "wiz_to_obsidian.exporter")

        note = models.WizNote(
            kb_name="Main KB",
            kb_guid="kb-1",
            doc_guid="doc-collab",
            title="Diagram",
            note_type="collaboration",
            body=models.NoteBody(markdown="![](wiz-resource://doc-collab/one.png|two.png)"),
        )
        inventory = models.Inventory(
            notes=(note,),
            resource_bytes_by_key={"wiz-resource://doc-collab/two.png": b"img"},
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            result = exporter.export_inventory(inventory=inventory, output_dir=Path(temp_dir))
            note_text = (Path(temp_dir) / "Diagram.md").read_text(encoding="utf-8")

            self.assertIn("![](_wiz/resources/doc-collab/two.png)", note_text)
            self.assertEqual(1, result.report["summary"]["exported_resources"])
            self.assertEqual([], result.report["missing_resources"])

    def test_export_inventory_writes_generated_assets_referenced_by_note_body(self) -> None:
        models = import_or_fail(self, "wiz_to_obsidian.models")
        exporter = import_or_fail(self, "wiz_to_obsidian.exporter")

        note = models.WizNote(
            kb_name="Main KB",
            kb_guid="kb-1",
            doc_guid="doc-generated",
            title="Generated Asset",
            note_type="collaboration",
            body=models.NoteBody(
                markdown="[Drawio source](wiz-resource://doc-generated/diagram.drawio)",
                generated_assets=(
                    models.GeneratedAsset(
                        key="wiz-resource://doc-generated/diagram.drawio",
                        payload=b"<mxfile><diagram/></mxfile>",
                    ),
                ),
            ),
        )
        inventory = models.Inventory(notes=(note,))

        with tempfile.TemporaryDirectory() as temp_dir:
            result = exporter.export_inventory(inventory=inventory, output_dir=Path(temp_dir))
            note_text = (Path(temp_dir) / "Generated Asset.md").read_text(encoding="utf-8")
            asset_path = Path(temp_dir) / "_wiz" / "resources" / "doc-generated" / "diagram.drawio"

            self.assertIn("[Drawio source](_wiz/resources/doc-generated/diagram.drawio)", note_text)
            self.assertEqual(b"<mxfile><diagram/></mxfile>", asset_path.read_bytes())
            self.assertEqual([], result.report["missing_resources"])
            self.assertEqual(1, result.report["summary"]["exported_resources"])

    def test_export_inventory_respects_explicit_note_paths_and_can_skip_report_write(self) -> None:
        models = import_or_fail(self, "wiz_to_obsidian.models")
        exporter = import_or_fail(self, "wiz_to_obsidian.exporter")

        note = models.WizNote(
            kb_name="Main KB",
            kb_guid="kb-1",
            doc_guid="doc-1",
            title="Quarterly Plan",
            body=models.NoteBody(markdown="# Ready"),
        )
        inventory = models.Inventory(notes=(note,))

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            result = exporter.export_inventory(
                inventory=inventory,
                output_dir=output_dir,
                note_relative_paths_by_doc_guid={"doc-1": Path("Custom") / "Pinned.md"},
                write_report=False,
                write_content_audit_files=False,
            )

            self.assertTrue((output_dir / "Custom" / "Pinned.md").exists())
            self.assertFalse((output_dir / "_wiz" / "report.json").exists())
            self.assertEqual(1, result.report["summary"]["exported_notes"])

    def test_export_inventory_writes_attachment_bytes_cached_by_att_guid(self) -> None:
        models = import_or_fail(self, "wiz_to_obsidian.models")
        exporter = import_or_fail(self, "wiz_to_obsidian.exporter")

        note = models.WizNote(
            kb_name="Main KB",
            kb_guid="kb-1",
            doc_guid="doc-1",
            title="Quarterly Plan",
            attachments=(
                models.AttachmentRecord(
                    att_guid="att-1",
                    doc_guid="doc-1",
                    name="spec.pdf",
                    size=4,
                ),
            ),
        )
        inventory = models.Inventory(
            notes=(note,),
            attachment_bytes_by_key={"wiz-attachment://doc-1/att-1": b"pdf!"},
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            exporter.export_inventory(inventory=inventory, output_dir=Path(temp_dir))
            attachment_path = Path(temp_dir) / "_wiz" / "attachments" / "doc-1" / "spec.pdf"

            self.assertEqual(b"pdf!", attachment_path.read_bytes())

    def test_export_inventory_treats_placeholder_note_body_as_missing(self) -> None:
        models = import_or_fail(self, "wiz_to_obsidian.models")
        exporter = import_or_fail(self, "wiz_to_obsidian.exporter")

        note = models.WizNote(
            kb_name="Main KB",
            kb_guid="kb-1",
            doc_guid="doc-1",
            title="Placeholder Note",
            note_type="collaboration",
            body=models.NoteBody(html=self._placeholder_note_html("doc-1")),
        )
        inventory = models.Inventory(notes=(note,))

        with tempfile.TemporaryDirectory() as temp_dir:
            result = exporter.export_inventory(inventory=inventory, output_dir=Path(temp_dir))
            note_text = (Path(temp_dir) / "Placeholder Note.md").read_text(encoding="utf-8")

            self.assertNotIn("当前客户端版本较低", note_text)
            self.assertEqual(0, result.report["summary"]["exported_notes"])
            self.assertEqual(1, result.report["summary"]["missing_body_count"])
            self.assertEqual(["doc-1"], result.report["missing_bodies"])

    def test_export_inventory_preserves_existing_markdown_when_note_body_missing(self) -> None:
        models = import_or_fail(self, "wiz_to_obsidian.models")
        exporter = import_or_fail(self, "wiz_to_obsidian.exporter")

        note = models.WizNote(
            kb_name="Main KB",
            kb_guid="kb-1",
            doc_guid="doc-1",
            title="Preserve Existing",
            folder_parts=("Inbox",),
            updated_at=datetime(2026, 4, 12, 8, 0, tzinfo=timezone.utc),
            body=models.NoteBody(),
        )
        inventory = models.Inventory(notes=(note,))

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            note_path = output_dir / "Inbox" / "Preserve Existing.md"
            note_path.parent.mkdir(parents=True, exist_ok=True)
            note_path.write_text(
                "\n".join(
                    [
                        "---",
                        'title: "Old Title"',
                        "wiz_doc_guid: doc-1",
                        "updated: 2026-04-01T00:00:00+00:00",
                        "---",
                        "",
                        "# Existing Body",
                        "",
                        "Keep this content.",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            result = exporter.export_inventory(
                inventory=inventory,
                output_dir=output_dir,
                write_content_audit_files=False,
            )

            note_text = note_path.read_text(encoding="utf-8")
            self.assertIn("title: Preserve Existing", note_text)
            self.assertIn("updated: 2026-04-12T08:00:00+00:00", note_text)
            self.assertIn("# Existing Body", note_text)
            self.assertIn("Keep this content.", note_text)
            self.assertTrue(result.sync_state.notes_by_doc_guid["doc-1"].needs_repair)


if __name__ == "__main__":
    unittest.main()
