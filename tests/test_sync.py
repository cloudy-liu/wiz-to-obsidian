from __future__ import annotations

import importlib
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
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
    def test_plan_incremental_sync_uses_state_and_repair_flags(self) -> None:
        models = import_or_fail(self, "wiz_to_obsidian.models")
        sync = import_or_fail(self, "wiz_to_obsidian.sync")

        note = models.WizNote(
            kb_name="KB",
            kb_guid="kb-1",
            doc_guid="doc-repair",
            title="Repair",
            folder_parts=("Inbox",),
            updated_at=datetime(2026, 4, 4, 10, 0, tzinfo=timezone.utc),
            body=models.NoteBody(markdown="# Repair"),
        )
        inventory = models.Inventory(notes=(note,))
        state = models.SyncState(
            notes_by_doc_guid={
                "doc-repair": models.SyncStateNote(
                    doc_guid="doc-repair",
                    relative_path=Path("Inbox") / "Repair.md",
                    updated="2026-04-04T10:00:00+00:00",
                    needs_repair=True,
                )
            }
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(sync, "load_or_rebuild_sync_state", side_effect=AssertionError("state reload not expected")):
                plan = sync.plan_incremental_sync(inventory, Path(temp_dir), sync_state=state)

        self.assertEqual(("doc-repair",), tuple(note.doc_guid for note in plan.notes_to_export))
        self.assertEqual("repair", plan.reasons_by_doc_guid["doc-repair"])
        self.assertEqual((), plan.skipped_doc_guids)

    def test_load_or_rebuild_sync_state_rebuilds_from_existing_export(self) -> None:
        sync = import_or_fail(self, "wiz_to_obsidian.sync")

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "Inbox").mkdir()
            (output_dir / "Inbox" / "Roadmap.md").write_text(
                "\n".join(
                    [
                        "---",
                        "wiz_doc_guid: doc-1",
                        "updated: 2026-04-04T10:00:00+00:00",
                        "---",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            load_result = sync.load_or_rebuild_sync_state(output_dir)

        self.assertEqual("rebuild", load_result.source)
        self.assertEqual(
            Path("Inbox") / "Roadmap.md",
            load_result.state.notes_by_doc_guid["doc-1"].relative_path,
        )
        self.assertEqual("2026-04-04T10:00:00+00:00", load_result.state.notes_by_doc_guid["doc-1"].updated)

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
                        "# Same",
                        "",
                        "Existing content.",
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

    def test_incremental_sync_inventory_preserves_existing_body_when_moved_note_body_missing(self) -> None:
        models = import_or_fail(self, "wiz_to_obsidian.models")
        sync = import_or_fail(self, "wiz_to_obsidian.sync")

        note = models.WizNote(
            kb_name="KB",
            kb_guid="kb-1",
            doc_guid="doc-move",
            title="Moved New",
            folder_parts=("Inbox",),
            updated_at=datetime(2026, 4, 4, 11, 0, tzinfo=timezone.utc),
            body=models.NoteBody(),
        )
        inventory = models.Inventory(notes=(note,))
        state = models.SyncState(
            notes_by_doc_guid={
                "doc-move": models.SyncStateNote(
                    doc_guid="doc-move",
                    relative_path=Path("Inbox") / "Moved Old.md",
                    updated="2026-04-04T10:00:00+00:00",
                    needs_repair=False,
                )
            },
            doc_version=123,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            old_path = output_dir / "Inbox" / "Moved Old.md"
            old_path.parent.mkdir(parents=True, exist_ok=True)
            old_path.write_text(
                "\n".join(
                    [
                        "---",
                        "wiz_doc_guid: doc-move",
                        "updated: 2026-04-04T10:00:00+00:00",
                        "---",
                        "",
                        "# Existing Body",
                        "",
                        "Do not lose this body.",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            result = sync.incremental_sync_inventory(
                inventory=inventory,
                output_dir=output_dir,
                sync_state=state,
            )

            new_path = output_dir / "Inbox" / "Moved New.md"
            self.assertFalse(old_path.exists())
            self.assertTrue(new_path.exists())
            new_text = new_path.read_text(encoding="utf-8")
            self.assertIn("# Existing Body", new_text)
            self.assertIn("Do not lose this body.", new_text)
            self.assertIn("updated: 2026-04-04T11:00:00+00:00", new_text)
            self.assertTrue(result.sync_state.notes_by_doc_guid["doc-move"].needs_repair)
            self.assertEqual(123, result.sync_state.doc_version)

    def test_incremental_sync_inventory_rebases_preserved_asset_links_when_note_moves_folders(self) -> None:
        models = import_or_fail(self, "wiz_to_obsidian.models")
        sync = import_or_fail(self, "wiz_to_obsidian.sync")

        note = models.WizNote(
            kb_name="KB",
            kb_guid="kb-1",
            doc_guid="doc-asset",
            title="Moved Across Folders",
            folder_parts=("Archive", "2026"),
            updated_at=datetime(2026, 4, 4, 11, 0, tzinfo=timezone.utc),
            body=models.NoteBody(),
        )
        inventory = models.Inventory(notes=(note,))
        state = models.SyncState(
            notes_by_doc_guid={
                "doc-asset": models.SyncStateNote(
                    doc_guid="doc-asset",
                    relative_path=Path("Inbox") / "Moved Across Folders.md",
                    updated="2026-04-04T10:00:00+00:00",
                    needs_repair=False,
                )
            }
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            old_path = output_dir / "Inbox" / "Moved Across Folders.md"
            old_path.parent.mkdir(parents=True, exist_ok=True)
            old_path.write_text(
                "\n".join(
                    [
                        "---",
                        "wiz_doc_guid: doc-asset",
                        "updated: 2026-04-04T10:00:00+00:00",
                        "---",
                        "",
                        "# Existing Body",
                        "",
                        "![](../_wiz/resources/doc-asset/cover.png)",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            sync.incremental_sync_inventory(
                inventory=inventory,
                output_dir=output_dir,
                sync_state=state,
            )

            new_path = output_dir / "Archive" / "2026" / "Moved Across Folders.md"
            new_text = new_path.read_text(encoding="utf-8")
            self.assertIn("![](../../_wiz/resources/doc-asset/cover.png)", new_text)


    def test_plan_incremental_sync_detects_remote_updated_notes(self) -> None:
        models = import_or_fail(self, "wiz_to_obsidian.models")
        sync = import_or_fail(self, "wiz_to_obsidian.sync")

        note = models.WizNote(
            kb_name="KB",
            kb_guid="kb-1",
            doc_guid="doc-remote",
            title="Remote Updated",
            folder_parts=("Inbox",),
            updated_at=datetime(2026, 4, 4, 10, 0, tzinfo=timezone.utc),
            body=models.NoteBody(markdown="# Remote Updated"),
        )
        inventory = models.Inventory(notes=(note,))
        state = models.SyncState(
            notes_by_doc_guid={
                "doc-remote": models.SyncStateNote(
                    doc_guid="doc-remote",
                    relative_path=Path("Inbox") / "Remote Updated.md",
                    updated="2026-04-04T10:00:00+00:00",
                    needs_repair=False,
                )
            }
        )

        # remote dataModified is 2026-04-05T08:00:00Z = 1775376000000ms
        remote_versions = {
            "doc-remote": {
                "dataModified": 1775376000000,
                "version": 42,
                "title": "Remote Updated",
                "type": "collaboration",
            }
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(sync, "load_or_rebuild_sync_state", side_effect=AssertionError("not expected")):
                plan = sync.plan_incremental_sync(
                    inventory, Path(temp_dir), sync_state=state, remote_versions=remote_versions
                )

        self.assertEqual(("doc-remote",), tuple(note.doc_guid for note in plan.notes_to_export))
        self.assertEqual("remote_updated", plan.reasons_by_doc_guid["doc-remote"])
        self.assertEqual((), plan.skipped_doc_guids)

    def test_plan_incremental_sync_skips_when_remote_not_newer(self) -> None:
        models = import_or_fail(self, "wiz_to_obsidian.models")
        sync = import_or_fail(self, "wiz_to_obsidian.sync")

        note = models.WizNote(
            kb_name="KB",
            kb_guid="kb-1",
            doc_guid="doc-remote",
            title="Remote Same",
            folder_parts=("Inbox",),
            updated_at=datetime(2026, 4, 4, 10, 0, tzinfo=timezone.utc),
            body=models.NoteBody(markdown="# Remote Same"),
        )
        inventory = models.Inventory(notes=(note,))
        state = models.SyncState(
            notes_by_doc_guid={
                "doc-remote": models.SyncStateNote(
                    doc_guid="doc-remote",
                    relative_path=Path("Inbox") / "Remote Same.md",
                    updated="2026-04-04T10:00:00+00:00",
                    needs_repair=False,
                )
            }
        )

        # remote dataModified = 2026-04-04T08:00:00Z = older than state
        remote_versions = {
            "doc-remote": {
                "dataModified": 1775289600000,
                "version": 10,
                "title": "Remote Same",
                "type": "collaboration",
            }
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(sync, "load_or_rebuild_sync_state", side_effect=AssertionError("not expected")):
                plan = sync.plan_incremental_sync(
                    inventory, Path(temp_dir), sync_state=state, remote_versions=remote_versions
                )

        self.assertEqual((), plan.notes_to_export)
        self.assertEqual(("doc-remote",), plan.skipped_doc_guids)

    def test_plan_incremental_sync_ignores_remote_when_not_provided(self) -> None:
        models = import_or_fail(self, "wiz_to_obsidian.models")
        sync = import_or_fail(self, "wiz_to_obsidian.sync")

        note = models.WizNote(
            kb_name="KB",
            kb_guid="kb-1",
            doc_guid="doc-skip",
            title="Skip",
            folder_parts=("Inbox",),
            updated_at=datetime(2026, 4, 4, 10, 0, tzinfo=timezone.utc),
            body=models.NoteBody(markdown="# Skip"),
        )
        inventory = models.Inventory(notes=(note,))
        state = models.SyncState(
            notes_by_doc_guid={
                "doc-skip": models.SyncStateNote(
                    doc_guid="doc-skip",
                    relative_path=Path("Inbox") / "Skip.md",
                    updated="2026-04-04T10:00:00+00:00",
                    needs_repair=False,
                )
            }
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(sync, "load_or_rebuild_sync_state", side_effect=AssertionError("not expected")):
                plan = sync.plan_incremental_sync(inventory, Path(temp_dir), sync_state=state)

        self.assertEqual((), plan.notes_to_export)
        self.assertEqual(("doc-skip",), plan.skipped_doc_guids)

    def test_incremental_sync_inventory_preserves_existing_doc_version_when_remote_version_not_updated(self) -> None:
        models = import_or_fail(self, "wiz_to_obsidian.models")
        sync = import_or_fail(self, "wiz_to_obsidian.sync")

        note = models.WizNote(
            kb_name="KB",
            kb_guid="kb-1",
            doc_guid="doc-1",
            title="Keep Version",
            folder_parts=("Inbox",),
            updated_at=datetime(2026, 4, 4, 10, 0, tzinfo=timezone.utc),
            body=models.NoteBody(markdown="# Body"),
        )
        inventory = models.Inventory(notes=(note,))
        state = models.SyncState(
            notes_by_doc_guid={
                "doc-1": models.SyncStateNote(
                    doc_guid="doc-1",
                    relative_path=Path("Inbox") / "Keep Version.md",
                    updated="2026-04-04T10:00:00+00:00",
                    needs_repair=False,
                )
            },
            doc_version=123,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            note_path = output_dir / "Inbox" / "Keep Version.md"
            note_path.parent.mkdir(parents=True, exist_ok=True)
            note_path.write_text(
                "\n".join(
                    [
                        "---",
                        "wiz_doc_guid: doc-1",
                        "updated: 2026-04-04T10:00:00+00:00",
                        "---",
                        "",
                        "# Body",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            result = sync.incremental_sync_inventory(
                inventory=inventory,
                output_dir=output_dir,
                sync_state=state,
                doc_version=0,
            )

            written_state = sync.load_sync_state(output_dir)
            self.assertIsNotNone(written_state)
            self.assertEqual(123, result.sync_state.doc_version)
            self.assertEqual(123, written_state.doc_version)

    def test_incremental_sync_inventory_respects_limit_with_prebuilt_plan(self) -> None:
        models = import_or_fail(self, "wiz_to_obsidian.models")
        sync = import_or_fail(self, "wiz_to_obsidian.sync")

        note1 = models.WizNote(
            kb_name="KB", kb_guid="kb-1", doc_guid="doc-a",
            title="First", folder_parts=("Inbox",),
            updated_at=datetime(2026, 4, 4, 10, 0, tzinfo=timezone.utc),
            body=models.NoteBody(markdown="# A"),
        )
        note2 = models.WizNote(
            kb_name="KB", kb_guid="kb-1", doc_guid="doc-b",
            title="Second", folder_parts=("Inbox",),
            updated_at=datetime(2026, 4, 4, 10, 0, tzinfo=timezone.utc),
            body=models.NoteBody(markdown="# B"),
        )
        inventory = models.Inventory(notes=(note1, note2))
        plan = sync.IncrementalSyncPlan(
            notes_to_export=(note1, note2),
            note_relative_paths_by_doc_guid={
                "doc-a": Path("Inbox") / "First.md",
                "doc-b": Path("Inbox") / "Second.md",
            },
            skipped_doc_guids=(),
            stale_paths_to_remove=(),
            reasons_by_doc_guid={"doc-a": "new", "doc-b": "new"},
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            result = sync.incremental_sync_inventory(
                inventory=inventory,
                output_dir=output_dir,
                limit=1,
                plan=plan,
                sync_state=models.SyncState(notes_by_doc_guid={}),
            )

            # Only 1 note should be exported
            self.assertEqual(1, result.report["summary"]["exported_notes"])
            # The other should be in skipped
            self.assertIn("doc-a", {n.doc_guid for n in result.sync_state.notes_by_doc_guid.values()} if hasattr(result.sync_state.notes_by_doc_guid, 'values') else [])


if __name__ == "__main__":
    unittest.main()
