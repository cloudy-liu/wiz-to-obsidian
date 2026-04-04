from __future__ import annotations

import importlib
import json
import tempfile
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


class ContentAuditTests(unittest.TestCase):
    def test_export_inventory_writes_content_audit_files_and_tracks_unresolved_content(self) -> None:
        models = import_or_fail(self, "wiz_to_obsidian.models")
        exporter = import_or_fail(self, "wiz_to_obsidian.exporter")

        note = models.WizNote(
            kb_name="Main KB",
            kb_guid="kb-1",
            doc_guid="doc-audit-1",
            title="Audit Sample",
            note_type="lite/markdown",
            body=models.NoteBody(
                markdown="\n".join(
                    [
                        "# Imported",
                        "![diagram](index_files/cover.png)",
                        "_Table omitted (2 x 2)_",
                    ]
                )
            ),
        )
        inventory = models.Inventory(notes=(note,))

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            result = exporter.export_inventory(inventory=inventory, output_dir=output_dir)
            audit_json_path = output_dir / "_wiz" / "content_audit.json"
            audit_md_path = output_dir / "_wiz" / "content_audit.md"

            audit = json.loads(audit_json_path.read_text(encoding="utf-8"))
            unresolved = {entry["doc_guid"]: entry for entry in audit["unresolved"]}
            issue_codes = {issue["code"] for issue in unresolved["doc-audit-1"]["issues"]}

            self.assertTrue(audit_json_path.exists())
            self.assertTrue(audit_md_path.exists())
            self.assertIn("doc-audit-1", unresolved)
            self.assertIn("missing_assets", issue_codes)
            self.assertIn("omitted_tables", issue_codes)
            self.assertIn("content_integrity", result.report)
            self.assertEqual(
                audit["summary"]["unresolved_count"],
                result.report["content_integrity"]["unresolved_count"],
            )

    def test_export_inventory_marks_manual_review_for_length_gap_and_unsupported_blocks(self) -> None:
        models = import_or_fail(self, "wiz_to_obsidian.models")
        exporter = import_or_fail(self, "wiz_to_obsidian.exporter")

        note = models.WizNote(
            kb_name="Main KB",
            kb_guid="kb-1",
            doc_guid="doc-audit-2",
            title="Needs Review",
            note_type="collaboration",
            body=models.NoteBody(
                markdown="# Short",
                metadata=models.BodyMetadata(
                    source_text_length=1200,
                    unsupported_block_types=("diagram-board",),
                ),
            ),
        )
        inventory = models.Inventory(notes=(note,))

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            exporter.export_inventory(inventory=inventory, output_dir=output_dir)
            audit = json.loads((output_dir / "_wiz" / "content_audit.json").read_text(encoding="utf-8"))
            manual_review = {entry["doc_guid"]: entry for entry in audit["manual_review_candidates"]}
            issue_codes = {issue["code"] for issue in manual_review["doc-audit-2"]["issues"]}

            self.assertIn("doc-audit-2", manual_review)
            self.assertIn("length_gap", issue_codes)
            self.assertIn("unsupported_blocks", issue_codes)

    def test_export_inventory_uses_visible_html_text_for_length_gap_detection(self) -> None:
        models = import_or_fail(self, "wiz_to_obsidian.models")
        exporter = import_or_fail(self, "wiz_to_obsidian.exporter")

        note = models.WizNote(
            kb_name="Main KB",
            kb_guid="kb-1",
            doc_guid="doc-audit-3",
            title="HTML Visible Text",
            note_type="document",
            body=models.NoteBody(
                html="<html><body>"
                + ("<div><span>" * 80)
                + "Hello World Again"
                + ("</span></div>" * 80)
                + "</body></html>",
            ),
        )
        inventory = models.Inventory(notes=(note,))

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            exporter.export_inventory(inventory=inventory, output_dir=output_dir)
            audit = json.loads((output_dir / "_wiz" / "content_audit.json").read_text(encoding="utf-8"))
            manual_review = {entry["doc_guid"]: entry for entry in audit["manual_review_candidates"]}

            self.assertNotIn("doc-audit-3", manual_review)


if __name__ == "__main__":
    unittest.main()
