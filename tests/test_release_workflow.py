from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "release.yml"


class ReleaseWorkflowTests(unittest.TestCase):
    def test_release_workflow_builds_x64_zip_per_platform(self) -> None:
        text = WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertIn("windows-2022", text)
        self.assertIn("ubuntu-22.04", text)
        self.assertIn("macos-13", text)
        self.assertIn("arch: x64", text)
        self.assertIn("binary_name: wiz2obs_cli.exe", text)
        self.assertIn("binary_name: wiz2obs_cli", text)
        self.assertIn("pyinstaller --onefile --name wiz2obs_cli", text)
        self.assertIn("wiz2obs_cli-${GITHUB_REF_NAME}_${{ matrix.platform }}_${{ matrix.arch }}.zip", text)


if __name__ == "__main__":
    unittest.main()
