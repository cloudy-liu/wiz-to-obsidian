from __future__ import annotations

import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "sync_wiz_to_obsidian.py"


def load_script_module(testcase: unittest.TestCase):
    spec = importlib.util.spec_from_file_location("sync_wiz_to_obsidian", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        testcase.fail(f"expected script module at {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules.pop("sync_wiz_to_obsidian", None)
    spec.loader.exec_module(module)
    return module


class SyncScriptTests(unittest.TestCase):
    def test_build_export_args_includes_hydration_by_default(self) -> None:
        module = load_script_module(self)

        args = module.build_export_args(
            output="D:\\vault\\WizSync",
            hydrate=True,
            limit=0,
        )

        self.assertEqual(
            [
                "-m",
                "wiz_to_obsidian.cli",
                "export",
                "--output",
                "D:\\vault\\WizSync",
                "--incremental",
                "--hydrate-missing",
            ],
            args,
        )

    def test_build_export_args_includes_optional_path_overrides(self) -> None:
        module = load_script_module(self)

        args = module.build_export_args(
            output="D:\\vault\\Wiz",
            hydrate=True,
            limit=3,
            leveldb_dir="C:\\wiz\\leveldb",
            blob_dir="C:\\wiz\\blob",
            cache_dir="C:\\wiz\\cache",
        )

        self.assertEqual(
            [
                "-m",
                "wiz_to_obsidian.cli",
                "export",
                "--output",
                "D:\\vault\\Wiz",
                "--incremental",
                "--hydrate-missing",
                "--leveldb-dir",
                "C:\\wiz\\leveldb",
                "--blob-dir",
                "C:\\wiz\\blob",
                "--cache-dir",
                "C:\\wiz\\cache",
                "--limit",
                "3",
            ],
            args,
        )

    def test_dry_run_prints_command_without_spawning_subprocess(self) -> None:
        module = load_script_module(self)
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout), mock.patch.object(module.subprocess, "run") as run:
            exit_code = module.main(["--dry-run", "--output", "D:\\vault\\Wiz", "--no-hydrate", "--limit", "5"])

        self.assertEqual(0, exit_code)
        run.assert_not_called()
        output = stdout.getvalue()
        self.assertIn("Mode   : incremental", output)
        self.assertIn("Hydrate: False", output)
        self.assertIn("--limit 5", output)
        self.assertNotIn("--hydrate-missing", output)

    def test_full_mode_omits_incremental_flag(self) -> None:
        module = load_script_module(self)
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout), mock.patch.object(module.subprocess, "run") as run:
            exit_code = module.main(["--dry-run", "--output", "D:\\vault\\Wiz", "--full"])

        self.assertEqual(0, exit_code)
        run.assert_not_called()
        output = stdout.getvalue()
        self.assertIn("Mode   : full", output)
        self.assertNotIn("--incremental", output)

    def test_dry_run_requires_output_when_env_missing(self) -> None:
        module = load_script_module(self)
        stdout = io.StringIO()
        stderr = io.StringIO()

        with (
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
            mock.patch.dict(module.os.environ, {}, clear=True),
        ):
            with self.assertRaises(SystemExit) as error:
                module.main(["--dry-run"])

        self.assertEqual(2, error.exception.code)
        self.assertIn("--output", stderr.getvalue())

    def test_dry_run_uses_output_env_when_present(self) -> None:
        module = load_script_module(self)
        stdout = io.StringIO()

        with (
            contextlib.redirect_stdout(stdout),
            mock.patch.object(module.subprocess, "run") as run,
            mock.patch.dict(module.os.environ, {"WIZ_TO_OBSIDIAN_OUTPUT_DIR": "D:\\vault\\Wiz"}),
        ):
            exit_code = module.main(["--dry-run"])

        self.assertEqual(0, exit_code)
        run.assert_not_called()
        output = stdout.getvalue()
        self.assertIn("Output : D:\\vault\\Wiz", output)

    def test_dry_run_reads_output_from_repo_dotenv(self) -> None:
        module = load_script_module(self)
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / ".env").write_text("WIZ_TO_OBSIDIAN_OUTPUT_DIR=D:\\vault\\Wiz\n", encoding="utf-8")

            with (
                contextlib.redirect_stdout(stdout),
                mock.patch.object(module, "repo_root", return_value=temp_root),
                mock.patch.object(module.subprocess, "run") as run,
                mock.patch.dict(module.os.environ, {}, clear=True),
            ):
                exit_code = module.main(["--dry-run"])

        self.assertEqual(0, exit_code)
        run.assert_not_called()
        output = stdout.getvalue()
        self.assertIn("Output : D:\\vault\\Wiz", output)

    def test_dry_run_prints_elapsed_time(self) -> None:
        module = load_script_module(self)
        stdout = io.StringIO()
        time_values = iter([10.0, 12.5])

        with contextlib.redirect_stdout(stdout), mock.patch.object(module.subprocess, "run") as run:
            exit_code = module.main(["--dry-run", "--output", "D:\\vault\\Wiz"], time_fn=lambda: next(time_values))

        self.assertEqual(0, exit_code)
        run.assert_not_called()
        output = stdout.getvalue()
        self.assertIn("Elapsed: 2.50s", output)


if __name__ == "__main__":
    unittest.main()
