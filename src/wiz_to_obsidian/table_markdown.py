from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from html import unescape
from html.parser import HTMLParser
from typing import Iterable


TABLE_BLOCK = re.compile(r"<table\b[^>]*>.*?</table>", re.IGNORECASE | re.DOTALL)
WHITESPACE = re.compile(r"[ \t\r\n]+")


class TableConversionMode(str, Enum):
    FIDELITY = "fidelity"
    HYBRID = "hybrid"
    EDITABLE = "editable"


@dataclass
class TableConversionStats:
    html_tables: int = 0
    converted_tables: int = 0
    skipped_tables: int = 0
    skipped_reasons: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped_tables += 1
        self.skipped_reasons[reason] = self.skipped_reasons.get(reason, 0) + 1


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["_Node | str"] = field(default_factory=list)


class _FragmentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.root = _Node("root")
        self._stack: list[_Node] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = tag.lower()
        node = _Node(normalized, {name.lower(): value or "" for name, value in attrs})
        self._stack[-1].children.append(node)
        if normalized not in {"br", "img", "hr", "meta", "link", "input"}:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = tag.lower()
        self._stack[-1].children.append(_Node(normalized, {name.lower(): value or "" for name, value in attrs}))

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == normalized:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self._stack[-1].children.append(data)

    def handle_entityref(self, name: str) -> None:
        self._stack[-1].children.append(unescape(f"&{name};"))

    def handle_charref(self, name: str) -> None:
        self._stack[-1].children.append(unescape(f"&#{name};"))


def convert_html_tables_in_markdown(
    markdown: str,
    *,
    mode: TableConversionMode | str = TableConversionMode.HYBRID,
) -> tuple[str, TableConversionStats]:
    conversion_mode = TableConversionMode(mode)
    stats = TableConversionStats()
    if conversion_mode == TableConversionMode.FIDELITY:
        stats.html_tables = len(TABLE_BLOCK.findall(markdown))
        for _ in range(stats.html_tables):
            stats.skip("fidelity")
        return markdown, stats

    def replace_table(match: re.Match[str]) -> str:
        stats.html_tables += 1
        table_html = match.group(0)
        rendered, reason = _convert_table_html(table_html)
        if rendered is None:
            stats.skip(reason)
            return table_html
        stats.converted_tables += 1
        return rendered

    return TABLE_BLOCK.sub(replace_table, markdown), stats


def _convert_table_html(table_html: str) -> tuple[str | None, str]:
    if len(re.findall(r"<table\b", table_html, flags=re.IGNORECASE)) > 1:
        return None, "nested_table"

    parser = _FragmentParser()
    parser.feed(table_html)
    table = _first_descendant(parser.root, "table")
    if table is None:
        return None, "invalid_html"
    if _has_descendant(table, "pre"):
        return None, "pre_block"

    rows: list[list[str]] = []
    for row in _descendants(table, "tr"):
        cells: list[str] = []
        for cell in _row_cells(row):
            if "rowspan" in cell.attrs or "colspan" in cell.attrs:
                return None, "span"
            cells.append(_render_cell(cell).strip())
        if cells:
            rows.append(cells)

    if not rows:
        return None, "empty_table"

    width = max(len(row) for row in rows)
    normalized_rows = [row + [""] * (width - len(row)) for row in rows]
    return _render_markdown_table(normalized_rows), ""


def _first_descendant(node: _Node, tag: str) -> _Node | None:
    for child in node.children:
        if isinstance(child, str):
            continue
        if child.tag == tag:
            return child
        found = _first_descendant(child, tag)
        if found is not None:
            return found
    return None


def _has_descendant(node: _Node, tag: str) -> bool:
    return any(True for _ in _descendants(node, tag))


def _descendants(node: _Node, tag: str) -> Iterable[_Node]:
    for child in node.children:
        if isinstance(child, str):
            continue
        if child.tag == tag:
            yield child
        yield from _descendants(child, tag)


def _row_cells(row: _Node) -> list[_Node]:
    return [child for child in row.children if isinstance(child, _Node) and child.tag in {"th", "td"}]


def _render_markdown_table(rows: list[list[str]]) -> str:
    header = rows[0]
    divider = ["---"] * len(header)
    lines = [
        "| " + " | ".join(_escape_table_cell(cell) for cell in header) + " |",
        "| " + " | ".join(divider) + " |",
    ]
    lines.extend("| " + " | ".join(_escape_table_cell(cell) for cell in row) + " |" for row in rows[1:])
    return "\n".join(lines)


def _render_cell(node: _Node) -> str:
    parts = [_render_child(child) for child in node.children]
    return _normalize_cell_text("".join(parts))


def _render_child(child: _Node | str) -> str:
    if isinstance(child, str):
        return child

    inner = "".join(_render_child(grandchild) for grandchild in child.children)
    if child.tag == "br":
        return "<br>"
    if child.tag in {"strong", "b"}:
        return f"**{_normalize_inline_text(inner)}**"
    if child.tag in {"em", "i"}:
        return f"*{_normalize_inline_text(inner)}*"
    if child.tag == "code":
        code = _normalize_inline_text(inner).replace("`", "\\`")
        return f"`{code}`"
    if child.tag == "a":
        href = child.attrs.get("href", "").strip()
        label = _normalize_inline_text(inner) or href
        return f"[{label}]({href})" if href else label
    if child.tag == "img":
        src = child.attrs.get("src", "").strip()
        if not src:
            return ""
        alt = child.attrs.get("alt", "").strip()
        return f'<img src="{src}" alt="{alt}">'
    if child.tag in {"p", "div"}:
        return f"{inner}<br>" if inner.strip() else ""
    return inner


def _normalize_inline_text(text: str) -> str:
    return WHITESPACE.sub(" ", unescape(text)).strip()


def _normalize_cell_text(text: str) -> str:
    normalized = unescape(text).replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"[ \t]*\n[ \t]*", "<br>", normalized)
    normalized = re.sub(r"(?:<br>\s*)+$", "", normalized)
    normalized = re.sub(r"\s*<br>\s*", "<br>", normalized)
    normalized = WHITESPACE.sub(" ", normalized)
    return normalized.strip()


def _escape_table_cell(text: str) -> str:
    return text.replace("|", "\\|")


__all__ = [
    "TableConversionMode",
    "TableConversionStats",
    "convert_html_tables_in_markdown",
]
