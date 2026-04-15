from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .content_audit import write_content_audit
from .markdown_export import (
    _clean_html_document,
    NotePathResolver,
    NoteForExport,
    make_attachment_key,
    render_frontmatter,
    render_note_markdown,
)
from .models import Inventory, SyncState, SyncStateNote, WizNote
from .reporting import build_export_report


INVALID_FILE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1F]+')
ASSET_KEY = re.compile(r"^wiz-(resource|attachment)://(?:(?P<doc_guid>[^/]+)/)?(?P<name>.+)$")
HTML_ASSET = re.compile(r"""(?:src|href)\s*=\s*["'](?P<value>[^"']+)["']""", re.IGNORECASE)
MARKDOWN_TARGET = re.compile(r"!\[[^\]]*\]\((?P<target>[^)\r\n]+)\)|\[[^\]]*\]\((?P<link_target>[^)\r\n]+)\)")
HTML_IMAGE_TAG = re.compile(r"""<img\b[^>]*src\s*=\s*["'](?P<target>[^"']+)["'][^>]*>""", re.IGNORECASE | re.DOTALL)
HTML_LINK_TAG = re.compile(
    r"""<a\b[^>]*href\s*=\s*["'](?P<target>[^"']+)["'][^>]*>.*?</a>""",
    re.IGNORECASE | re.DOTALL,
)
FRONTMATTER_BLOCK = re.compile(r"\A---\s*\r?\n(?P<body>.*?)(?:\r?\n)---(?:\r?\n|\Z)", re.DOTALL)
FRONTMATTER_FIELD = re.compile(r"^(?P<key>[A-Za-z0-9_]+):\s*(?P<value>.*)$")


@dataclass(frozen=True)
class ExportResult:
    output_dir: Path
    report_path: Path
    report: dict
    sync_state: SyncState | None = None


def _sanitize_asset_name(name: str) -> str:
    cleaned = INVALID_FILE_CHARS.sub("-", name.strip()).strip(" .")
    return cleaned or "asset.bin"


def _to_posix(path: Path) -> str:
    return path.as_posix()


def _parse_asset_key(key: str) -> tuple[str, str, str] | None:
    match = ASSET_KEY.match(key)
    if not match:
        return None
    return match.group(1), match.group("doc_guid") or "", match.group("name")


def _extract_asset_keys(*texts: str | None) -> set[str]:
    keys: set[str] = set()
    for text in texts:
        if not text:
            continue
        keys.update(re.findall(r"wiz-(?:resource|attachment)://[^\s)\]\"'>]+", text))
    return keys


def _normalize_markdown_target(value: str) -> str:
    target = value.strip()
    if not target:
        return ""
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1].strip()
    if not target:
        return ""
    target = target.split(None, 1)[0]
    target = re.sub(r"\\([_()\\])", r"\1", target)
    return target.replace("\\", "/")


def _is_legacy_asset_path(value: str) -> bool:
    return value.startswith("index_files/") or bool(re.match(r"[^/\s]+_files/", value))


def _iter_body_targets(*texts: str | None) -> Iterable[str]:
    seen: set[str] = set()
    for text in texts:
        if not text:
            continue
        for match in HTML_ASSET.finditer(text):
            value = match.group("value").strip().replace("\\", "/")
            if value and value not in seen:
                seen.add(value)
                yield value
        for match in MARKDOWN_TARGET.finditer(text):
            raw_value = match.group("target") or match.group("link_target") or ""
            value = _normalize_markdown_target(raw_value)
            if value and value not in seen:
                seen.add(value)
                yield value


def _relative_link(from_dir: Path, target: Path) -> Path:
    return Path(os.path.relpath(target, from_dir))


