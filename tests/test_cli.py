from __future__ import annotations

import importlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock
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


class CliTests(unittest.TestCase):
    def test_scan_command_prints_inventory_summary_as_json(self) -> None:
        cli = import_or_fail(self, "wiz_to_obsidian.cli")
        models = import_or_fail(self, "wiz_to_obsidian.models")

        inventory = models.Inventory(
            notes=(
                models.WizNote(
                    kb_name="Main KB",
                    kb_guid="kb-1",
                    doc_guid="doc-1",
                    title="Roadmap",
                    body=models.NoteBody(markdown="# Ready"),
                ),
            )
        )

        stdout = io.StringIO()
        exit_code = cli.main(
            ["scan"],
            stdout=stdout,
            scan_inventory_fn=lambda leveldb_dir, blob_dir: inventory,
        )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(0, exit_code)
        self.assertEqual(1, payload["summary"]["total_notes"])
        self.assertEqual(1, payload["summary"]["notes_with_body"])

    def test_export_command_runs_scan_and_writes_files(self) -> None:
        cli = import_or_fail(self, "wiz_to_obsidian.cli")
        models = import_or_fail(self, "wiz_to_obsidian.models")
        exporter = import_or_fail(self, "wiz_to_obsidian.exporter")

        inventory = models.Inventory(
            notes=(
                models.WizNote(
                    kb_name="Main KB",
                    kb_guid="kb-1",
                    doc_guid="doc-1",
                    title="Roadmap",
                    body=models.NoteBody(markdown="# Ready"),
                ),
            )
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            stdout = io.StringIO()
            exit_code = cli.main(
                ["export", "--output", temp_dir],
                stdout=stdout,
                scan_inventory_fn=lambda leveldb_dir, blob_dir: inventory,
                export_inventory_fn=exporter.export_inventory,
            )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(0, exit_code)
            self.assertTrue((Path(temp_dir) / "Roadmap.md").exists())
            self.assertEqual(1, payload["summary"]["total_notes"])
            self.assertEqual(1, payload["summary"]["exported_notes"])

    def test_export_command_accepts_wiz_source_dir_overrides_after_subcommand(self) -> None:
        cli = import_or_fail(self, "wiz_to_obsidian.cli")
        models = import_or_fail(self, "wiz_to_obsidian.models")

        inventory = models.Inventory(notes=())
        captured = {}

        def fake_scan(leveldb_dir, blob_dir):
            captured["leveldb_dir"] = leveldb_dir
            captured["blob_dir"] = blob_dir
            return inventory

        with tempfile.TemporaryDirectory() as temp_dir:
            exit_code = cli.main(
                [
                    "export",
                    "--output",
                    temp_dir,
                    "--leveldb-dir",
                    "D:/wiz/leveldb",
                    "--blob-dir",
                    "D:/wiz/blob",
                ],
                stdout=io.StringIO(),
                scan_inventory_fn=fake_scan,
                export_inventory_fn=lambda inventory, output_dir, limit=None: type(
                    "Result",
                    (),
                    {
                        "output_dir": Path(output_dir),
                        "report_path": Path(output_dir) / "_wiz" / "report.json",
                        "report": {"summary": {"total_notes": 0, "exported_notes": 0}},
                    },
                )(),
            )

        self.assertEqual(0, exit_code)
        self.assertEqual(Path("D:/wiz/leveldb"), captured["leveldb_dir"])
        self.assertEqual(Path("D:/wiz/blob"), captured["blob_dir"])

    def test_export_command_hydrates_inventory_before_export_when_requested(self) -> None:
        cli = import_or_fail(self, "wiz_to_obsidian.cli")
        models = import_or_fail(self, "wiz_to_obsidian.models")
        exporter = import_or_fail(self, "wiz_to_obsidian.exporter")
        hydration = import_or_fail(self, "wiz_to_obsidian.wiz_hydration")

        inventory = models.Inventory(
            notes=(
                models.WizNote(
                    kb_name="Main KB",
                    kb_guid="kb-1",
                    doc_guid="doc-1",
                    title="Roadmap",
                    body=models.NoteBody(),
                ),
            )
        )
        hydrated_inventory = models.Inventory(
            notes=(
                models.WizNote(
                    kb_name="Main KB",
                    kb_guid="kb-1",
                    doc_guid="doc-1",
                    title="Roadmap",
                    body=models.NoteBody(markdown="# Ready"),
                ),
            )
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            stdout = io.StringIO()
            exit_code = cli.main(
                ["export", "--output", temp_dir, "--hydrate-missing"],
                stdout=stdout,
                scan_inventory_fn=lambda leveldb_dir, blob_dir: inventory,
                build_hydration_client_fn=lambda args: object(),
                hydrate_inventory_fn=lambda inventory, client: hydration.HydrationResult(
                    inventory=hydrated_inventory,
                    summary={"hydrated_notes": 1, "hydrated_resources": 0, "hydrated_attachments": 0},
                ),
                export_inventory_fn=exporter.export_inventory,
            )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(0, exit_code)
            self.assertEqual(1, payload["hydration"]["hydrated_notes"])
            self.assertTrue((Path(temp_dir) / "Roadmap.md").exists())

    def test_export_command_dispatches_incremental_sync_when_requested(self) -> None:
        cli = import_or_fail(self, "wiz_to_obsidian.cli")
        models = import_or_fail(self, "wiz_to_obsidian.models")

        inventory = models.Inventory(
            notes=(
                models.WizNote(
                    kb_name="Main KB",
                    kb_guid="kb-1",
                    doc_guid="doc-1",
                    title="Roadmap",
                    body=models.NoteBody(markdown="# Ready"),
                ),
            )
        )

        class FakeSyncResult:
            def __init__(self, output_dir: Path) -> None:
                self.output_dir = output_dir
                self.report_path = output_dir / "_wiz" / "report.json"
                self.report = {
                    "summary": {
                        "total_notes": 1,
                        "exported_notes": 1,
                        "skipped_notes": 0,
                        "new_notes": 1,
                        "updated_notes": 0,
                        "moved_notes": 0,
                        "removed_old_paths": 0,
                        "exported_resources": 0,
                        "exported_attachments": 0,
                    }
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            stdout = io.StringIO()
            exit_code = cli.main(
                ["export", "--output", temp_dir, "--incremental"],
                stdout=stdout,
                scan_inventory_fn=lambda leveldb_dir, blob_dir: inventory,
                incremental_sync_inventory_fn=lambda inventory, output_dir, limit=None: FakeSyncResult(Path(temp_dir)),
            )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(0, exit_code)
            self.assertEqual(1, payload["summary"]["exported_notes"])
            self.assertEqual(1, payload["summary"]["new_notes"])

    def test_incremental_export_only_loads_local_payloads_for_planned_notes(self) -> None:
        cli = import_or_fail(self, "wiz_to_obsidian.cli")
        models = import_or_fail(self, "wiz_to_obsidian.models")

        metadata_inventory = models.Inventory(
            notes=(
                models.WizNote(
                    kb_name="Main KB",
                    kb_guid="kb-1",
                    doc_guid="doc-same",
                    title="Same",
                    folder_parts=("Inbox",),
                    updated_at=datetime(2026, 4, 4, 10, 0, tzinfo=timezone.utc),
                ),
                models.WizNote(
                    kb_name="Main KB",
                    kb_guid="kb-1",
                    doc_guid="doc-new",
                    title="New",
                    folder_parts=("Inbox",),
                    updated_at=datetime(2026, 4, 4, 11, 0, tzinfo=timezone.utc),
                ),
            )
        )
        captured = {}

        class FakeSyncResult:
            def __init__(self, output_dir: Path) -> None:
                self.output_dir = output_dir
                self.report_path = output_dir / "_wiz" / "report.json"
                self.report = {
                    "summary": {
                        "total_notes": 2,
                        "exported_notes": 1,
                        "skipped_notes": 1,
                        "new_notes": 1,
                        "updated_notes": 0,
                        "moved_notes": 0,
                        "removed_old_paths": 0,
                        "exported_resources": 0,
                        "exported_attachments": 0,
                    }
                }

        def fake_load_payloads(*, metadata_inventory, doc_guids, leveldb_dir=None, blob_dir=None, source=None):
            captured["doc_guids"] = set(doc_guids)
            return models.Inventory(
                notes=tuple(note for note in metadata_inventory.notes if note.doc_guid in doc_guids),
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir) / "_wiz"
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / "state.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "notes": {
                            "doc-same": {
                                "relative_path": "Inbox/Same.md",
                                "updated": "2026-04-04T10:00:00+00:00",
                                "needs_repair": False,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            exit_code = cli.main(
                ["export", "--output", temp_dir, "--incremental"],
                stdout=io.StringIO(),
                scan_inventory_metadata_fn=lambda leveldb_dir, blob_dir: metadata_inventory,
                load_note_payloads_fn=fake_load_payloads,
                incremental_sync_inventory_fn=lambda inventory, output_dir, limit=None, progress=None, plan=None, sync_state=None: FakeSyncResult(Path(temp_dir)),
            )

        self.assertEqual(0, exit_code)
        self.assertEqual({"doc-new"}, captured["doc_guids"])

    def test_incremental_export_skips_payload_loading_and_hydration_when_plan_is_empty(self) -> None:
        cli = import_or_fail(self, "wiz_to_obsidian.cli")
        models = import_or_fail(self, "wiz_to_obsidian.models")

        metadata_inventory = models.Inventory(
            notes=(
                models.WizNote(
                    kb_name="Main KB",
                    kb_guid="kb-1",
                    doc_guid="doc-same",
                    title="Same",
                    folder_parts=("Inbox",),
                    updated_at=datetime(2026, 4, 4, 10, 0, tzinfo=timezone.utc),
                ),
            )
        )

        class FakeSyncResult:
            def __init__(self, output_dir: Path) -> None:
                self.output_dir = output_dir
                self.report_path = output_dir / "_wiz" / "report.json"
                self.report = {
                    "summary": {
                        "total_notes": 1,
                        "exported_notes": 0,
                        "skipped_notes": 1,
                        "new_notes": 0,
                        "updated_notes": 0,
                        "moved_notes": 0,
                        "removed_old_paths": 0,
                        "exported_resources": 0,
                        "exported_attachments": 0,
                    }
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir) / "_wiz"
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / "state.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "notes": {
                            "doc-same": {
                                "relative_path": "Inbox/Same.md",
                                "updated": "2026-04-04T10:00:00+00:00",
                                "needs_repair": False,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            exit_code = cli.main(
                ["export", "--output", temp_dir, "--incremental", "--hydrate-missing"],
                stdout=io.StringIO(),
                scan_inventory_metadata_fn=lambda leveldb_dir, blob_dir: metadata_inventory,
                load_note_payloads_fn=lambda **kwargs: (_ for _ in ()).throw(AssertionError("payload load not expected")),
                build_hydration_client_fn=lambda args: mock.MagicMock(),
                incremental_sync_inventory_fn=lambda inventory, output_dir, limit=None, progress=None, plan=None, sync_state=None, hydration_repair_status=None, doc_version=0: FakeSyncResult(Path(temp_dir)),
            )

        self.assertEqual(0, exit_code)

    def test_incremental_export_writes_state_file(self) -> None:
        cli = import_or_fail(self, "wiz_to_obsidian.cli")
        models = import_or_fail(self, "wiz_to_obsidian.models")

        metadata_inventory = models.Inventory(
            notes=(
                models.WizNote(
                    kb_name="Main KB",
                    kb_guid="kb-1",
                    doc_guid="doc-1",
                    title="Roadmap",
                    folder_parts=("Inbox",),
                    updated_at=datetime(2026, 4, 4, 11, 0, tzinfo=timezone.utc),
                ),
            )
        )
        payload_inventory = models.Inventory(
            notes=(
                models.WizNote(
                    kb_name="Main KB",
                    kb_guid="kb-1",
                    doc_guid="doc-1",
                    title="Roadmap",
                    folder_parts=("Inbox",),
                    updated_at=datetime(2026, 4, 4, 11, 0, tzinfo=timezone.utc),
                    body=models.NoteBody(markdown="# Ready"),
                ),
            )
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            exit_code = cli.main(
                ["export", "--output", temp_dir, "--incremental"],
                stdout=io.StringIO(),
                scan_inventory_metadata_fn=lambda leveldb_dir, blob_dir: metadata_inventory,
                load_note_payloads_fn=lambda **kwargs: payload_inventory,
            )

            self.assertEqual(0, exit_code)
            state_path = Path(temp_dir) / "_wiz" / "state.json"
            self.assertTrue(state_path.exists())
            state_payload = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual("Inbox/Roadmap.md", state_payload["notes"]["doc-1"]["relative_path"])
            self.assertEqual("2026-04-04T11:00:00+00:00", state_payload["notes"]["doc-1"]["updated"])

    def test_export_command_writes_progress_updates_to_stderr(self) -> None:
        cli = import_or_fail(self, "wiz_to_obsidian.cli")
        models = import_or_fail(self, "wiz_to_obsidian.models")
        hydration = import_or_fail(self, "wiz_to_obsidian.wiz_hydration")

        inventory = models.Inventory(
            notes=(
                models.WizNote(
                    kb_name="Main KB",
                    kb_guid="kb-1",
                    doc_guid="doc-1",
                    title="Roadmap",
                    body=models.NoteBody(markdown="# Ready"),
                ),
            )
        )

        class FakeExportResult:
            def __init__(self, output_dir: Path) -> None:
                self.output_dir = output_dir
                self.report_path = output_dir / "_wiz" / "report.json"
                self.report = {
                    "summary": {
                        "total_notes": 1,
                        "exported_notes": 1,
                        "missing_body_count": 0,
                        "missing_resource_count": 0,
                        "exported_resources": 0,
                        "exported_attachments": 0,
                    }
                }

        def fake_hydrate(*, inventory, client, progress=None):
            if progress is not None:
                progress("1/1 Roadmap")
            return hydration.HydrationResult(
                inventory=inventory,
                summary={
                    "hydrated_notes": 0,
                    "hydrated_resources": 0,
                    "hydrated_attachments": 0,
                    "hydration_failures": 0,
                },
            )

        def fake_export(*, inventory, output_dir, limit=None, progress=None):
            if progress is not None:
                progress("1/1 Roadmap.md")
            return FakeExportResult(output_dir)

        with tempfile.TemporaryDirectory() as temp_dir:
            stdout = io.StringIO()
            stderr = io.StringIO()
            exit_code = cli.main(
                ["export", "--output", temp_dir, "--hydrate-missing"],
                stdout=stdout,
                stderr=stderr,
                scan_inventory_fn=lambda leveldb_dir, blob_dir: inventory,
                build_hydration_client_fn=lambda args: object(),
                hydrate_inventory_fn=fake_hydrate,
                export_inventory_fn=fake_export,
            )

            self.assertEqual(0, exit_code)
            logs = stderr.getvalue()
            self.assertIn("[scan]", logs)
            self.assertIn("[hydrate] 1/1 Roadmap", logs)
            self.assertIn("[export] 1/1 Roadmap.md", logs)

    def test_export_command_writes_stage_elapsed_updates_to_stderr(self) -> None:
        cli = import_or_fail(self, "wiz_to_obsidian.cli")
        models = import_or_fail(self, "wiz_to_obsidian.models")

        inventory = models.Inventory(
            notes=(
                models.WizNote(
                    kb_name="Main KB",
                    kb_guid="kb-1",
                    doc_guid="doc-1",
                    title="Roadmap",
                    body=models.NoteBody(markdown="# Ready"),
                ),
            )
        )

        class FakeExportResult:
            def __init__(self, output_dir: Path) -> None:
                self.output_dir = output_dir
                self.report_path = output_dir / "_wiz" / "report.json"
                self.report = {
                    "summary": {
                        "total_notes": 1,
                        "exported_notes": 1,
                        "missing_body_count": 0,
                        "missing_resource_count": 0,
                        "exported_resources": 0,
                        "exported_attachments": 0,
                    }
                }
                self.sync_state = None

        def fake_export(*, inventory, output_dir, limit=None, progress=None):
            if progress is not None:
                progress("1/1 Roadmap.md")
            return FakeExportResult(output_dir)

        time_values = iter([100.0, 101.0, 102.0, 103.5, 106.0, 107.0, 108.0])
        with tempfile.TemporaryDirectory() as temp_dir:
            stdout = io.StringIO()
            stderr = io.StringIO()
            exit_code = cli.main(
                ["export", "--output", temp_dir],
                stdout=stdout,
                stderr=stderr,
                scan_inventory_fn=lambda leveldb_dir, blob_dir: inventory,
                export_inventory_fn=fake_export,
                time_fn=lambda: next(time_values),
            )

            self.assertEqual(0, exit_code)
            logs = stderr.getvalue()
            self.assertIn("[scan] done in 1.00s", logs)
            self.assertIn("[export] done in 2.50s", logs)
            self.assertIn("[done] total elapsed: 8.00s", logs)

    def test_incremental_export_refreshes_updated_note_body_when_hydrating(self) -> None:
        cli = import_or_fail(self, "wiz_to_obsidian.cli")
        models = import_or_fail(self, "wiz_to_obsidian.models")

        inventory = models.Inventory(
            notes=(
                models.WizNote(
                    kb_name="Main KB",
                    kb_guid="kb-1",
                    doc_guid="doc-1",
                    title="Roadmap",
                    folder_parts=("Inbox",),
                    updated_at=datetime(2026, 4, 4, 11, 0, tzinfo=timezone.utc),
                    body=models.NoteBody(markdown="# Old Body"),
                ),
            )
        )

        class FakeClient:
            def fetch_note_body(self, note):
                return models.NoteBody(markdown="# New Body")

            def fetch_resource(self, note, resource_name: str):
                return None

            def fetch_attachment(self, note, attachment):
                return None

        with tempfile.TemporaryDirectory() as temp_dir:
            note_path = Path(temp_dir) / "Inbox" / "Roadmap.md"
            note_path.parent.mkdir(parents=True, exist_ok=True)
            note_path.write_text(
                "\n".join(
                    [
                        "---",
                        "wiz_doc_guid: doc-1",
                        "updated: 2026-04-04T10:00:00+00:00",
                        "---",
                        "",
                        "# Previously Exported",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            stderr = io.StringIO()
            exit_code = cli.main(
                ["export", "--output", temp_dir, "--incremental", "--hydrate-missing"],
                stdout=stdout,
                stderr=stderr,
                scan_inventory_metadata_fn=lambda leveldb_dir, blob_dir: inventory,
                load_note_payloads_fn=lambda **kwargs: kwargs.get("metadata_inventory", inventory),
                build_hydration_client_fn=lambda args: FakeClient(),
            )

            self.assertEqual(0, exit_code)
            updated_text = note_path.read_text(encoding="utf-8")
            self.assertIn("# New Body", updated_text)
            self.assertNotIn("# Old Body", updated_text)

    def test_build_hydration_client_does_not_enable_remote_from_cached_token_alone(self) -> None:
        cli = import_or_fail(self, "wiz_to_obsidian.cli")

        class FakeCachedClient:
            def __init__(self) -> None:
                self.cached_auth = type("Auth", (), {"token": "cached-token", "ks_server_url": "https://ks.wiz.cn"})()

        args = type(
            "Args",
            (),
            {
                "cache_dir": Path("C:/fake-cache"),
                "wiz_user_id": None,
                "wiz_password": None,
                "wiz_token": None,
                "wiz_auto_login_param": None,
                "wiz_server_url": "https://as.wiz.cn",
                "wiz_ks_url": None,
            },
        )()

        fake_cached_client = FakeCachedClient()
        with mock.patch.object(cli, "ChromiumCacheBackend", return_value=object()), mock.patch.object(
            cli, "CachedWizClient", return_value=fake_cached_client
        ), mock.patch.object(cli, "RemoteWizClient") as remote_client:
            result = cli._build_hydration_client(args)

        self.assertIs(result, fake_cached_client)
        remote_client.assert_not_called()

    def test_main_loads_dotenv_before_parser_defaults(self) -> None:
        cli = import_or_fail(self, "wiz_to_obsidian.cli")
        models = import_or_fail(self, "wiz_to_obsidian.models")

        inventory = models.Inventory(notes=())
        captured = {}

        def fake_builder(args):
            captured["wiz_user_id"] = args.wiz_user_id
            captured["wiz_password"] = args.wiz_password
            raise RuntimeError("stop-after-capture")

        with tempfile.TemporaryDirectory() as temp_dir:
            dotenv_path = Path(temp_dir) / ".env"
            dotenv_path.write_text("WIZ_USER_ID=test-user\nWIZ_PASSWORD=test-pass\n", encoding="utf-8")
            cwd = Path.cwd()
            try:
                os.chdir(temp_dir)
                with mock.patch.dict(
                    "os.environ",
                    {
                        key: value
                        for key, value in os.environ.items()
                        if key
                        not in {
                            "WIZ_USER_ID",
                            "WIZ_PASSWORD",
                            "WIZ_TOKEN",
                            "WIZ_AUTO_LOGIN_PARAM",
                            "WIZ_SERVER_URL",
                            "WIZ_KS_URL",
                        }
                    },
                    clear=True,
                ):
                    with self.assertRaisesRegex(RuntimeError, "stop-after-capture"):
                        cli.main(
                            ["export", "--hydrate-missing", "--output", str(Path(temp_dir) / "out")],
                            stdout=io.StringIO(),
                            scan_inventory_fn=lambda leveldb_dir, blob_dir: inventory,
                            build_hydration_client_fn=fake_builder,
                        )
            finally:
                os.chdir(cwd)

        self.assertEqual("test-user", captured["wiz_user_id"])
        self.assertEqual("test-pass", captured["wiz_password"])

    def test_main_loads_dotenv_aliases_and_colon_syntax(self) -> None:
        cli = import_or_fail(self, "wiz_to_obsidian.cli")
        models = import_or_fail(self, "wiz_to_obsidian.models")

        inventory = models.Inventory(notes=())
        captured = {}

        def fake_builder(args):
            captured["wiz_user_id"] = args.wiz_user_id
            captured["wiz_password"] = args.wiz_password
            raise RuntimeError("stop-after-capture")

        with tempfile.TemporaryDirectory() as temp_dir:
            dotenv_path = Path(temp_dir) / ".env"
            dotenv_path.write_text("user_id: test-user\npw: test-pass\n", encoding="utf-8")
            cwd = Path.cwd()
            try:
                os.chdir(temp_dir)
                with mock.patch.dict(
                    "os.environ",
                    {
                        key: value
                        for key, value in os.environ.items()
                        if key
                        not in {
                            "WIZ_USER_ID",
                            "WIZ_PASSWORD",
                            "WIZ_TOKEN",
                            "WIZ_AUTO_LOGIN_PARAM",
                            "WIZ_SERVER_URL",
                            "WIZ_KS_URL",
                        }
                    },
                    clear=True,
                ):
                    with self.assertRaisesRegex(RuntimeError, "stop-after-capture"):
                        cli.main(
                            ["export", "--hydrate-missing", "--output", str(Path(temp_dir) / "out")],
                            stdout=io.StringIO(),
                            scan_inventory_fn=lambda leveldb_dir, blob_dir: inventory,
                            build_hydration_client_fn=fake_builder,
                        )
            finally:
                os.chdir(cwd)

        self.assertEqual("test-user", captured["wiz_user_id"])
        self.assertEqual("test-pass", captured["wiz_password"])


if __name__ == "__main__":
    unittest.main()
