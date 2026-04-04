from __future__ import annotations

import json
import re
from html import unescape
from pathlib import Path
from typing import Mapping

from .markdown_export import _clean_html_document
from .models import Inventory, WizNote


OMITTED_TABLE = re.compile(r"_Table omitted\b")
LEGACY_ASSET_PATH = re.compile(r"(?:index_files/[^\s)\"'>]+|[^\s/]+_files/[^\s)\"'>]+)")
RAW_WIZ_REFERENCE = re.compile(r"wiz-(?:resource|attachment)://[^\s)\"'>]+")
FRONTMATTER = re.compile(r"\A---\n.*?\n---\n?", re.DOTALL)


def _strip_frontmatter(markdown: str) -> str:
    return FRONTMATTER.sub("", markdown, count=1)


def _note_entry(note: WizNote, *, path: Path, output_dir: Path, issues: list[dict]) -> dict:
    return {
        "doc_guid": note.doc_guid,
        "title": note.title,
        "path": path.relative_to(output_dir).as_posix(),
        "issues": issues,
    }


def _drawio_asset_count(note: WizNote) -> int:
    return sum(1 for asset in note.body.generated_assets if ".drawio" in asset.key)


def _visible_html_text_length(html: str) -> int:
    cleaned = _clean_html_document(html)
    text = re.sub(r"<[^>]+>", " ", cleaned)
    text = re.sub(r"\s+", " ", unescape(text)).strip()
    return len(text)


def _source_text_length(note: WizNote) -> int:
    if note.body.metadata.source_text_length:
        return note.body.metadata.source_text_length
    if note.body.markdown:
        return len(note.body.markdown.strip())
    if note.body.html:
        return _visible_html_text_length(note.body.html)
    return 0


def _content_audit_markdown(payload: dict) -> str:
    lines = [
        "# Content Audit",
        "",
        f"- notes_scanned: {payload['summary']['notes_scanned']}",
        f"- auto_fixed_count: {payload['summary']['auto_fixed_count']}",
        f"- unresolved_count: {payload['summary']['unresolved_count']}",
        f"- manual_review_count: {payload['summary']['manual_review_count']}",
        "",
    ]

    for section_name, title in (
        ("auto_fixed", "Auto Fixed"),
        ("unresolved", "Unresolved"),
        ("manual_review_candidates", "Manual Review Candidates"),
    ):
        lines.append(f"## {title}")
        entries = payload[section_name]
        if not entries:
            lines.append("- none")
            lines.append("")
            continue
        for entry in entries:
            issue_codes = ", ".join(issue["code"] for issue in entry["issues"])
            lines.append(f"- {entry['title']} ({entry['doc_guid']}) [{entry['path']}] :: {issue_codes}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_content_audit(
    *,
    inventory: Inventory,
    output_dir: Path,
    note_paths_by_doc_guid: Mapping[str, Path],
    missing_resources_by_doc_guid: Mapping[str, tuple[str, ...] | list[str] | set[str]],
) -> dict:
    auto_fixed: list[dict] = []
    unresolved: list[dict] = []
    manual_review_candidates: list[dict] = []

    for note in inventory.notes:
        note_path = note_paths_by_doc_guid.get(note.doc_guid)
        if note_path is None or not note_path.exists():
            continue

        note_text = note_path.read_text(encoding="utf-8")
        body_text = _strip_frontmatter(note_text).strip()
        auto_issues: list[dict] = []
        unresolved_issues: list[dict] = []
        manual_issues: list[dict] = []

        missing_targets = sorted({str(target) for target in missing_resources_by_doc_guid.get(note.doc_guid, ()) if target})
        if missing_targets:
            auto_issues.append({"code": "rewrote_missing_asset_links", "count": len(missing_targets)})
            unresolved_issues.append({"code": "missing_assets", "targets": missing_targets})

        omitted_tables = len(OMITTED_TABLE.findall(body_text))
        if omitted_tables:
            unresolved_issues.append({"code": "omitted_tables", "count": omitted_tables})
        elif note.body.metadata.collaboration_table_count:
            auto_issues.append(
                {
                    "code": "restored_collaboration_tables",
                    "count": note.body.metadata.collaboration_table_count,
                }
            )

        legacy_asset_paths = sorted(set(LEGACY_ASSET_PATH.findall(body_text)))
        if legacy_asset_paths:
            unresolved_issues.append({"code": "legacy_asset_references", "targets": legacy_asset_paths})

        raw_wiz_references = sorted(set(RAW_WIZ_REFERENCE.findall(body_text)))
        if raw_wiz_references:
            unresolved_issues.append({"code": "raw_wiz_references", "targets": raw_wiz_references})

        drawio_assets = _drawio_asset_count(note)
        if note.body.metadata.collaboration_drawio_count and drawio_assets:
            auto_issues.append({"code": "preserved_drawio_sidecars", "count": drawio_assets})

        unsupported_block_types = list(note.body.metadata.unsupported_block_types)
        if unsupported_block_types:
            manual_issues.append({"code": "unsupported_blocks", "types": unsupported_block_types})

        source_length = _source_text_length(note)
        exported_length = len(body_text)
        if source_length >= 500 and exported_length < source_length * 0.6 and (source_length - exported_length) >= 300:
            manual_issues.append(
                {
                    "code": "length_gap",
                    "source_length": source_length,
                    "exported_length": exported_length,
                }
            )

        if not note.body.has_meaningful_content:
            unresolved_issues.append({"code": "missing_body"})

        if auto_issues:
            auto_fixed.append(_note_entry(note, path=note_path, output_dir=output_dir, issues=auto_issues))
        if unresolved_issues:
            unresolved.append(_note_entry(note, path=note_path, output_dir=output_dir, issues=unresolved_issues))
        if manual_issues:
            manual_review_candidates.append(
                _note_entry(note, path=note_path, output_dir=output_dir, issues=manual_issues)
            )

    payload = {
        "summary": {
            "notes_scanned": len(note_paths_by_doc_guid),
            "auto_fixed_count": len(auto_fixed),
            "unresolved_count": len(unresolved),
            "manual_review_count": len(manual_review_candidates),
        },
        "auto_fixed": auto_fixed,
        "unresolved": unresolved,
        "manual_review_candidates": manual_review_candidates,
    }

    audit_dir = output_dir / "_wiz"
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / "content_audit.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (audit_dir / "content_audit.md").write_text(_content_audit_markdown(payload), encoding="utf-8")
    return payload


__all__ = ["write_content_audit"]
