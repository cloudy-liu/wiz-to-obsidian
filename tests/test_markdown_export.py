from __future__ import annotations

import importlib
import re
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


class MarkdownExportTests(unittest.TestCase):
    @staticmethod
    def _body_after_frontmatter(markdown: str) -> str:
        match = re.search(r"^---\n.*?\n---\n\n(?P<body>.*)\n?$", markdown, flags=re.DOTALL)
        if not match:
            raise AssertionError("expected markdown frontmatter wrapper")
        return match.group("body")

    def test_note_path_preserves_folder_hierarchy_and_avoids_collisions(self) -> None:
        module = import_or_fail(self, "wiz_to_obsidian.markdown_export")
        resolver = module.NotePathResolver()

        note_one = module.NoteForExport(
            kb_name="Main KB",
            kb_guid="kb-1",
            doc_guid="doc-1",
            title="Plan/Review",
            folder_parts=("Projects", "2026"),
            tags=("work",),
            note_type="lite/markdown",
            created_at=datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc),
            updated_at=datetime(2026, 3, 2, 10, 0, tzinfo=timezone.utc),
            body_markdown="# Draft",
        )
        note_two = module.NoteForExport(
            kb_name="Main KB",
            kb_guid="kb-1",
            doc_guid="doc-2",
            title="Plan/Review",
            folder_parts=("Projects", "2026"),
            tags=("work",),
            note_type="lite/markdown",
            created_at=datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc),
            updated_at=datetime(2026, 3, 2, 10, 0, tzinfo=timezone.utc),
            body_markdown="# Draft",
        )

        path_one = resolver.note_relative_path(note_one)
        path_two = resolver.note_relative_path(note_two)

        self.assertEqual(Path("Projects/2026/Plan-Review.md"), path_one)
        self.assertEqual(Path("Projects/2026/Plan-Review--doc-2.md"), path_two)

    def test_render_markdown_writes_frontmatter_and_rewrites_links(self) -> None:
        module = import_or_fail(self, "wiz_to_obsidian.markdown_export")
        note = module.NoteForExport(
            kb_name="Main KB",
            kb_guid="kb-1",
            doc_guid="doc-1",
            title="Quarterly Plan",
            folder_parts=("Projects",),
            tags=("work", "planning"),
            note_type="lite/markdown",
            created_at=datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc),
            updated_at=datetime(2026, 3, 2, 10, 0, tzinfo=timezone.utc),
            body_html='<p>Hello</p><img src="wiz-resource://cover.png"><p><a href="wiz-attachment://spec.pdf">Spec</a></p>',
        )

        markdown = module.render_note_markdown(
            note=note,
            resource_paths={
                "wiz-resource://cover.png": Path("assets/doc-1/cover.png"),
                "wiz-attachment://spec.pdf": Path("attachments/doc-1/spec.pdf"),
            },
        )

        self.assertIn("---", markdown)
        self.assertIn("title: Quarterly Plan", markdown)
        self.assertIn("wiz_doc_guid: doc-1", markdown)
        self.assertIn("wiz_kb_name: Main KB", markdown)
        self.assertIn("- work", markdown)
        self.assertIn("- planning", markdown)
        self.assertIn("![](assets/doc-1/cover.png)", markdown)
        self.assertIn("[Spec](attachments/doc-1/spec.pdf)", markdown)

    def test_render_markdown_strips_document_head_and_styles_before_html_conversion(self) -> None:
        module = import_or_fail(self, "wiz_to_obsidian.markdown_export")
        note = module.NoteForExport(
            kb_name="Main KB",
            kb_guid="kb-1",
            doc_guid="doc-3",
            title="Wechat Clip",
            body_html=(
                "<!doctype html><html><head><style>.hidden{display:none}</style>"
                "<script>console.log('x')</script><title>Ignore Me</title></head>"
                '<body><p>Hello</p><img src="cover.png"></body></html>'
            ),
        )

        markdown = module.render_note_markdown(
            note=note,
            resource_paths={"cover.png": Path("_wiz/resources/doc-3/cover.png")},
        )

        self.assertIn("Hello", markdown)
        self.assertIn("![](_wiz/resources/doc-3/cover.png)", markdown)
        self.assertNotIn("display:none", markdown)
        self.assertNotIn("console.log", markdown)
        self.assertNotIn("Ignore Me", markdown)

    def test_render_markdown_ignores_commented_fake_body_tags(self) -> None:
        module = import_or_fail(self, "wiz_to_obsidian.markdown_export")
        note = module.NoteForExport(
            kb_name="Main KB",
            kb_guid="kb-1",
            doc_guid="doc-3a",
            title="Wechat Clip",
            body_html=(
                "<!DOCTYPE html><!--headTrap<body></body><head></head><html></html>-->"
                "<html><head><title>Keep Me</title></head>"
                "<body><h1>Hello</h1><p>World</p></body></html>"
            ),
        )

        markdown = module.render_note_markdown(note=note, resource_paths={})

        self.assertIn("# Hello", markdown)
        self.assertIn("World", markdown)

    def test_render_markdown_drops_empty_images_and_promotes_lazy_sources(self) -> None:
        module = import_or_fail(self, "wiz_to_obsidian.markdown_export")
        note = module.NoteForExport(
            kb_name="Main KB",
            kb_guid="kb-1",
            doc_guid="doc-3b",
            title="Wechat Clip",
            body_html=(
                "<html><body>"
                '<img class="avatar" src="" alt="">'
                '<img data-src="https://example.com/cover.png" alt="cover">'
                "</body></html>"
            ),
        )

        markdown = module.render_note_markdown(note=note, resource_paths={})

        self.assertNotIn("![]()", markdown)
        self.assertIn("![cover](https://example.com/cover.png)", markdown)

    def test_render_markdown_unwraps_preformatted_markdown_html(self) -> None:
        module = import_or_fail(self, "wiz_to_obsidian.markdown_export")
        note = module.NoteForExport(
            kb_name="Main KB",
            kb_guid="kb-1",
            doc_guid="doc-4",
            title="Hydrated Markdown",
            body_html=(
                "<!doctype html><html><body><pre># Heading\n\n"
                "Paragraph text.\n\n"
                "```python\nprint('ok')\n```\n\n"
                "![image](index_files/cover.png)\n"
                "</pre></body></html>"
            ),
        )

        markdown = module.render_note_markdown(
            note=note,
            resource_paths={"index_files/cover.png": Path("_wiz/resources/doc-4/cover.png")},
        )
        body = self._body_after_frontmatter(markdown)

        self.assertTrue(body.startswith("# Heading"))
        self.assertIn("Paragraph text.", body)
        self.assertIn("```python\nprint('ok')\n```", body)
        self.assertIn("![image](_wiz/resources/doc-4/cover.png)", body)
        self.assertNotIn("```\n# Heading", body)


if __name__ == "__main__":
    unittest.main()
