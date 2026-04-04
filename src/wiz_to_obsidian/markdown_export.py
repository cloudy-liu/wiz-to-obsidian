from __future__ import annotations

from dataclasses import dataclass, field
from html import escape as html_escape, unescape
import json
import re
from pathlib import Path
from typing import Mapping, Sequence

from .models import BodyMetadata, GeneratedAsset, NoteForExport


INVALID_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1F]+')
MULTISPACE = re.compile(r"\s+")
TITLE_SUFFIX = re.compile(r"\.(?:md|markdown|html?)$", re.IGNORECASE)


def make_resource_key(doc_guid: str, resource_name: str) -> str:
    return f"wiz-resource://{doc_guid}/{resource_name}"


def make_attachment_key(doc_guid: str, attachment_name: str) -> str:
    return f"wiz-attachment://{doc_guid}/{attachment_name}"


def _sanitize_segment(value: str, *, fallback: str) -> str:
    cleaned = INVALID_PATH_CHARS.sub("-", value.strip())
    cleaned = MULTISPACE.sub(" ", cleaned).strip(" .")
    return cleaned or fallback


def _to_posix(path: Path) -> str:
    return path.as_posix()


class NotePathResolver:
    def __init__(self) -> None:
        self._seen_paths: set[str] = set()

    def note_relative_path(self, note: NoteForExport) -> Path:
        folder = [_sanitize_segment(part, fallback="Folder") for part in note.folder_parts]
        title = TITLE_SUFFIX.sub("", note.title.strip())
        base_name = _sanitize_segment(title, fallback=note.doc_guid)
        candidate = Path(*folder, f"{base_name}.md")
        if _to_posix(candidate) not in self._seen_paths:
            self._seen_paths.add(_to_posix(candidate))
            return candidate

        deduped = Path(*folder, f"{base_name}--{note.doc_guid}.md")
        self._seen_paths.add(_to_posix(deduped))
        return deduped


@dataclass(frozen=True)
class CollaborationRenderResult:
    markdown: str
    generated_assets: tuple[GeneratedAsset, ...] = ()
    metadata: BodyMetadata = field(default_factory=BodyMetadata)


@dataclass
class _CollaborationRenderState:
    doc_guid: str
    generated_assets: dict[str, bytes] = field(default_factory=dict)
    table_count: int = 0
    drawio_count: int = 0
    unsupported_block_types: set[str] = field(default_factory=set)


def _yaml_scalar(value: str | None) -> str:
    if value is None:
        return '""'
    if value == "" or any(char in value for char in [":", "#", "\n", "\r", '"', "'"]):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def render_frontmatter(note: NoteForExport) -> str:
    folder = "/".join(note.folder_parts)
    lines = [
        "---",
        f"title: {_yaml_scalar(note.title)}",
        f"wiz_doc_guid: {_yaml_scalar(note.doc_guid)}",
        f"wiz_kb_guid: {_yaml_scalar(note.kb_guid)}",
        f"wiz_kb_name: {_yaml_scalar(note.kb_name)}",
        f"wiz_note_type: {_yaml_scalar(note.note_type)}",
        f"wiz_folder: {_yaml_scalar(folder)}",
    ]
    if note.created_at is not None:
        lines.append(f"created: {note.created_at.isoformat()}")
    if note.updated_at is not None:
        lines.append(f"updated: {note.updated_at.isoformat()}")
    lines.append("tags:")
    if note.tags:
        lines.extend(f"- {_yaml_scalar(tag)}" for tag in note.tags)
    else:
        lines.append("[]")
    lines.append("---")
    return "\n".join(lines)


def _rewrite_resource_urls(text: str, resource_paths: Mapping[str, Path]) -> str:
    rewritten = text
    for original, target in resource_paths.items():
        rewritten = rewritten.replace(original, _to_posix(target))
    return rewritten


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


