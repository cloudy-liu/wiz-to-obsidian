from __future__ import annotations

import importlib
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


class ReportingTests(unittest.TestCase):
    def test_report_summarizes_missing_bodies_and_resources(self) -> None:
        module = import_or_fail(self, "wiz_to_obsidian.reporting")

        report = module.build_export_report(
            total_notes=10,
            exported_notes=7,
            missing_bodies=("doc-3", "doc-5"),
            missing_resources=("doc-1:cover.png",),
            exported_resources=12,
            exported_attachments=4,
        )

        self.assertEqual(10, report["summary"]["total_notes"])
        self.assertEqual(7, report["summary"]["exported_notes"])
        self.assertEqual(2, report["summary"]["missing_body_count"])
        self.assertEqual(1, report["summary"]["missing_resource_count"])
        self.assertEqual(("doc-3", "doc-5"), tuple(report["missing_bodies"]))


if __name__ == "__main__":
    unittest.main()
