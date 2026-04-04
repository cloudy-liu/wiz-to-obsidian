from __future__ import annotations

import importlib
import json
import unittest
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


class WizCacheTests(unittest.TestCase):
    def test_cached_wiz_client_reads_note_assets_and_login_payloads(self) -> None:
        models = import_or_fail(self, "wiz_to_obsidian.models")
        cache_module = import_or_fail(self, "wiz_to_obsidian.wiz_cache")

        note = models.WizNote(
            kb_name="Main KB",
            kb_guid="kb-1",
            doc_guid="doc-1",
            title="Roadmap",
            attachments=(
                models.AttachmentRecord(
                    att_guid="att-1",
                    doc_guid="doc-1",
                    name="spec.pdf",
                    size=4,
                ),
            ),
        )

        class FakeBackend:
            def iter_entries(self):
                yield cache_module.CachedEntry(
                    key="https://as.wiz.cn/as/user/login",
                    payload=json.dumps(
                        {
                            "returnCode": 200,
                            "result": {
                                "token": "cached-token",
                                "kbServer": "https://ks.wiz.cn",
                            },
                        }
                    ).encode("utf-8"),
                )
                yield cache_module.CachedEntry(
                    key="https://ks.wiz.cn/ks/note/download/kb-1/doc-1?downloadInfo=1&downloadData=1",
                    payload=json.dumps(
                        {
                            "returnCode": 200,
                            "html": "<p>Hello</p>",
                        }
                    ).encode("utf-8"),
                )
                yield cache_module.CachedEntry(
                    key="https://ks.wiz.cn/ks/object/download/kb-1/doc-1?objType=resource&objId=cover.png",
                    payload=b"img",
                )
                yield cache_module.CachedEntry(
                    key="https://ks.wiz.cn/ks/object/download/kb-1/doc-1?objType=attachment&objId=att-1",
                    payload=b"pdf!",
                )

        client = cache_module.CachedWizClient(FakeBackend())

        self.assertEqual("cached-token", client.cached_auth.token)
        self.assertEqual("https://ks.wiz.cn", client.cached_auth.ks_server_url)
        self.assertEqual("<p>Hello</p>", client.fetch_note_body(note).html)
        self.assertEqual(b"img", client.fetch_resource(note, "cover.png"))
        self.assertEqual(b"pdf!", client.fetch_attachment(note, note.attachments[0]))