def _fallback_html_to_markdown(html: str) -> str:
    text = re.sub(
        r'<img[^>]*src="([^"]+)"[^>]*>',
        lambda match: f'![]({match.group(1)})',
        html,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        lambda match: f'[{_strip_tags(match.group(2)).strip()}]({match.group(1)})',
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(r"</p\s*>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<strong>(.*?)</strong>", r"**\1**", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<em>(.*?)</em>", r"*\1*", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", "", text)
    text = MULTISPACE.sub(" ", text)
    text = re.sub(r" *\n *", "\n", text)
    return text.strip()


def _clean_html_document(html: str) -> str:
    cleaned = re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"<style\b[^>]*>.*?</style>", "", cleaned, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"<!--.*?-->", "", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"<img\b(?P<attrs>[^>]*)>", _normalize_html_image_tag, cleaned, flags=re.IGNORECASE | re.DOTALL)

    body_match = re.search(r"<body\b[^>]*>(.*?)</body>", cleaned, flags=re.IGNORECASE | re.DOTALL)
    if body_match:
        cleaned = body_match.group(1)
    else:
        cleaned = re.sub(r"<head\b[^>]*>.*?</head>", "", cleaned, flags=re.IGNORECASE | re.DOTALL)

    cleaned = re.sub(r"<(?:meta|link)\b[^>]*>", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<title\b[^>]*>.*?</title>", "", cleaned, flags=re.IGNORECASE | re.DOTALL)
    return cleaned.strip()


def _normalize_html_image_tag(match: re.Match[str]) -> str:
    attrs_text = match.group("attrs") or ""

    src = ""
    for key in ("src", "data-src", "data-original", "data-actualsrc", "data-lazy-src"):
        candidate = _html_attribute_value(attrs_text, key)
        if candidate:
            src = candidate
            break
    if not src:
        return ""

    alt = _html_attribute_value(attrs_text, "alt")
    if alt:
        return f'<img src="{src}" alt="{alt}">'
    return f'<img src="{src}">'


def _html_attribute_value(attrs_text: str, name: str) -> str:
    match = re.search(
        rf"""\b{re.escape(name)}\s*=\s*(?:"(?P<double>[^"]*)"|'(?P<single>[^']*)'|(?P<bare>[^\s>]+))""",
        attrs_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return ""
    return next((value.strip() for value in match.groups() if value is not None), "")


def _unwrap_preformatted_markdown(html: str) -> str | None:
    cleaned = _clean_html_document(html)
    match = re.fullmatch(r"\s*<pre\b[^>]*>(?P<content>.*?)</pre>\s*", cleaned, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    return unescape(match.group("content")).strip("\r\n")


def _html_to_markdown(html: str) -> str:
    preformatted_markdown = _unwrap_preformatted_markdown(html)
    if preformatted_markdown is not None:
        return preformatted_markdown

    html = _clean_html_document(html)
    try:
        from markdownify import markdownify as to_markdown
    except ImportError:
        return _fallback_html_to_markdown(html)
    return to_markdown(html, heading_style="ATX").strip()


def _coerce_collaboration_payload(payload: Mapping[str, object] | str | bytes) -> Mapping[str, object]:
    if isinstance(payload, Mapping):
        return payload
    if isinstance(payload, bytes):
        return json.loads(payload.decode("utf-8"))
    return json.loads(payload)


def _collaboration_root(data: Mapping[str, object]) -> Mapping[str, object]:
    blocks = data.get("blocks")
    if isinstance(blocks, Sequence) and not isinstance(blocks, (str, bytes, bytearray)):
        return data
    for value in data.values():
        if not isinstance(value, Mapping):
            continue
        blocks = value.get("blocks")
        if isinstance(blocks, Sequence) and not isinstance(blocks, (str, bytes, bytearray)):
            return value
    return data


def _collaboration_lookup(data: Mapping[str, object]) -> dict[str, object]:
    lookup: dict[str, object] = {}
    for key, value in data.items():
        if isinstance(key, str):
            lookup[key] = value

    blocks = data.get("blocks") or []
    if isinstance(blocks, Sequence) and not isinstance(blocks, (str, bytes, bytearray)):
        for block in blocks:
            if not isinstance(block, Mapping):
                continue
            block_id = str(block.get("id") or "").strip()
            if block_id:
                lookup.setdefault(block_id, block)
    return lookup


def _render_inline_code(value: str) -> str:
    ticks = "``" if "`" in value else "`"
    return f"{ticks}{value}{ticks}"


def _render_inline_text(parts: Sequence[Mapping[str, object]]) -> str:
    fragments: list[str] = []
    for part in parts:
        attributes = part.get("attributes") or {}
        if not isinstance(attributes, Mapping):
            attributes = {}

        if attributes.get("box") and attributes.get("type") == "br":
            fragments.append("\n")
            continue
        if attributes.get("box") and attributes.get("type") == "wiki-link":
            name = str(attributes.get("name") or "").strip()
            if name:
                fragments.append(f"[[{name}]]")
            continue

        text = str(part.get("insert") or "")
        if not text:
            continue
        if attributes.get("style-code"):
            text = _render_inline_code(text)
        if attributes.get("style-bold"):
            text = f"**{text}**"
        if attributes.get("style-italic"):
            text = f"*{text}*"
        if attributes.get("style-strikethrough"):
            text = f"~~{text}~~"
        link = str(attributes.get("link") or "").strip()
        if link:
            text = f"[{text or link}]({link})"
        fragments.append(text)
    return "".join(fragments).strip()


def _render_inline_html(parts: Sequence[Mapping[str, object]]) -> str:
    fragments: list[str] = []
    for part in parts:
        attributes = part.get("attributes") or {}
        if not isinstance(attributes, Mapping):
            attributes = {}

        if attributes.get("box") and attributes.get("type") == "br":
            fragments.append("<br>")
            continue
        if attributes.get("box") and attributes.get("type") == "wiki-link":
            name = str(attributes.get("name") or "").strip()
            if name:
                fragments.append(html_escape(f"[[{name}]]"))
            continue

        text = str(part.get("insert") or "")
        if not text:
            continue
        rendered = html_escape(text)
        if attributes.get("style-code"):
            rendered = f"<code>{rendered}</code>"
        if attributes.get("style-bold"):
            rendered = f"<strong>{rendered}</strong>"
        if attributes.get("style-italic"):
            rendered = f"<em>{rendered}</em>"
        if attributes.get("style-strikethrough"):
            rendered = f"<del>{rendered}</del>"
        link = str(attributes.get("link") or "").strip()
        if link:
            href = html_escape(link)
            rendered = f'<a href="{href}">{rendered or href}</a>'
        fragments.append(rendered)
    return "".join(fragments).strip()


def _quote_lines(text: str) -> str:
    lines = text.splitlines() or [""]
    return "\n".join("> " if not line else f"> {line}" for line in lines)


def _render_text_block(block: Mapping[str, object]) -> str:
    content = _render_inline_text(block.get("text") or [])
    if not content:
        return ""

    heading = block.get("heading")
    if isinstance(heading, (int, float)) and int(heading) > 0:
        level = max(1, min(6, int(heading)))
        content = f"{'#' * level} {content}"

    if block.get("quoted"):
        content = _quote_lines(content)
    return content


def _coerce_int(value: object, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _render_list_block(block: Mapping[str, object]) -> str:
    content = _render_inline_text(block.get("text") or [])
    heading = block.get("heading")
    if isinstance(heading, (int, float)) and int(heading) > 0:
        level = max(1, min(6, int(heading)))
        line = f"{'#' * level} {content}" if content else ""
        if block.get("quoted"):
            line = _quote_lines(line)
        return line

    level = max(1, _coerce_int(block.get("level"), default=1))
    indent = "  " * (level - 1)

    checkbox = str(block.get("checkbox") or "").strip().lower()
    if checkbox:
        marker = "- [x]" if checkbox == "checked" else "- [ ]"
    elif block.get("ordered"):
        marker = f"{_coerce_int(block.get('start'), default=1)}."
    else:
        marker = "-"

    line = f"{indent}{marker}"
    if content:
        line = f"{line} {content}"
    if block.get("quoted"):
        line = _quote_lines(line)
    return line


def _child_lookup_id(entry: object) -> str:
    if isinstance(entry, str):
        return entry.strip()
    if not isinstance(entry, Mapping):
        return ""
    return str(entry.get("id") or entry.get("childId") or entry.get("blockId") or "").strip()


def _render_code_line_text(parts: Sequence[Mapping[str, object]]) -> str:
    fragments: list[str] = []
    for part in parts:
        if isinstance(part, Mapping):
            fragments.append(str(part.get("insert") or ""))
    return "".join(fragments)


def _code_text_from_children(block: Mapping[str, object], *, lookup: Mapping[str, object]) -> str:
    children = block.get("children") or []
    if not isinstance(children, Sequence) or isinstance(children, (str, bytes, bytearray)):
        return ""

    lines: list[str] = []
    for child in children:
        child_value: object = child
        child_id = _child_lookup_id(child)
        if child_id:
            child_value = lookup.get(child_id)

        for line_block in _coerce_block_sequence(child_value):
            if str(line_block.get("type") or "").strip().lower() == "code-line":
                lines.append(_render_code_line_text(line_block.get("text") or []))
            else:
                lines.append(_render_inline_text(line_block.get("text") or []))
    return "\n".join(lines).rstrip()


def _code_block_text(block: Mapping[str, object], *, lookup: Mapping[str, object]) -> str:
    code = str(block.get("code") or "").rstrip()
    if code:
        return code

    code = _render_inline_text(block.get("text") or [])
    if code:
        return code

    return _code_text_from_children(block, lookup=lookup)


def _render_code_block(block: Mapping[str, object], *, lookup: Mapping[str, object]) -> str:
    language = str(block.get("language") or "").strip().lower()
    code = _code_block_text(block, lookup=lookup)
    if not code:
        return ""
    return f"```{language}\n{code}\n```"


def _coerce_block_sequence(value: object) -> list[Mapping[str, object]]:
    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [item for item in value if isinstance(item, Mapping)]
    return []


def _cell_span(value: object) -> int:
    return max(1, _coerce_int(value, default=1))


def _register_generated_asset(state: _CollaborationRenderState, *, name: str, text: str) -> str:
    key = make_resource_key(state.doc_guid, name)
    state.generated_assets.setdefault(key, text.encode("utf-8"))
    return key


def _drawio_base_name(block: Mapping[str, object], src: str) -> str:
    if src:
        path = Path(src)
        if path.stem:
            return path.stem
        if path.name:
            return path.name
    return _sanitize_segment(str(block.get("id") or ""), fallback="diagram")


def _normalize_table_rows(block: Mapping[str, object]) -> list[list[str]]:
    for key in ("cells", "tableData", "rowsData"):
        rows = block.get(key)
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
            continue
        normalized_rows: list[list[str]] = []
        for row in rows:
            if not isinstance(row, Sequence) or isinstance(row, (str, bytes, bytearray)):
                continue
            normalized_row: list[str] = []
            for cell in row:
                if isinstance(cell, Mapping):
                    normalized_row.append(_render_inline_text(cell.get("text") or []))
                else:
                    normalized_row.append(str(cell))
            normalized_rows.append(normalized_row)
        if normalized_rows:
            return normalized_rows
    return []


def _table_child_spec(entry: object) -> tuple[str, int, int] | None:
    if isinstance(entry, str):
        text = entry.strip()
        if text:
            return text, 1, 1
        return None
    if not isinstance(entry, Mapping):
        return None

    cell_id = str(
        entry.get("id") or entry.get("cellId") or entry.get("childId") or entry.get("blockId") or ""
    ).strip()
    if not cell_id:
        return None
    return (
        cell_id,
        _cell_span(entry.get("rowspan") or entry.get("rowSpan")),
        _cell_span(entry.get("colspan") or entry.get("colSpan")),
    )


def _markdown_table_cell(text: str) -> str:
    return text.replace("|", r"\|").replace("\n", "<br>")


def _split_embed_sources(src: str) -> list[str]:
    return [part.strip() for part in src.split("|") if part.strip()]


def _embed_block_html(block: Mapping[str, object], *, state: _CollaborationRenderState) -> str:
    embed_type = str(block.get("embedType") or "").strip().lower()
    embed_data = block.get("embedData") or {}
    if not isinstance(embed_data, Mapping):
        embed_data = {}

    src = str(embed_data.get("src") or "").strip()
    if embed_type in {"image", "plantuml"} and src:
        sources = _split_embed_sources(src)
        return "<br>".join(
            f'<img src="{html_escape(make_resource_key(state.doc_guid, item))}" alt="">' for item in sources
        )
    if embed_type == "drawio" and src:
        return f'<img src="{html_escape(make_resource_key(state.doc_guid, src))}" alt="">'
    if embed_type == "office" and src:
        label = str(embed_data.get("fileName") or Path(src).name)
        return f'<a href="{html_escape(make_resource_key(state.doc_guid, src))}">{html_escape(label)}</a>'
    if embed_type == "hr":
        return "<hr>"
    if embed_type == "encrypt-text":
        prompt = str(embed_data.get("prompt") or "password protected")
        return f"<blockquote>Encrypted content omitted: {html_escape(prompt)}</blockquote>"
    if src:
        href = html_escape(make_resource_key(state.doc_guid, src))
        return f'<a href="{href}">Embedded {html_escape(embed_type or "file")}</a>'
    return ""


def _render_embed_block(block: Mapping[str, object], *, state: _CollaborationRenderState) -> str:
    embed_type = str(block.get("embedType") or "").strip().lower()
    embed_data = block.get("embedData") or {}
    if not isinstance(embed_data, Mapping):
        embed_data = {}

    src = str(embed_data.get("src") or "").strip()
    if embed_type in {"image", "plantuml"} and src:
        return "\n\n".join(f"![]({make_resource_key(state.doc_guid, item)})" for item in _split_embed_sources(src))
    if embed_type == "drawio":
        state.drawio_count += 1
        preview_src = src
        base_name = _drawio_base_name(block, src)
        xml = str(embed_data.get("xml") or "")
        xml_svg = str(embed_data.get("xmlSvg") or "")

        if not preview_src and xml_svg.strip():
            preview_src = f"{base_name}.drawio.svg"
            _register_generated_asset(state, name=preview_src, text=xml_svg)

        lines: list[str] = []
        if preview_src:
            lines.append(f"![]({make_resource_key(state.doc_guid, preview_src)})")
        if xml.strip():
            source_key = _register_generated_asset(state, name=f"{base_name}.drawio", text=xml)
            lines.append(f"[Drawio source]({source_key})")
        if xml_svg.strip():
            svg_key = _register_generated_asset(state, name=f"{base_name}.drawio.svg", text=xml_svg)
            if preview_src != f"{base_name}.drawio.svg":
                lines.append(f"[Drawio SVG]({svg_key})")
        return "\n".join(line for line in lines if line).strip()
    if embed_type == "office" and src:
        label = str(embed_data.get("fileName") or Path(src).name)
        return f"[{label}]({make_resource_key(state.doc_guid, src)})"
    if embed_type == "hr":
        return "---"
    if embed_type == "encrypt-text":
        prompt = str(embed_data.get("prompt") or "password protected")
        return f"> [Encrypted content omitted: {prompt}]"
    if src:
        return f"[Embedded {embed_type or 'file'}]({make_resource_key(state.doc_guid, src)})"
    return ""


def _table_cell_fragments(
    cell_blocks: Sequence[Mapping[str, object]],
    *,
    state: _CollaborationRenderState,
) -> tuple[str, str, bool]:
    markdown_fragments: list[str] = []
    html_fragments: list[str] = []
    simple_markdown = True

    for block in cell_blocks:
        block_type = str(block.get("type") or "").strip().lower()
        if block_type == "text":
            markdown = _render_inline_text(block.get("text") or [])
            html = _render_inline_html(block.get("text") or [])
            if block.get("heading") or block.get("quoted"):
                simple_markdown = False
            if markdown:
                markdown_fragments.append(markdown)
            if html:
                html_fragments.append(html)
            continue

        simple_markdown = False
        if block_type == "list":
            markdown = _render_list_block(block)
            html = html_escape(markdown)
        elif block_type == "code":
            language = str(block.get("language") or "").strip().lower()
            code = str(block.get("code") or "").rstrip() or _render_inline_text(block.get("text") or [])
            markdown = _render_inline_code(code) if code else ""
            class_attr = f' class="language-{html_escape(language)}"' if language else ""
            html = f"<pre><code{class_attr}>{html_escape(code)}</code></pre>" if code else ""
        elif block_type == "embed":
            markdown = _render_embed_block(block, state=state)
            html = _embed_block_html(block, state=state)
        else:
            state.unsupported_block_types.add(block_type or "<empty>")
            markdown = ""
            html = ""

        if markdown:
            markdown_fragments.append(markdown)
        if html:
            html_fragments.append(html)

    markdown_text = " ".join(fragment.strip() for fragment in markdown_fragments if fragment.strip()).strip()
    html_text = "<br>".join(fragment for fragment in html_fragments if fragment).strip()
    if not markdown_text and html_text:
        markdown_text = MULTISPACE.sub(" ", unescape(re.sub(r"<[^>]+>", " ", html_text))).strip()
    if "\n" in markdown_text:
        simple_markdown = False
    return markdown_text, html_text, simple_markdown and bool(markdown_text)


def _html_table_from_rows(
    rows: list[list[tuple[str, int, int, str]]],
    *,
    header_rows: int,
) -> str:
    lines = ["<table>"]
    for row_index, row in enumerate(rows):
        lines.append("<tr>")
        tag = "th" if row_index < header_rows else "td"
        for _, row_span, col_span, html in row:
            attrs: list[str] = []
            if row_span > 1:
                attrs.append(f' rowspan="{row_span}"')
            if col_span > 1:
                attrs.append(f' colspan="{col_span}"')
            lines.append(f"<{tag}{''.join(attrs)}>{html or ''}</{tag}>")
        lines.append("</tr>")
    lines.append("</table>")
    return "\n".join(lines)


def _table_rows_from_children(
    block: Mapping[str, object],
    *,
    lookup: Mapping[str, object],
    state: _CollaborationRenderState,
) -> tuple[list[list[tuple[str, int, int, str]]], bool]:
    row_count = _coerce_int(block.get("rows"), default=0)
    col_count = _coerce_int(block.get("cols"), default=0)
    children = block.get("children") or []
    if row_count <= 0 or col_count <= 0:
        return [], False
    if not isinstance(children, Sequence) or isinstance(children, (str, bytes, bytearray)):
        return [], False

    occupied = [[False] * col_count for _ in range(row_count)]
    rendered_rows: list[list[tuple[str, int, int, str]]] = [[] for _ in range(row_count)]
    complex_table = False

    for child in children:
        spec = _table_child_spec(child)
        if spec is None:
            continue
        cell_id, row_span, col_span = spec
        cell_value = lookup.get(cell_id)
        cell_mapping = cell_value if isinstance(cell_value, Mapping) else {}
        row_span = max(row_span, _cell_span(cell_mapping.get("rowspan") or cell_mapping.get("rowSpan")))
        col_span = max(col_span, _cell_span(cell_mapping.get("colspan") or cell_mapping.get("colSpan")))

        cell_blocks = _coerce_block_sequence(cell_value)
        markdown_text, html_text, simple_markdown = _table_cell_fragments(cell_blocks, state=state)
        if row_span > 1 or col_span > 1 or not simple_markdown:
            complex_table = True

        slot: tuple[int, int] | None = None
        for row_index in range(row_count):
            for col_index in range(col_count):
                if not occupied[row_index][col_index]:
                    slot = (row_index, col_index)
                    break
            if slot is not None:
                break
        if slot is None:
            break

        row_index, col_index = slot
        for fill_row in range(row_index, min(row_count, row_index + row_span)):
            for fill_col in range(col_index, min(col_count, col_index + col_span)):
                occupied[fill_row][fill_col] = True
        rendered_rows[row_index].append((markdown_text, row_span, col_span, html_text or html_escape(markdown_text)))

    normalized_rows = [row for row in rendered_rows if row]
    return normalized_rows, complex_table and bool(normalized_rows)


def _render_markdown_table(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    header = rows[0]
    divider = ["---"] * len(header)
    body_rows = rows[1:]
    rendered_rows = ["| " + " | ".join(header) + " |", "| " + " | ".join(divider) + " |"]
    rendered_rows.extend("| " + " | ".join(row) + " |" for row in body_rows)
    return "\n".join(rendered_rows)


def _render_table_block(
    block: Mapping[str, object],
    *,
    lookup: Mapping[str, object],
    state: _CollaborationRenderState,
) -> str:
    state.table_count += 1

    direct_rows = _normalize_table_rows(block)
    if direct_rows:
        return _render_markdown_table([[_markdown_table_cell(cell) for cell in row] for row in direct_rows])

    child_rows, complex_table = _table_rows_from_children(block, lookup=lookup, state=state)
    if child_rows:
        header_rows = _coerce_int(
            block.get("headerRows") or block.get("headerRowCount") or block.get("headRows"),
            default=1 if len(child_rows) > 1 else 0,
        )
        header_rows = max(0, min(len(child_rows), header_rows or (1 if len(child_rows) > 1 else 0)))
        if complex_table:
            return _html_table_from_rows(child_rows, header_rows=header_rows or 1)
        markdown_rows = [[_markdown_table_cell(cell[0]) for cell in row] for row in child_rows]
        return _render_markdown_table(markdown_rows)

    row_count = _coerce_int(block.get("rows"), default=0)
    col_count = _coerce_int(block.get("cols"), default=0)
    if row_count or col_count:
        return f"_Table omitted ({row_count} x {col_count})_"
    return ""


def _collect_block_source_text(
    block: Mapping[str, object],
    *,
    lookup: Mapping[str, object],
    state: _CollaborationRenderState,
) -> str:
    block_type = str(block.get("type") or "").strip().lower()
    if block_type in {"text", "list"}:
        return _render_inline_text(block.get("text") or [])
    if block_type == "code":
        return _code_block_text(block, lookup=lookup)
    if block_type == "table":
        fragments: list[str] = []
        for child in block.get("children") or []:
            spec = _table_child_spec(child)
            if spec is None:
                continue
            cell_value = lookup.get(spec[0])
            cell_blocks = _coerce_block_sequence(cell_value)
            cell_text, _, _ = _table_cell_fragments(cell_blocks, state=state)
            if cell_text:
                fragments.append(cell_text)
        return "\n".join(fragments)
    return ""


def _render_collaboration_block(
    block: Mapping[str, object],
    *,
    lookup: Mapping[str, object],
    state: _CollaborationRenderState,
) -> str:
    block_type = str(block.get("type") or "").strip().lower()
    if block_type == "text":
        return _render_text_block(block)
    if block_type == "list":
        return _render_list_block(block)
    if block_type == "code":
        return _render_code_block(block, lookup=lookup)
    if block_type == "table":
        return _render_table_block(block, lookup=lookup, state=state)
    if block_type == "embed":
        return _render_embed_block(block, state=state)
    if block_type:
        state.unsupported_block_types.add(block_type)
    return ""


def render_collaboration_payload(
    payload: Mapping[str, object] | str | bytes,
    *,
    doc_guid: str,
) -> CollaborationRenderResult:
    raw_data = _coerce_collaboration_payload(payload)
    data = _collaboration_root(raw_data)
    blocks = data.get("blocks") or []
    if not isinstance(blocks, Sequence) or isinstance(blocks, (str, bytes, bytearray)):
        return CollaborationRenderResult(markdown="")

    lookup = _collaboration_lookup(data)
    state = _CollaborationRenderState(doc_guid=doc_guid)

    rendered_blocks: list[str] = []
    source_fragments: list[str] = []
    previous_type = ""
    for raw_block in blocks:
        if not isinstance(raw_block, Mapping):
            continue

        block_type = str(raw_block.get("type") or "").strip().lower()
        rendered = _render_collaboration_block(raw_block, lookup=lookup, state=state)
        source_text = _collect_block_source_text(raw_block, lookup=lookup, state=state)
        if source_text:
            source_fragments.append(source_text)

        if not rendered:
            continue
        if rendered_blocks:
            separator = "\n" if previous_type == block_type == "list" else "\n\n"
            rendered_blocks.append(separator)
        rendered_blocks.append(rendered)
        previous_type = block_type

    metadata = BodyMetadata(
        source_text_length=len("\n".join(fragment for fragment in source_fragments if fragment).strip()),
        collaboration_table_count=state.table_count,
        collaboration_drawio_count=state.drawio_count,
        unsupported_block_types=tuple(sorted(state.unsupported_block_types)),
    )
    generated_assets = tuple(
        GeneratedAsset(key=key, payload=payload_bytes)
        for key, payload_bytes in state.generated_assets.items()
    )
    return CollaborationRenderResult(
        markdown="".join(rendered_blocks).strip(),
        generated_assets=generated_assets,
        metadata=metadata,
    )


def render_collaboration_document(
    payload: Mapping[str, object] | str | bytes,
    *,
    doc_guid: str,
) -> str:
    return render_collaboration_payload(payload, doc_guid=doc_guid).markdown


def render_note_markdown(note: NoteForExport, resource_paths: Mapping[str, Path]) -> str:
    if note.body_markdown:
        body = _rewrite_resource_urls(note.body_markdown, resource_paths).strip()
    else:
        rewritten_html = _rewrite_resource_urls(note.body_html or "", resource_paths)
        body = _html_to_markdown(rewritten_html)

    frontmatter = render_frontmatter(note)
    if body:
        return f"{frontmatter}\n\n{body}\n"
    return f"{frontmatter}\n"


__all__ = [
    "NoteForExport",
    "NotePathResolver",
    "make_attachment_key",
    "make_resource_key",
    "render_collaboration_document",
    "render_collaboration_payload",
    "render_frontmatter",
    "render_note_markdown",
]
