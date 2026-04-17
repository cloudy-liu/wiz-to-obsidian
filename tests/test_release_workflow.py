from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "release.yml"
RELEASE_README_PATH = ROOT / "release" / "README.md"


class ReleaseWorkflowTests(unittest.TestCase):
    def test_release_workflow_builds_x64_zip_per_platform(self) -> None:
        text = WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertIn("windows-2022", text)
        self.assertIn("ubuntu-22.04", text)
        self.assertIn("macos-15-intel", text)
        self.assertIn("arch: x64", text)
        self.assertIn("binary_name: wiz2obs_cli.exe", text)
        self.assertIn("binary_name: wiz2obs_cli", text)
        self.assertIn("pip install pyinstaller pytest", text)
        self.assertIn("name: wiz2obs-cli-${{ github.ref_name }}", text)
        self.assertIn("pyinstaller --onefile --name wiz2obs_cli", text)
        self.assertIn(".env.example", text)
        self.assertIn("config.example.env", text)
        self.assertIn("release/README.md", text)
        self.assertIn("wiz2obs_cli-${GITHUB_REF_NAME}_${{ matrix.platform }}_${{ matrix.arch }}.zip", text)

    def test_release_readme_exists_for_packaged_zip(self) -> None:
        text = RELEASE_README_PATH.read_text(encoding="utf-8")

        self.assertIn("wiz2obs_cli sync", text)
        self.assertIn("config.example.env", text)


if __name__ == "__main__":
    unittest.main()