def _iter_note_asset_aliases(note: WizNote, inventory: Inventory) -> Iterable[tuple[str, str]]:
    for key in _resource_payloads_for_note(note, inventory):
        parsed = _parse_asset_key(key)
        if parsed is None:
            continue
        _, doc_guid, name = parsed
        if doc_guid == note.doc_guid:
            yield key, key
            yield f"wiz-resource://{name}", key
            yield name, key
            yield f"index_files/{name}", key
            yield f"{note.doc_guid}_files/{name}", key

    for attachment in note.attachments:
        key = _resolve_attachment_inventory_key(note, attachment, inventory)
        yield key, key
        if attachment.att_guid:
            yield make_attachment_key(note.doc_guid, attachment.att_guid), key
        if attachment.name:
            yield make_attachment_key(note.doc_guid, attachment.name), key
            yield f"wiz-attachment://{attachment.name}", key
            yield attachment.name, key
            yield f"index_files/{attachment.name}", key
            yield f"{note.doc_guid}_files/{attachment.name}", key


def _attachment_candidate_keys(note: WizNote, attachment) -> list[str]:
    keys: list[str] = []
    if getattr(attachment, "att_guid", ""):
        keys.append(make_attachment_key(note.doc_guid, attachment.att_guid))
    if getattr(attachment, "name", ""):
        name_key = make_attachment_key(note.doc_guid, attachment.name)
        if name_key not in keys:
            keys.append(name_key)
    return keys


def _resolve_attachment_inventory_key(note: WizNote, attachment, inventory: Inventory) -> str:
    for candidate_key in _attachment_candidate_keys(note, attachment):
        if candidate_key in inventory.attachment_bytes_by_key:
            return candidate_key
    candidates = _attachment_candidate_keys(note, attachment)
    return candidates[0] if candidates else make_attachment_key(note.doc_guid, attachment.name)


def _candidate_inventory_keys(referenced_key: str, alias_lookup: dict[str, str]) -> list[str]:
    candidates: list[str] = []
    direct = alias_lookup.get(referenced_key)
    if direct:
        candidates.append(direct)

    parsed = _parse_asset_key(referenced_key)
    if parsed is None:
        return candidates

    asset_kind, doc_guid, asset_name = parsed
    if "|" not in asset_name:
        if referenced_key not in candidates:
            candidates.append(referenced_key)
        return candidates

    for option in asset_name.split("|"):
        option = option.strip()
        if not option:
            continue
        scoped_key = f"wiz-{asset_kind}://{doc_guid}/{option}" if doc_guid else f"wiz-{asset_kind}://{option}"
        mapped = alias_lookup.get(scoped_key, scoped_key)
        if mapped not in candidates:
            candidates.append(mapped)
    return candidates


def _discover_note_references(note: WizNote, alias_lookup: dict[str, str]) -> set[str]:
    if note.body.is_placeholder:
        return set()

    cleaned_html = _clean_html_document(note.body.html or "") if note.body.html else ""

    referenced_keys = _extract_asset_keys(note.body.markdown, cleaned_html or note.body.html)
    for target in _iter_body_targets(note.body.markdown, cleaned_html):
        if target in alias_lookup or _is_legacy_asset_path(target):
            referenced_keys.add(target)

    body_text = "\n".join(part for part in (note.body.markdown, cleaned_html) if part)
    for alias in alias_lookup:
        if alias.startswith("wiz-"):
            continue
        if not alias:
            continue
        if "/" in alias:
            if alias in body_text:
                referenced_keys.add(alias)
            continue
        if f'"{alias}"' in body_text or f"'{alias}'" in body_text:
            referenced_keys.add(alias)
    return referenced_keys


def _resource_payloads_for_note(note: WizNote, inventory: Inventory) -> dict[str, bytes]:
    payloads = dict(inventory.resource_bytes_by_key)
    for generated_asset in note.body.generated_assets:
        payloads[generated_asset.key] = generated_asset.payload
    return payloads


