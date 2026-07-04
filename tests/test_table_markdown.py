from __future__ import annotations

import unittest


class TableMarkdownTests(unittest.TestCase):
    def test_converts_basic_html_table_to_markdown_table(self) -> None:
        from wiz_to_obsidian.table_markdown import convert_html_tables_in_markdown

        source = (
            "Before\n\n"
            "<table>"
            "<tr><th><strong>#</strong></th><th><strong>Name</strong></th></tr>"
            "<tr><td>1</td><td><strong>VSCode</strong></td></tr>"
            "</table>"
            "\n\nAfter"
        )

        converted, stats = convert_html_tables_in_markdown(source)

        self.assertIn("| **#** | **Name** |", converted)
        self.assertIn("| --- | --- |", converted)
        self.assertIn("| 1 | **VSCode** |", converted)
        self.assertIn("Before", converted)
        self.assertIn("After", converted)
        self.assertEqual(1, stats.html_tables)
        self.assertEqual(1, stats.converted_tables)
        self.assertEqual(0, stats.skipped_tables)

    def test_preserves_image_cells_as_html_to_keep_table_rendering_stable(self) -> None:
        from wiz_to_obsidian.table_markdown import convert_html_tables_in_markdown

        source = (
            "<table>"
            "<tr><th>Feature</th><th>Image</th></tr>"
            '<tr><td>Whitespace</td><td><img src="../_wiz/resources/doc/screen.png" alt=""></td></tr>'
            "</table>"
        )

        converted, stats = convert_html_tables_in_markdown(source)

        self.assertIn('<img src="../_wiz/resources/doc/screen.png" alt="">', converted)
        self.assertNotIn("<table>", converted)
        self.assertEqual(1, stats.converted_tables)

    def test_converts_inline_markup_links_code_breaks_and_escapes_pipes(self) -> None:
        from wiz_to_obsidian.table_markdown import convert_html_tables_in_markdown

        source = (
            "<table>"
            "<tr><th>Kind</th><th>Notes</th></tr>"
            "<tr>"
            "<td><em>A | B</em></td>"
            '<td>Use <code>x | y</code><br><a href="https://example.com">docs</a></td>'
            "</tr>"
            "</table>"
        )

        converted, _ = convert_html_tables_in_markdown(source)

        self.assertIn("| *A \\| B* | Use `x \\| y`<br>[docs](https://example.com) |", converted)

    def test_skips_tables_with_rowspan_or_pre_blocks(self) -> None:
        from wiz_to_obsidian.table_markdown import convert_html_tables_in_markdown

        source = (
            "<table><tr><td rowspan=\"2\">A</td><td>B</td></tr></table>\n"
            "<table><tr><td><pre>line 1\nline 2</pre></td></tr></table>"
        )

        converted, stats = convert_html_tables_in_markdown(source)

        self.assertEqual(source, converted)
        self.assertEqual(2, stats.html_tables)
        self.assertEqual(0, stats.converted_tables)
        self.assertEqual(2, stats.skipped_tables)
        self.assertEqual(1, stats.skipped_reasons["span"])
        self.assertEqual(1, stats.skipped_reasons["pre_block"])


if __name__ == "__main__":
    unittest.main()
