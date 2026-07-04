from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path


class PostProcessTests(unittest.TestCase):
    def test_rewrite_tables_dry_run_reports_without_writing(self) -> None:
        from wiz_to_obsidian.postprocess import rewrite_tables

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            note = root / "Tools.md"
            original = "<table><tr><th>A</th></tr><tr><td>B</td></tr></table>\n"
            note.write_text(original, encoding="utf-8")

            result = rewrite_tables(root)

            self.assertEqual(original, note.read_text(encoding="utf-8"))
            self.assertTrue(result.dry_run)
            self.assertEqual(1, result.markdown_files)
            self.assertEqual(1, result.html_tables)
            self.assertEqual(1, result.converted_tables)
            self.assertEqual(1, result.changed_files)
            self.assertFalse((root / "_wiz" / "rewrite-tables-report.json").exists())

    def test_rewrite_tables_output_copy_leaves_input_unchanged(self) -> None:
        from wiz_to_obsidian.postprocess import rewrite_tables

        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            input_dir = base / "vault"
            output_dir = base / "vault-table-md"
            input_dir.mkdir()
            note = input_dir / "Tools.md"
            original = "<table><tr><th>A</th></tr><tr><td>B</td></tr></table>\n"
            note.write_text(original, encoding="utf-8")
            (input_dir / "asset.bin").write_bytes(b"asset")

            result = rewrite_tables(input_dir, output_dir=output_dir)

            self.assertEqual(original, note.read_text(encoding="utf-8"))
            self.assertIn("| A |", (output_dir / "Tools.md").read_text(encoding="utf-8"))
            self.assertEqual(b"asset", (output_dir / "asset.bin").read_bytes())
            self.assertFalse(result.dry_run)
            report = json.loads((output_dir / "_wiz" / "rewrite-tables-report.json").read_text(encoding="utf-8"))
            self.assertEqual(1, report["summary"]["converted_tables"])

    def test_rewrite_tables_refuses_existing_output_without_force(self) -> None:
        from wiz_to_obsidian.postprocess import rewrite_tables

        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            input_dir = base / "vault"
            output_dir = base / "existing"
            input_dir.mkdir()
            output_dir.mkdir()

            with self.assertRaises(FileExistsError):
                rewrite_tables(input_dir, output_dir=output_dir)

    def test_rewrite_tables_write_updates_input_and_reports_skips(self) -> None:
        from wiz_to_obsidian.postprocess import rewrite_tables

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            convertible = root / "Tools.md"
            skipped = root / "Code.md"
            markdown_table = root / "Already.md"
            convertible.write_text("<table><tr><th>A</th></tr><tr><td>B</td></tr></table>", encoding="utf-8")
            skipped.write_text("<table><tr><td><pre>x</pre></td></tr></table>", encoding="utf-8")
            markdown_table.write_text("| A |\n| --- |\n| B |\n", encoding="utf-8")

            result = rewrite_tables(root, write=True)

            self.assertIn("| A |", convertible.read_text(encoding="utf-8"))
            self.assertIn("<table>", skipped.read_text(encoding="utf-8"))
            self.assertEqual("| A |\n| --- |\n| B |\n", markdown_table.read_text(encoding="utf-8"))
            self.assertEqual(3, result.markdown_files)
            self.assertEqual(2, result.html_tables)
            self.assertEqual(1, result.converted_tables)
            self.assertEqual(1, result.skipped_tables)
            self.assertEqual(1, result.skipped_reasons["pre_block"])
            self.assertTrue((root / "_wiz" / "rewrite-tables-report.json").exists())

    def test_rewrite_tables_rejects_write_with_output(self) -> None:
        from wiz_to_obsidian.postprocess import rewrite_tables

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self.assertRaises(ValueError):
                rewrite_tables(root, output_dir=root / "copy", write=True)


if __name__ == "__main__":
    unittest.main()