def _write_binary(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() == payload:
        return
    path.write_bytes(payload)


def _append_attachment_section(markdown: str, attachment_lines: list[str]) -> str:
    if not attachment_lines:
        return markdown
    return f"{markdown.rstrip()}\n\n## Attachments\n" + "\n".join(attachment_lines) + "\n"


def _parse_frontmatter_fields(text: str) -> dict[str, str]:
    match = FRONTMATTER_BLOCK.match(text)
    if not match:
        return {}

    fields: dict[str, str] = {}
    for line in match.group("body").splitlines():
        field_match = FRONTMATTER_FIELD.match(line.strip())
        if field_match:
            value = field_match.group("value").strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            fields[field_match.group("key")] = value
    return fields


def _strip_frontmatter(text: str) -> str:
    match = FRONTMATTER_BLOCK.match(text)
    if not match:
        return text.strip()
    return text[match.end() :].strip()


def _looks_like_relative_link(target: str) -> bool:
    normalized = target.strip()
    if not normalized or normalized.startswith(("#", "/", "mailto:", "data:")):
        return False
    return "://" not in normalized


def _rebase_relative_link_target(
    target: str,
    *,
    source_path: Path,
    target_path: Path,
    output_dir: Path,
) -> str:
    normalized = target.strip()
    if not _looks_like_relative_link(normalized):
        return target

    source_root = output_dir.resolve(strict=False)
    resolved_target = (source_path.parent / Path(normalized)).resolve(strict=False)
    try:
        resolved_target.relative_to(source_root)
    except ValueError:
        return target

    rebased = Path(os.path.relpath(resolved_target, target_path.parent))
    return _to_posix(rebased)


def _rebase_preserved_body_links(
    body: str,
    *,
    source_path: Path,
    target_path: Path,
    output_dir: Path,
) -> str:
    if source_path == target_path:
        return body

    def replace_markdown_target(match: re.Match[str]) -> str:
        raw_target = match.group("target") or match.group("link_target") or ""
        rebased_target = _rebase_relative_link_target(
            raw_target,
            source_path=source_path,
            target_path=target_path,
            output_dir=output_dir,
        )
        if not raw_target or rebased_target == raw_target:
            return match.group(0)
        return match.group(0).replace(raw_target, rebased_target, 1)

    def replace_html_target(match: re.Match[str]) -> str:
        raw_target = match.group("value").strip()
        rebased_target = _rebase_relative_link_target(
            raw_target,
            source_path=source_path,
            target_path=target_path,
            output_dir=output_dir,
        )
        if not raw_target or rebased_target == raw_target:
            return match.group(0)
        return match.group(0).replace(raw_target, rebased_target, 1)

    rebased = MARKDOWN_TARGET.sub(replace_markdown_target, body)
    rebased = HTML_ASSET.sub(replace_html_target, rebased)
    return rebased


def _preserved_body_from_existing_note(
    *,
    source_path: Path,
    target_path: Path,
    output_dir: Path,
    doc_guid: str,
) -> str | None:
    if not source_path.exists():
        return None
    try:
        text = source_path.read_text(encoding="utf-8")
    except OSError:
        return None

    if _parse_frontmatter_fields(text).get("wiz_doc_guid") != doc_guid:
        return None

    body = _strip_frontmatter(text)
    if not body:
        return None
    return _rebase_preserved_body_links(
        body,
        source_path=source_path,
        target_path=target_path,
        output_dir=output_dir,
    )


def _missing_asset_callout(target: str) -> str:
    return "\n" + "\n".join(
        [
            "> [!warning] Missing migrated asset",
            f"> original: {target}",
        ]
    ) + "\n"


def _replace_missing_assets(markdown: str, missing_targets: set[str]) -> str:
    if not missing_targets:
        return markdown

    def replace_markdown_target(match: re.Match[str]) -> str:
        raw_target = match.group("target") or match.group("link_target") or ""
        normalized = _normalize_markdown_target(raw_target)
        if normalized not in missing_targets:
            return match.group(0)
        return _missing_asset_callout(normalized)

    def replace_html_tag(match: re.Match[str]) -> str:
        normalized = _normalize_markdown_target(match.group("target") or "")
        if normalized not in missing_targets:
            return match.group(0)
        return _missing_asset_callout(normalized)

    updated = MARKDOWN_TARGET.sub(replace_markdown_target, markdown)
    updated = HTML_IMAGE_TAG.sub(replace_html_tag, updated)
    updated = HTML_LINK_TAG.sub(replace_html_tag, updated)
    return updated


def export_inventory(
    *,
    inventory: Inventory,
    output_dir: Path,
    limit: int | None = None,
    note_relative_paths_by_doc_guid: dict[str, Path] | None = None,
    existing_note_paths_by_doc_guid: dict[str, Path] | None = None,
    write_report: bool = True,
    write_content_audit_files: bool = True,
    progress: Callable[[str], None] | None = None,
) -> ExportResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    resolver = NotePathResolver()
    notes = inventory.notes[:limit] if limit is not None else inventory.notes

    exported_notes = 0
    exported_resources: set[Path] = set()
    exported_attachments: set[Path] = set()
    missing_bodies: set[str] = set()
    missing_resources: set[str] = set()
    note_paths_by_doc_guid: dict[str, Path] = {}
    note_markdowns_by_doc_guid: dict[str, str] = {}
    missing_resources_by_doc_guid: dict[str, tuple[str, ...]] = {}
    sync_state_notes_by_doc_guid: dict[str, SyncStateNote] = {}

    total_notes = len(notes)
    for index, note in enumerate(notes, start=1):
        note_for_export = NoteForExport.from_wiz_note(note)
        explicit_note_path = None
        if note_relative_paths_by_doc_guid is not None:
            explicit_note_path = note_relative_paths_by_doc_guid.get(note.doc_guid)
        note_relative_path = explicit_note_path or resolver.note_relative_path(note_for_export)
        if progress is not None:
            progress(f"{index}/{total_notes} {note_relative_path.as_posix()}")
        note_path = output_dir / note_relative_path
        note_path.parent.mkdir(parents=True, exist_ok=True)

        resource_payloads = _resource_payloads_for_note(note, inventory)
        alias_lookup = dict(_iter_note_asset_aliases(note, inventory))
        referenced_keys = _discover_note_references(note, alias_lookup)
        resource_paths: dict[str, Path] = {}
        note_missing_resources: set[str] = set()
        needs_plain_name_alias = bool(
            note.body.has_meaningful_content and note.body.html and not note.body.markdown
        )

        for referenced_key in referenced_keys:
            payload = None
            asset_path = None
            inventory_key = referenced_key
            resolved_kind = ""
            for candidate_key in _candidate_inventory_keys(referenced_key, alias_lookup):
                parsed = _parse_asset_key(candidate_key)
                if parsed is None:
                    continue

                asset_kind, doc_guid, asset_name = parsed
                if asset_kind == "resource":
                    payload = resource_payloads.get(candidate_key)
                    candidate_path = output_dir / "_wiz" / "resources" / doc_guid / _sanitize_asset_name(asset_name)
                else:
                    payload = inventory.attachment_bytes_by_key.get(candidate_key)
                    candidate_path = output_dir / "_wiz" / "attachments" / doc_guid / _sanitize_asset_name(asset_name)

                if payload is not None:
                    inventory_key = candidate_key
                    asset_path = candidate_path
                    resolved_kind = asset_kind
                    break

            if payload is None or asset_path is None:
                missing_resources.add(referenced_key)
                note_missing_resources.add(referenced_key)
                continue

            _write_binary(asset_path, payload)
            relative_asset_path = _relative_link(note_path.parent, asset_path)
            resource_paths[referenced_key] = relative_asset_path
            resource_paths[inventory_key] = relative_asset_path
            bare_asset_name = asset_path.name
            if resolved_kind == "resource":
                resource_paths[f"wiz-resource://{bare_asset_name}"] = relative_asset_path
                if needs_plain_name_alias and referenced_key == bare_asset_name:
                    resource_paths[bare_asset_name] = relative_asset_path
                exported_resources.add(asset_path)
            else:
                resource_paths[f"wiz-attachment://{bare_asset_name}"] = relative_asset_path
                if needs_plain_name_alias and referenced_key == bare_asset_name:
                    resource_paths[bare_asset_name] = relative_asset_path
                exported_attachments.add(asset_path)

        attachment_lines: list[str] = []
        missing_attachment_bytes = False
        for attachment in note.attachments:
            attachment_key = make_attachment_key(note.doc_guid, attachment.name)
            payload = None
            resolved_attachment_key = attachment_key
            for candidate_key in _attachment_candidate_keys(note, attachment):
                payload = inventory.attachment_bytes_by_key.get(candidate_key)
                if payload is not None:
                    resolved_attachment_key = candidate_key
                    break
            if payload is None:
                missing_resources.add(attachment_key)
                missing_attachment_bytes = True
                note_missing_resources.add(attachment_key)
                attachment_lines.append(f"- {attachment.name} (missing local bytes)")
                continue

            attachment_path = output_dir / "_wiz" / "attachments" / note.doc_guid / _sanitize_asset_name(attachment.name)
            _write_binary(attachment_path, payload)
            relative_attachment_path = _relative_link(note_path.parent, attachment_path)
            exported_attachments.add(attachment_path)
            attachment_lines.append(f"- [{attachment.name}]({_to_posix(relative_attachment_path)})")
            resource_paths.setdefault(resolved_attachment_key, relative_attachment_path)
            for candidate_key in _attachment_candidate_keys(note, attachment):
                resource_paths.setdefault(candidate_key, relative_attachment_path)
            resource_paths.setdefault(f"wiz-attachment://{attachment.name}", relative_attachment_path)

        preserved_body = None
        if not note.body.has_meaningful_content:
            fallback_paths = [note_path]
            if existing_note_paths_by_doc_guid is not None:
                fallback_path = existing_note_paths_by_doc_guid.get(note.doc_guid)
                if fallback_path is not None and fallback_path not in fallback_paths:
                    fallback_paths.append(fallback_path)
            for candidate_path in fallback_paths:
                preserved_body = _preserved_body_from_existing_note(
                    source_path=candidate_path,
                    target_path=note_path,
                    output_dir=output_dir,
                    doc_guid=note.doc_guid,
                )
                if preserved_body is not None:
                    break

        if preserved_body is not None:
            note_markdown = f"{render_frontmatter(note_for_export)}\n\n{preserved_body.rstrip()}\n"
        else:
            note_markdown = render_note_markdown(note_for_export, resource_paths)
            note_markdown = _append_attachment_section(note_markdown, attachment_lines)
            note_markdown = _replace_missing_assets(note_markdown, note_missing_resources)
        if not note_path.exists() or note_path.read_text(encoding="utf-8") != note_markdown:
            note_path.write_text(note_markdown, encoding="utf-8")
        note_paths_by_doc_guid[note.doc_guid] = note_path
        note_markdowns_by_doc_guid[note.doc_guid] = note_markdown
        missing_resources_by_doc_guid[note.doc_guid] = tuple(sorted(note_missing_resources))
        sync_state_notes_by_doc_guid[note.doc_guid] = SyncStateNote(
            doc_guid=note.doc_guid,
            relative_path=note_relative_path,
            updated=note.updated_at.isoformat() if note.updated_at is not None else None,
            needs_repair=(
                not note.body.has_meaningful_content
                or bool(note_missing_resources)
                or missing_attachment_bytes
            ),
        )

        if note.body.has_meaningful_content:
            exported_notes += 1
        else:
            missing_bodies.add(note.doc_guid)

    report = build_export_report(
        total_notes=len(notes),
        exported_notes=exported_notes,
        missing_bodies=tuple(sorted(missing_bodies)),
        missing_resources=tuple(sorted(missing_resources)),
        exported_resources=len(exported_resources),
        exported_attachments=len(exported_attachments),
    )
    report_path = output_dir / "_wiz" / "report.json"
    if write_content_audit_files:
        content_audit = write_content_audit(
            inventory=Inventory(
                notes=tuple(notes),
                resource_bytes_by_key=inventory.resource_bytes_by_key,
                attachment_bytes_by_key=inventory.attachment_bytes_by_key,
            ),
            output_dir=output_dir,
            note_paths_by_doc_guid=note_paths_by_doc_guid,
            missing_resources_by_doc_guid=missing_resources_by_doc_guid,
            note_markdowns_by_doc_guid=note_markdowns_by_doc_guid,
        )
        report["content_integrity"] = dict(content_audit["summary"])
    if write_report:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    return ExportResult(
        output_dir=output_dir,
        report_path=report_path,
        report=report,
        sync_state=SyncState(notes_by_doc_guid=sync_state_notes_by_doc_guid),
    )


__all__ = ["ExportResult", "export_inventory"]
