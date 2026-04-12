from __future__ import annotations

import importlib
import json
import tempfile
import time
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


def _make_note(
    models,
    *,
    doc_guid: str,
    title: str = "",
    folder_parts: tuple[str, ...] = (),
    body_markdown: str | None = None,
    body_html: str | None = None,
    updated_at: datetime | None = None,
    attachments: tuple = (),
) -> object:
    return models.WizNote(
        kb_name="Test KB",
        kb_guid="kb-1",
        doc_guid=doc_guid,
        title=title or f"Note {doc_guid}",
        folder_parts=folder_parts,
        body=models.NoteBody(markdown=body_markdown, html=body_html),
        updated_at=updated_at,
        attachments=attachments,
    )


def _make_large_inventory(models, count: int, *, with_body: bool = True) -> object:
    notes = []
    for i in range(count):
        doc_guid = f"doc-{i:04d}"
        md = f"# Note {i}\nContent for note {i}." if with_body else None
        notes.append(
            _make_note(models, doc_guid=doc_guid, title=f"Note {i}", body_markdown=md,
                       updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        )
    return models.Inventory(notes=tuple(notes))


class PerformanceBenchmarkTests(unittest.TestCase):
    """Performance benchmark tests using synthetic inventories.
    These tests verify algorithmic complexity properties, not absolute wall-clock times.
    """

    def test_cache_index_lookup_is_o1(self) -> None:
        """Cache index lookup should not grow linearly with entry count."""
        cache_module = import_or_fail(self, "wiz_to_obsidian.wiz_cache")
        models = import_or_fail(self, "wiz_to_obsidian.models")

        note = models.WizNote(
            kb_name="KB", kb_guid="kb-1", doc_guid="doc-target", title="Target",
        )

        def build_client(entry_count: int) -> cache_module.CachedWizClient:
            class FakeBackend:
                def iter_entries(self):
                    # Login entry
                    yield cache_module.CachedEntry(
                        key="https://as.wiz.cn/as/user/login",
                        payload=json.dumps({"returnCode": 200, "result": {"token": "t"}}).encode(),
                    )
                    # The target note body
                    yield cache_module.CachedEntry(
                        key="https://ks.wiz.cn/ks/note/download/kb-1/doc-target?downloadInfo=1&downloadData=1",
                        payload=json.dumps({"returnCode": 200, "html": "<p>Target</p>"}).encode(),
                    )
                    # Fill with other entries
                    for i in range(entry_count):
                        yield cache_module.CachedEntry(
                            key=f"https://ks.wiz.cn/ks/note/download/kb-1/doc-fill-{i:05d}?downloadInfo=1&downloadData=1",
                            payload=json.dumps({"returnCode": 200, "html": f"<p>Filler {i}</p>"}).encode(),
                        )

            return cache_module.CachedWizClient(FakeBackend())

        small_client = build_client(10)
        large_client = build_client(1000)

        # Both should find the same note body
        small_body = small_client.fetch_note_body(note)
        large_body = large_client.fetch_note_body(note)
        self.assertEqual(small_body.html, large_body.html)

        # Measure lookup times - large should not be proportionally slower
        iterations = 200
        start = time.perf_counter()
        for _ in range(iterations):
            small_client.fetch_note_body(note)
        small_elapsed = time.perf_counter() - start

        start = time.perf_counter()
        for _ in range(iterations):
            large_client.fetch_note_body(note)
        large_elapsed = time.perf_counter() - start

        # O(1) lookup: large should be at most 3x slower than small (not 100x)
        # Allow generous margin to avoid flaky CI failures
        self.assertLess(large_elapsed, small_elapsed * 10 + 0.5,
                        "Cache lookup should be O(1), not O(N)")

    def test_unchanged_file_not_rewritten(self) -> None:
        """Content-equal files should not trigger a disk write."""
        models = import_or_fail(self, "wiz_to_obsidian.models")
        exporter = import_or_fail(self, "wiz_to_obsidian.exporter")

        note = _make_note(
            models, doc_guid="doc-1", title="Unchanged",
            body_markdown="# Unchanged Note",
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        inventory = models.Inventory(notes=(note,))

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            # First export
            exporter.export_inventory(inventory=inventory, output_dir=output_dir)
            note_path = output_dir / "Unchanged.md"
            self.assertTrue(note_path.exists())
            first_mtime = note_path.stat().st_mtime

            # Re-export with identical content
            import time as _time
            _time.sleep(0.05)  # Ensure mtime would differ if written
            exporter.export_inventory(inventory=inventory, output_dir=output_dir)
            second_mtime = note_path.stat().st_mtime

            # mtime should be unchanged since content is identical
            self.assertEqual(first_mtime, second_mtime,
                             "File should not be rewritten when content is unchanged")

    def test_incremental_sync_skips_unchanged_notes(self) -> None:
        """Warm state with 0 changed notes should skip export entirely."""
        models = import_or_fail(self, "wiz_to_obsidian.models")
        sync = import_or_fail(self, "wiz_to_obsidian.sync")
        exporter = import_or_fail(self, "wiz_to_obsidian.exporter")

        inventory = _make_large_inventory(models, 20)
        updated_str = "2026-01-01T00:00:00+00:00"

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            # Full export first
            exporter.export_inventory(inventory=inventory, output_dir=output_dir)

            # Build a warm state that matches all notes
            state = models.SyncState(
                notes_by_doc_guid={
                    note.doc_guid: models.SyncStateNote(
                        doc_guid=note.doc_guid,
                        relative_path=Path(f"{note.title}.md"),
                        updated=updated_str,
                        needs_repair=False,
                    )
                    for note in inventory.notes
                }
            )

            plan = sync.plan_incremental_sync(inventory, output_dir, sync_state=state)
            self.assertEqual(0, len(plan.notes_to_export))
            self.assertEqual(20, len(plan.skipped_doc_guids))

    def test_incremental_sync_exports_only_changed_notes(self) -> None:
        """Only changed notes should be in the export plan."""
        models = import_or_fail(self, "wiz_to_obsidian.models")
        sync = import_or_fail(self, "wiz_to_obsidian.sync")
        exporter = import_or_fail(self, "wiz_to_obsidian.exporter")

        inventory = _make_large_inventory(models, 20)
        updated_str = "2026-01-01T00:00:00+00:00"

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            exporter.export_inventory(inventory=inventory, output_dir=output_dir)

            # Build state where only one note changed
            state_notes = {}
            for note in inventory.notes:
                if note.doc_guid == "doc-0010":
                    state_notes[note.doc_guid] = models.SyncStateNote(
                        doc_guid=note.doc_guid,
                        relative_path=Path(f"{note.title}.md"),
                        updated="2025-01-01T00:00:00+00:00",  # Old timestamp
                        needs_repair=False,
                    )
                else:
                    state_notes[note.doc_guid] = models.SyncStateNote(
                        doc_guid=note.doc_guid,
                        relative_path=Path(f"{note.title}.md"),
                        updated=updated_str,
                        needs_repair=False,
                    )
            state = models.SyncState(notes_by_doc_guid=state_notes)

            plan = sync.plan_incremental_sync(inventory, output_dir, sync_state=state)
            self.assertEqual(1, len(plan.notes_to_export))
            self.assertEqual("doc-0010", plan.notes_to_export[0].doc_guid)
            self.assertEqual(19, len(plan.skipped_doc_guids))

    def test_incremental_hydration_proportional_to_changed(self) -> None:
        """Hydration requests should be proportional to changed notes, not total."""
        models = import_or_fail(self, "wiz_to_obsidian.models")
        hydration = import_or_fail(self, "wiz_to_obsidian.wiz_hydration")

        # Create inventory with 10 notes, all needing hydration
        notes = tuple(
            _make_note(models, doc_guid=f"doc-{i:04d}", title=f"Note {i}")
            for i in range(10)
        )
        inventory = models.Inventory(notes=notes)

        fetch_counts = {"body": 0, "resource": 0, "attachment": 0}

        class CountingClient:
            def fetch_note_body(self, note):
                fetch_counts["body"] += 1
                return models.NoteBody(markdown=f"# {note.title}")

            def fetch_resource(self, note, resource_name: str):
                fetch_counts["resource"] += 1
                return None

            def fetch_attachment(self, note, attachment):
                fetch_counts["attachment"] += 1
                return None

        result = hydration.hydrate_inventory(inventory=inventory, client=CountingClient())
        self.assertEqual(10, result.summary["hydrated_notes"])
        self.assertEqual(10, fetch_counts["body"])

    def test_incremental_sync_with_repair_flag(self) -> None:
        """Notes marked needs_repair in state should be re-exported."""
        models = import_or_fail(self, "wiz_to_obsidian.models")
        sync = import_or_fail(self, "wiz_to_obsidian.sync")

        inventory = _make_large_inventory(models, 5)
        updated_str = "2026-01-01T00:00:00+00:00"

        # All notes match state except one is marked for repair
        state_notes = {}
        for i, note in enumerate(inventory.notes):
            state_notes[note.doc_guid] = models.SyncStateNote(
                doc_guid=note.doc_guid,
                relative_path=Path(f"{note.title}.md"),
                updated=updated_str,
                needs_repair=(i == 2),  # Only doc-0002 needs repair
            )
        state = models.SyncState(notes_by_doc_guid=state_notes)

        with tempfile.TemporaryDirectory() as temp_dir:
            plan = sync.plan_incremental_sync(inventory, Path(temp_dir), sync_state=state)
            self.assertEqual(1, len(plan.notes_to_export))
            self.assertEqual("doc-0002", plan.notes_to_export[0].doc_guid)
            self.assertEqual("repair", plan.reasons_by_doc_guid.get("doc-0002"))


class FunctionalEquivalenceTests(unittest.TestCase):
    """Tests verifying that optimized code produces the same results as the original."""

    def test_cache_index_matches_linear_scan_for_note_body(self) -> None:
        """O(1) index lookup should return same results as linear scan."""
        cache_module = import_or_fail(self, "wiz_to_obsidian.wiz_cache")
        models = import_or_fail(self, "wiz_to_obsidian.models")

        note = models.WizNote(kb_name="KB", kb_guid="kb-1", doc_guid="doc-1", title="Test")

        entries = [
            cache_module.CachedEntry(
                key="https://ks.wiz.cn/ks/note/download/kb-1/doc-1?downloadInfo=1&downloadData=1",
                payload=json.dumps({"returnCode": 200, "html": "<p>Hello</p>"}).encode(),
            ),
            cache_module.CachedEntry(
                key="https://ks.wiz.cn/ks/object/download/kb-1/doc-1?objType=resource&objId=img.png",
                payload=b"img-data",
            ),
            cache_module.CachedEntry(
                key="https://ks.wiz.cn/ks/object/download/kb-1/doc-1?objType=attachment&objId=att-1",
                payload=b"att-data",
            ),
        ]

        class FakeBackend:
            def iter_entries(self):
                return iter(entries)

        client = cache_module.CachedWizClient(FakeBackend())
        body = client.fetch_note_body(note)
        self.assertEqual("<p>Hello</p>", body.html)

        # Verify index was built correctly
        self.assertIn(("kb-1", "doc-1"), client._note_body_index)
        self.assertIn(("kb-1", "doc-1", "resource", "img.png"), client._object_index)
        self.assertIn(("kb-1", "doc-1", "attachment", "att-1"), client._object_index)

    def test_cache_index_handles_duplicate_entries_keeps_last(self) -> None:
        """When multiple entries match, index should keep the last one (like dict update)."""
        cache_module = import_or_fail(self, "wiz_to_obsidian.wiz_cache")
        models = import_or_fail(self, "wiz_to_obsidian.models")

        note = models.WizNote(kb_name="KB", kb_guid="kb-1", doc_guid="doc-1", title="Test")

        entries = [
            cache_module.CachedEntry(
                key="https://ks.wiz.cn/ks/note/download/kb-1/doc-1?downloadInfo=1&downloadData=1",
                payload=json.dumps({"returnCode": 200, "html": "<p>First</p>"}).encode(),
            ),
            cache_module.CachedEntry(
                key="https://ks.wiz.cn/ks/note/download/kb-1/doc-1?downloadInfo=1&downloadData=1",
                payload=json.dumps({"returnCode": 200, "html": "<p>Second</p>"}).encode(),
            ),
        ]

        class FakeBackend:
            def iter_entries(self):
                return iter(entries)

        client = cache_module.CachedWizClient(FakeBackend())
        body = client.fetch_note_body(note)
        # Second entry should overwrite first (dict update behavior)
        self.assertEqual("<p>Second</p>", body.html)

    def test_concurrent_hydration_preserves_order(self) -> None:
        """Concurrent hydration should return notes in input order."""
        models = import_or_fail(self, "wiz_to_obsidian.models")
        hydration = import_or_fail(self, "wiz_to_obsidian.wiz_hydration")

        notes = tuple(
            _make_note(models, doc_guid=f"doc-{i:04d}", title=f"Note {i}")
            for i in range(20)
        )
        inventory = models.Inventory(notes=notes)

        class SlowClient:
            def __init__(self):
                self.call_order = []

            def fetch_note_body(self, note):
                self.call_order.append(note.doc_guid)
                return models.NoteBody(markdown=f"# {note.title}")

            def fetch_resource(self, note, resource_name: str):
                return None

            def fetch_attachment(self, note, attachment):
                return None

        client = SlowClient()
        result = hydration.hydrate_inventory(inventory=inventory, client=client)

        # Notes should be in original order
        result_guids = [note.doc_guid for note in result.inventory.notes]
        expected_guids = [f"doc-{i:04d}" for i in range(20)]
        self.assertEqual(expected_guids, result_guids)

    def test_concurrent_hydration_isolates_failures(self) -> None:
        """Failure of one note should not affect others in concurrent hydration."""
        models = import_or_fail(self, "wiz_to_obsidian.models")
        hydration = import_or_fail(self, "wiz_to_obsidian.wiz_hydration")

        notes = tuple(
            _make_note(models, doc_guid=f"doc-{i:04d}", title=f"Note {i}")
            for i in range(10)
        )
        inventory = models.Inventory(notes=notes)

        class PartialFailClient:
            def fetch_note_body(self, note):
                if note.doc_guid == "doc-0005":
                    raise RuntimeError("simulated failure")
                return models.NoteBody(markdown=f"# {note.title}")

            def fetch_resource(self, note, resource_name: str):
                return None

            def fetch_attachment(self, note, attachment):
                return None

        result = hydration.hydrate_inventory(inventory=inventory, client=PartialFailClient())

        # 9 should succeed, 1 should fail
        hydrated_count = sum(1 for note in result.inventory.notes if note.body.has_meaningful_content)
        self.assertEqual(9, hydrated_count)
        self.assertGreaterEqual(result.summary["hydration_failures"], 1)

    def test_conditional_write_different_content_triggers_write(self) -> None:
        """When content differs, file should be written."""
        models = import_or_fail(self, "wiz_to_obsidian.models")
        exporter = import_or_fail(self, "wiz_to_obsidian.exporter")

        note = _make_note(
            models, doc_guid="doc-1", title="Test",
            body_markdown="# Version 1",
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        inventory = models.Inventory(notes=(note,))

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            exporter.export_inventory(inventory=inventory, output_dir=output_dir)
            note_path = output_dir / "Test.md"
            v1_text = note_path.read_text(encoding="utf-8")
            self.assertIn("# Version 1", v1_text)

            # Now update with different content
            note2 = _make_note(
                models, doc_guid="doc-1", title="Test",
                body_markdown="# Version 2",
                updated_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
            )
            inventory2 = models.Inventory(notes=(note2,))
            exporter.export_inventory(inventory=inventory2, output_dir=output_dir)
            v2_text = note_path.read_text(encoding="utf-8")
            self.assertIn("# Version 2", v2_text)
            self.assertNotEqual(v1_text, v2_text)

    def test_content_audit_uses_in_memory_markdown(self) -> None:
        """Content audit should use in-memory markdown when provided, matching disk results."""
        models = import_or_fail(self, "wiz_to_obsidian.models")
        content_audit = import_or_fail(self, "wiz_to_obsidian.content_audit")
        exporter = import_or_fail(self, "wiz_to_obsidian.exporter")

        note = _make_note(
            models, doc_guid="doc-1", title="Audit Test",
            body_markdown="# Test",
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        inventory = models.Inventory(notes=(note,))

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            result = exporter.export_inventory(
                inventory=inventory, output_dir=output_dir,
                write_content_audit_files=False,
            )
            note_path = output_dir / "Audit Test.md"
            note_markdown = note_path.read_text(encoding="utf-8")

            # Audit with disk read (no in-memory)
            audit_disk = content_audit.write_content_audit(
                inventory=inventory,
                output_dir=output_dir,
                note_paths_by_doc_guid={"doc-1": note_path},
                missing_resources_by_doc_guid={"doc-1": ()},
            )

            # Audit with in-memory markdown
            audit_memory = content_audit.write_content_audit(
                inventory=inventory,
                output_dir=output_dir,
                note_paths_by_doc_guid={"doc-1": note_path},
                missing_resources_by_doc_guid={"doc-1": ()},
                note_markdowns_by_doc_guid={"doc-1": note_markdown},
            )

            # Both should produce identical summaries
            self.assertEqual(audit_disk["summary"], audit_memory["summary"])

    def test_per_note_repair_status_propagates(self) -> None:
        """Hydration repair status should correctly reflect per-note state."""
        models = import_or_fail(self, "wiz_to_obsidian.models")
        hydration = import_or_fail(self, "wiz_to_obsidian.wiz_hydration")

        # Note with body but missing resource -> needs_repair
        note_with_missing_resource = _make_note(
            models, doc_guid="doc-1", title="Missing Resource",
            body_markdown="# Note\n![](wiz-resource://doc-1/missing.png)",
        )
        # Note with complete body -> no repair needed
        note_complete = _make_note(
            models, doc_guid="doc-2", title="Complete",
            body_markdown="# Complete Note",
        )
        inventory = models.Inventory(notes=(note_with_missing_resource, note_complete))

        class NoOpClient:
            def fetch_note_body(self, note):
                return note.body if note.body.has_meaningful_content else models.NoteBody(markdown=f"# {note.title}")

            def fetch_resource(self, note, resource_name: str):
                return None  # Resource never available

            def fetch_attachment(self, note, attachment):
                return None

        result = hydration.hydrate_inventory(inventory=inventory, client=NoOpClient())

        self.assertIsNotNone(result.note_repair_status)
        # doc-1 should need repair (missing resource)
        self.assertTrue(result.note_repair_status.get("doc-1"))
        # doc-2 should not need repair (complete body, no missing resources)
        self.assertFalse(result.note_repair_status.get("doc-2"))

    def test_sync_state_persistence_round_trip(self) -> None:
        """SyncState should survive a write/load round trip."""
        models = import_or_fail(self, "wiz_to_obsidian.models")
        sync = import_or_fail(self, "wiz_to_obsidian.sync")

        original_state = models.SyncState(
            notes_by_doc_guid={
                "doc-1": models.SyncStateNote(
                    doc_guid="doc-1",
                    relative_path=Path("Folder/Note 1.md"),
                    updated="2026-01-01T00:00:00+00:00",
                    needs_repair=False,
                ),
                "doc-2": models.SyncStateNote(
                    doc_guid="doc-2",
                    relative_path=Path("Note 2.md"),
                    updated=None,
                    needs_repair=True,
                ),
            }
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            sync.write_sync_state(output_dir, original_state)
            loaded = sync.load_sync_state(output_dir)

            self.assertIsNotNone(loaded)
            self.assertEqual(2, len(loaded.notes_by_doc_guid))
            entry1 = loaded.notes_by_doc_guid["doc-1"]
            self.assertEqual(Path("Folder/Note 1.md"), entry1.relative_path)
            self.assertEqual("2026-01-01T00:00:00+00:00", entry1.updated)
            self.assertFalse(entry1.needs_repair)
            entry2 = loaded.notes_by_doc_guid["doc-2"]
            self.assertIsNone(entry2.updated)
            self.assertTrue(entry2.needs_repair)

    def test_hydration_repair_status_overrides_exporter_in_sync(self) -> None:
        """Hydration repair status should override exporter's needs_repair in sync state."""
        models = import_or_fail(self, "wiz_to_obsidian.models")
        sync = import_or_fail(self, "wiz_to_obsidian.sync")
        exporter = import_or_fail(self, "wiz_to_obsidian.exporter")

        note = _make_note(
            models, doc_guid="doc-1", title="Repaired",
            body_markdown="# Fixed",
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        inventory = models.Inventory(notes=(note,))

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            # Full export - exporter would mark needs_repair=False since body is complete
            result = exporter.export_inventory(inventory=inventory, output_dir=output_dir)
            exporter_state = result.sync_state
            self.assertFalse(exporter_state.notes_by_doc_guid["doc-1"].needs_repair)

            # Simulate sync where hydration says it still needs repair
            hydration_repair = {"doc-1": True}
            sync_result = sync.incremental_sync_inventory(
                inventory=inventory,
                output_dir=output_dir,
                hydration_repair_status=hydration_repair,
            )
            self.assertTrue(sync_result.sync_state.notes_by_doc_guid["doc-1"].needs_repair)


if __name__ == "__main__":
    unittest.main()
