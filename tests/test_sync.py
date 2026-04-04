from __future__ import annotations

import importlib
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


class SyncTests(unittest.TestCase):
    def test_plan_incremental_sync_detects_new_updated_and_moved_notes(self) -> None:
        models = import_or_fail(self, "wiz_to_obsidian.models")
        sync = import_or_fail(self, "wiz_to_obsidian.sync")

        note_same = models.WizNote(
            kb_name="KB",
            kb_guid="kb-1",
            doc_guid="doc-same",
            title="Same",
            folder_parts=("Inbox",),
            updated_at=datetime(2026, 4, 4, 10, 0, tzinfo=timezone.utc),
            body=models.NoteBody(markdown="# Same"),
        )
        note_updated = models.WizNote(
            kb_name="KB",
            kb_guid="kb-1",
            doc_guid="doc-updated",
            title="Updated",
            folder_parts=("Inbox",),
            updated_at=datetime(2026, 4, 4, 11, 0, tzinfo=timezone.utc),
            body=models.NoteBody(markdown="# Updated"),
        )
        note_moved = models.WizNote(
            kb_name="KB",
            kb_guid="kb-1",
            doc_guid="doc-moved",
            title="Moved New",
            folder_parts=("Inbox",),
            updated_at=datetime(2026, 4, 4, 12, 0, tzinfo=timezone.utc),
            body=models.NoteBody(markdown="# Moved"),
        )
        note_new = models.WizNote(
            kb_name="KB",
            kb_guid="kb-1",
            doc_guid="doc-new",
            title="Brand New",
            folder_parts=("Inbox",),
            updated_at=datetime(2026, 4, 4, 13, 0, tzinfo=timezone.utc),
            body=models.NoteBody(markdown="# New"),
        )
        inventory = models.Inventory(notes=(note_same, note_updated, note_moved, note_new))

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "Inbox").mkdir()
            (output_dir / "Inbox" / "Same.md").write_text(
                "\n".join(
                    [
                        "---",
                        "wiz_doc_guid: doc-same",
                        "updated: 2026-04-04T10:00:00+00:00",
                        "---",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            (output_dir / "Inbox" / "Updated.md").write_text(
                "\n".join(
                    [
                        "---",
                        "wiz_doc_guid: doc-updated",
                        "updated: 2026-04-04T09:00:00+00:00",
                        "---",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            (output_dir / "Inbox" / "Moved Old.md").write_text(
                "\n".join(
                    [
                        "---",
                        "wiz_doc_guid: doc-moved",
                        "updated: 2026-04-04T12:00:00+00:00",
                        "---",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            plan = sync.plan_incremental_sync(inventory, output_dir)

            self.assertEqual({"doc-updated", "doc-moved", "doc-new"}, {note.doc_guid for note in plan.notes_to_export})
            self.assertEqual(("doc-same",), plan.skipped_doc_guids)
            self.assertEqual("updated", plan.reasons_by_doc_guid["doc-updated"])
            self.assertEqual("moved", plan.reasons_by_doc_guid["doc-moved"])
            self.assertEqual("new", plan.reasons_by_doc_guid["doc-new"])
            self.assertIn(output_dir / "Inbox" / "Moved Old.md", plan.stale_paths_to_remove)

    def test_incremental_sync_inventory_removes_old_path_after_move(self) -> None:
        models = import_or_fail(self, "wiz_to_obsidian.models")
        sync = import_or_fail(self, "wiz_to_obsidian.sync")

        moved_note = models.WizNote(
            kb_name="KB",
            kb_guid="kb-1",
            doc_guid="doc-moved",
            title="Moved New",
            folder_parts=("Inbox",),
            updated_at=datetime(2026, 4, 4, 12, 0, tzinfo=timezone.utc),
            body=models.NoteBody(markdown="# Moved"),
        )
        inventory = models.Inventory(notes=(moved_note,))

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "Inbox").mkdir()
            old_path = output_dir / "Inbox" / "Moved Old.md"
            old_path.write_text(
                "\n".join(
                    [
                        "---",
                        "wiz_doc_guid: doc-moved",
                        "updated: 2026-04-04T12:00:00+00:00",
                        "---",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            result = sync.incremental_sync_inventory(inventory=inventory, output_dir=output_dir)

            self.assertFalse(old_path.exists())
            self.assertTrue((output_dir / "Inbox" / "Moved New.md").exists())
            self.assertEqual(1, result.report["summary"]["moved_notes"])
            self.assertEqual(1, result.report["summary"]["removed_old_paths"])


if __name__ == "__main__":
    unittest.main()
