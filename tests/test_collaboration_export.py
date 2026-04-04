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


class CollaborationRenderTests(unittest.TestCase):
    def test_render_collaboration_document_supports_common_block_types(self) -> None:
        module = import_or_fail(self, "wiz_to_obsidian.markdown_export")

        payload = {
            "blocks": [
                {
                    "id": "h1",
                    "type": "text",
                    "heading": 1,
                    "text": [{"insert": "Roadmap"}],
                },
                {
                    "id": "p1",
                    "type": "text",
                    "text": [
                        {"insert": "Bold", "attributes": {"style-bold": True}},
                        {"insert": " link", "attributes": {"link": "https://example.com"}},
                    ],
                },
                {
                    "id": "q1",
                    "type": "text",
                    "quoted": True,
                    "text": [{"insert": "Quoted"}],
                },
                {
                    "id": "l1",
                    "type": "list",
                    "level": 1,
                    "ordered": False,
                    "checkbox": "checked",
                    "text": [{"insert": "Ship"}],
                },
                {
                    "id": "l2",
                    "type": "list",
                    "level": 2,
                    "ordered": True,
                    "start": 3,
                    "text": [{"insert": "Third"}],
                },
                {
                    "id": "code1",
                    "type": "code",
                    "language": "python",
                    "code": "print('hi')",
                },
                {
                    "id": "img1",
                    "type": "embed",
                    "embedType": "image",
                    "embedData": {"src": "cover.png"},
                },
                {
                    "id": "file1",
                    "type": "embed",
                    "embedType": "office",
                    "embedData": {"src": "slides.pdf", "fileName": "slides.pdf"},
                },
                {
                    "id": "hr1",
                    "type": "embed",
                    "embedType": "hr",
                    "embedData": {},
                },
                {
                    "id": "secret1",
                    "type": "embed",
                    "embedType": "encrypt-text",
                    "embedData": {"prompt": "hint"},
                },
            ]
        }

        markdown = module.render_collaboration_document(payload, doc_guid="doc-1")

        self.assertIn("# Roadmap", markdown)
        self.assertIn("**Bold**", markdown)
        self.assertIn("[ link](https://example.com)", markdown)
        self.assertIn("> Quoted", markdown)
        self.assertIn("- [x] Ship", markdown)
        self.assertIn("  3. Third", markdown)
        self.assertIn("```python", markdown)
        self.assertIn("print('hi')", markdown)
        self.assertIn("![](wiz-resource://doc-1/cover.png)", markdown)
        self.assertIn("[slides.pdf](wiz-resource://doc-1/slides.pdf)", markdown)
        self.assertIn("\n---\n", markdown)
        self.assertIn("Encrypted content omitted", markdown)

    def test_render_collaboration_payload_restores_table_cells_from_snapshot_lookup(self) -> None:
        module = import_or_fail(self, "wiz_to_obsidian.markdown_export")

        payload = {
            "blocks": [
                {"id": "intro", "type": "text", "text": [{"insert": "BatchTP"}]},
                {
                    "id": "table-1",
                    "type": "table",
                    "rows": 3,
                    "cols": 2,
                    "children": ["c11", "c12", "c21", "c22", "c31", "c32"],
                },
            ],
            "c11": [{"type": "text", "text": [{"insert": "类型", "attributes": {"style-bold": True}}]}],
            "c12": [{"type": "text", "text": [{"insert": "说明", "attributes": {"style-bold": True}}]}],
            "c21": [{"type": "text", "text": [{"insert": "函数"}]}],
            "c22": [{"type": "text", "text": [{"insert": "查询 trace processor"}]}],
            "c31": [{"type": "text", "text": [{"insert": "备注"}]}],
            "c32": [{"type": "text", "text": [{"insert": "并行跑 tp"}]}],
        }

        result = module.render_collaboration_payload(payload, doc_guid="doc-1")

        self.assertIn("| **类型** | **说明** |", result.markdown)
        self.assertIn("| 函数 | 查询 trace processor |", result.markdown)
        self.assertIn("| 备注 | 并行跑 tp |", result.markdown)
        self.assertNotIn("_Table omitted", result.markdown)
        self.assertEqual(1, result.metadata.collaboration_table_count)
        self.assertGreater(result.metadata.source_text_length, 0)

    def test_render_collaboration_payload_restores_code_blocks_from_snapshot_lookup(self) -> None:
        module = import_or_fail(self, "wiz_to_obsidian.markdown_export")

        payload = {
            "blocks": [
                {"id": "title", "type": "text", "heading": 2, "text": [{"insert": "svg-logo"}]},
                {
                    "id": "code-1",
                    "type": "code",
                    "language": "bash",
                    "children": ["code-lines-1"],
                },
            ],
            "code-lines-1": [
                {"id": "line-1", "type": "code-line", "text": [{"insert": "curl -L https://github.com"}]},
                {"id": "line-2", "type": "code-line", "text": []},
                {"id": "line-3", "type": "code-line", "text": [{"insert": "gh repo clone example/repo"}]},
            ],
        }

        result = module.render_collaboration_payload(payload, doc_guid="doc-1")

        self.assertIn("## svg-logo", result.markdown)
        self.assertIn("```bash", result.markdown)
        self.assertIn("curl -L https://github.com", result.markdown)
        self.assertIn("gh repo clone example/repo", result.markdown)
        self.assertGreaterEqual(result.metadata.source_text_length, len("curl -L https://github.com"))

    def test_render_collaboration_payload_preserves_drawio_preview_and_source_assets(self) -> None:
        module = import_or_fail(self, "wiz_to_obsidian.markdown_export")

        payload = {
            "blocks": [
                {
                    "id": "embed-1",
                    "type": "embed",
                    "embedType": "drawio",
                    "embedData": {
                        "src": "boot-flow.svg",
                        "xml": "<mxfile><diagram>flow</diagram></mxfile>",
                        "xmlSvg": "<svg><text>flow</text></svg>",
                    },
                }
            ]
        }

        result = module.render_collaboration_payload(payload, doc_guid="doc-1")
        asset_keys = {asset.key for asset in result.generated_assets}

        self.assertIn("![](wiz-resource://doc-1/boot-flow.svg)", result.markdown)
        self.assertIn("[Drawio source](wiz-resource://doc-1/boot-flow.drawio)", result.markdown)
        self.assertIn("wiz-resource://doc-1/boot-flow.drawio", asset_keys)
        self.assertIn("wiz-resource://doc-1/boot-flow.drawio.svg", asset_keys)
        self.assertEqual(1, result.metadata.collaboration_drawio_count)

    def test_render_collaboration_payload_expands_pipe_separated_image_embeds(self) -> None:
        module = import_or_fail(self, "wiz_to_obsidian.markdown_export")

        payload = {
            "blocks": [
                {"id": "h1", "type": "text", "heading": 3, "text": [{"insert": "山水画风格"}]},
                {
                    "id": "img1",
                    "type": "embed",
                    "embedType": "image",
                    "embedData": {"src": "ink.png|cartoon.png"},
                },
            ]
        }

        result = module.render_collaboration_payload(payload, doc_guid="doc-1")

        self.assertIn("##", result.markdown)
        self.assertIn("![](wiz-resource://doc-1/ink.png)", result.markdown)
        self.assertIn("![](wiz-resource://doc-1/cartoon.png)", result.markdown)
        self.assertNotIn("ink.png|cartoon.png", result.markdown)

    def test_render_collaboration_payload_treats_list_block_with_heading_as_heading(self) -> None:
        module = import_or_fail(self, "wiz_to_obsidian.markdown_export")

        payload = {
            "blocks": [
                {"id": "title", "type": "text", "heading": 1, "text": [{"insert": "Tools"}]},
                {
                    "id": "section",
                    "type": "list",
                    "ordered": True,
                    "start": 1,
                    "level": 1,
                    "heading": 2,
                    "text": [{"insert": "VSCode"}],
                },
                {"id": "sub", "type": "text", "heading": 3, "text": [{"insert": "常用设定"}]},
            ]
        }

        result = module.render_collaboration_payload(payload, doc_guid="doc-1")

        self.assertIn("# Tools", result.markdown)
        self.assertIn("## VSCode", result.markdown)
        self.assertIn("### 常用设定", result.markdown)
        self.assertNotIn("1. VSCode", result.markdown)


if __name__ == "__main__":
    unittest.main()
