from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Callable, Mapping, Sequence

from .exporter import ExportResult, export_inventory
from .markdown_export import NotePathResolver
from .models import Inventory, NoteForExport, SyncState, SyncStateNote, WizNote


FRONTMATTER_BLOCK = re.compile(r"\A---\s*\r?\n(?P<body>.*?)(?:\r?\n)---(?:\r?\n|\Z)", re.DOTALL)
FRONTMATTER_FIELD = re.compile(r"^(?P<key>[A-Za-z0-9_]+):\s*(?P<value>.*)$")


@dataclass(frozen=True)
class ExportedNoteIndexEntry:
    doc_guid: str
    relative_path: Path
    updated: str | None


@dataclass(frozen=True)
class IncrementalSyncPlan:
    notes_to_export: tuple[WizNote, ...]
    note_relative_paths_by_doc_guid: Mapping[str, Path]
    skipped_doc_guids: tuple[str, ...]
    stale_paths_to_remove: tuple[Path, ...]
    reasons_by_doc_guid: Mapping[str, str]


@dataclass(frozen=True)
class IncrementalSyncResult:
    output_dir: Path
    report_path: Path
    report: dict
    plan: IncrementalSyncPlan
    sync_state: SyncState | None = None


@dataclass(frozen=True)
class SyncStateLoadResult:
    state: SyncState
    source: str


def _parse_frontmatter_fields(text: str) -> dict[str, str]:
    match = FRONTMATTER_BLOCK.match(text)
    if not match:
        return {}

    fields: dict[str, str] = {}
    for raw_line in match.group("body").splitlines():
        line = raw_line.rstrip()
        field_match = FRONTMATTER_FIELD.match(line)
        if not field_match:
            continue
        value = field_match.group("value").strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        fields[field_match.group("key")] = value
    return fields


def _iter_exported_markdown_paths(output_dir: Path) -> Sequence[Path]:
    if not output_dir.exists():
        return ()
    return tuple(path for path in output_dir.rglob("*.md") if "_wiz" not in path.parts)


def index_exported_notes(output_dir: Path) -> dict[str, ExportedNoteIndexEntry]:
    entries: dict[str, ExportedNoteIndexEntry] = {}
    for path in _iter_exported_markdown_paths(output_dir):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        fields = _parse_frontmatter_fields(text)
        doc_guid = fields.get("wiz_doc_guid", "").strip()
        if not doc_guid:
            continue
        entries[doc_guid] = ExportedNoteIndexEntry(
            doc_guid=doc_guid,
            relative_path=path.relative_to(output_dir),
            updated=fields.get("updated") or None,
        )
    return entries


def build_note_relative_paths(notes: Sequence[WizNote]) -> dict[str, Path]:
    resolver = NotePathResolver()
    relative_paths: dict[str, Path] = {}
    for note in notes:
        relative_paths[note.doc_guid] = resolver.note_relative_path(NoteForExport.from_wiz_note(note))
    return relative_paths


def _note_updated_string(note: WizNote) -> str | None:
    if note.updated_at is None:
        return None
    return note.updated_at.isoformat()


def sync_state_path(output_dir: Path) -> Path:
    return output_dir / "_wiz" / "state.json"


def _sync_state_payload(state: SyncState) -> dict[str, object]:
    notes_payload: dict[str, dict[str, object]] = {}
    for doc_guid, entry in sorted(state.notes_by_doc_guid.items()):
        note_dict: dict[str, object] = {
            "relative_path": entry.relative_path.as_posix(),
            "updated": entry.updated,
            "needs_repair": bool(entry.needs_repair),
        }
        if entry.remote_version is not None:
            note_dict["remote_version"] = int(entry.remote_version)
        notes_payload[doc_guid] = note_dict
    payload: dict[str, object] = {
        "version": int(state.version),
        "generated_at": state.generated_at,
        "notes": notes_payload,
    }
    if state.doc_version:
        payload["doc_version"] = int(state.doc_version)
    return payload


def _load_sync_state_from_json(payload: Mapping[str, object]) -> SyncState | None:
    notes_payload = payload.get("notes")
    if not isinstance(notes_payload, Mapping):
        return None

    notes_by_doc_guid: dict[str, SyncStateNote] = {}
    for raw_doc_guid, raw_entry in notes_payload.items():
        if not isinstance(raw_entry, Mapping):
            continue
        doc_guid = str(raw_doc_guid).strip()
        relative_path = str(raw_entry.get("relative_path") or "").strip()
        if not doc_guid or not relative_path:
            continue
        notes_by_doc_guid[doc_guid] = SyncStateNote(
            doc_guid=doc_guid,
            relative_path=Path(relative_path),
            updated=str(raw_entry.get("updated") or "").strip() or None,
            needs_repair=bool(raw_entry.get("needs_repair")),
            remote_version=int(raw_entry["remote_version"]) if raw_entry.get("remote_version") is not None else None,
        )

    doc_version = int(payload.get("doc_version") or 0)

    return SyncState(
        notes_by_doc_guid=notes_by_doc_guid,
        version=int(payload.get("version") or 1),
        generated_at=str(payload.get("generated_at") or "").strip() or None,
        doc_version=doc_version,
    )


def load_sync_state(output_dir: Path) -> SyncState | None:
    path = sync_state_path(output_dir)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    return _load_sync_state_from_json(payload)


def _exported_content_needs_repair(text: str) -> bool:
    """Determine if an exported markdown file's content indicates the note needs repair."""
    fm_match = FRONTMATTER_BLOCK.match(text)
    if fm_match:
        content = text[fm_match.end() :].strip()
    else:
        content = text.strip()
    # Empty content always needs repair
    if not content:
        return True
    # Content with only a title line and nothing else likely had no body
    # Pattern: single heading line with optional blank lines
    stripped_lines = [line for line in content.splitlines() if line.strip()]
    if len(stripped_lines) == 1 and stripped_lines[0].startswith("#"):
        return True
    return False


def rebuild_sync_state_from_export(output_dir: Path) -> SyncState:
    existing = index_exported_notes(output_dir)
    notes_by_doc_guid: dict[str, SyncStateNote] = {}
    for doc_guid, entry in existing.items():
        note_path = output_dir / entry.relative_path
        needs_repair = False
        if not note_path.exists():
            needs_repair = True
        else:
            try:
                text = note_path.read_text(encoding="utf-8")
                needs_repair = _exported_content_needs_repair(text)
            except OSError:
                needs_repair = True
        notes_by_doc_guid[doc_guid] = SyncStateNote(
            doc_guid=doc_guid,
            relative_path=entry.relative_path,
            updated=entry.updated,
            needs_repair=needs_repair,
        )
    return SyncState(notes_by_doc_guid=notes_by_doc_guid)


def load_or_rebuild_sync_state(output_dir: Path) -> SyncStateLoadResult:
    loaded_state = load_sync_state(output_dir)
    if loaded_state is not None:
        return SyncStateLoadResult(state=loaded_state, source="state")

    rebuilt_state = rebuild_sync_state_from_export(output_dir)
    if rebuilt_state.notes_by_doc_guid:
        return SyncStateLoadResult(state=rebuilt_state, source="rebuild")
    return SyncStateLoadResult(state=rebuilt_state, source="empty")


def write_sync_state(output_dir: Path, state: SyncState) -> Path:
    path = sync_state_path(output_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(_sync_state_payload(state), ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)
    return path


def plan_incremental_sync(
    inventory: Inventory,
    output_dir: Path,
    *,
    sync_state: SyncState | None = None,
    remote_versions: Mapping[str, Mapping] | None = None,
) -> IncrementalSyncPlan:
    desired_paths = build_note_relative_paths(inventory.notes)
    if sync_state is None:
        sync_state = load_or_rebuild_sync_state(output_dir).state

    notes_to_export: list[WizNote] = []
    skipped_doc_guids: list[str] = []
    stale_paths_to_remove: list[Path] = []
    reasons_by_doc_guid: dict[str, str] = {}

    for note in inventory.notes:
        desired_relative_path = desired_paths[note.doc_guid]
        current = sync_state.notes_by_doc_guid.get(note.doc_guid)
        updated = _note_updated_string(note)

        if current is None:
            reasons_by_doc_guid[note.doc_guid] = "new"
            notes_to_export.append(note)
            continue

        if current.relative_path != desired_relative_path:
            reasons_by_doc_guid[note.doc_guid] = "moved"
            notes_to_export.append(note)
            stale_paths_to_remove.append(output_dir / current.relative_path)
            continue

        if current.updated != updated:
            reasons_by_doc_guid[note.doc_guid] = "updated"
            notes_to_export.append(note)
            continue

        if current.needs_repair:
            reasons_by_doc_guid[note.doc_guid] = "repair"
            notes_to_export.append(note)
            continue

        if remote_versions is not None:
            remote_info = remote_versions.get(note.doc_guid)
            if remote_info is not None:
                data_modified = remote_info.get("dataModified")
                if isinstance(data_modified, (int, float)) and current.updated:
                    from datetime import datetime, timezone

                    remote_dt = datetime.fromtimestamp(data_modified / 1000, tz=timezone.utc)
                    try:
                        state_dt = datetime.fromisoformat(current.updated)
                        if state_dt.tzinfo is None:
                            state_dt = state_dt.replace(tzinfo=timezone.utc)
                    except (ValueError, OverflowError):
                        state_dt = None
                    if state_dt is not None and remote_dt > state_dt:
                        reasons_by_doc_guid[note.doc_guid] = "remote_updated"
                        notes_to_export.append(note)
                        continue

        skipped_doc_guids.append(note.doc_guid)

    unique_stale_paths: list[Path] = []
    seen_paths: set[Path] = set()
    for path in stale_paths_to_remove:
        if path not in seen_paths:
            seen_paths.add(path)
            unique_stale_paths.append(path)

    return IncrementalSyncPlan(
        notes_to_export=tuple(notes_to_export),
        note_relative_paths_by_doc_guid={doc_guid: desired_paths[doc_guid] for doc_guid in reasons_by_doc_guid},
        skipped_doc_guids=tuple(skipped_doc_guids),
        stale_paths_to_remove=tuple(unique_stale_paths),
        reasons_by_doc_guid=reasons_by_doc_guid,
    )


def _remove_empty_parent_dirs(path: Path, *, root: Path) -> None:
    current = path.parent
    while current != root and current.exists():
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def incremental_sync_inventory(
    *,
    inventory: Inventory,
    output_dir: Path,
    limit: int | None = None,
    progress: Callable[[str], None] | None = None,
    plan: IncrementalSyncPlan | None = None,
    sync_state: SyncState | None = None,
    hydration_repair_status: Mapping[str, bool] | None = None,
    doc_version: int = 0,
) -> IncrementalSyncResult:
    base_state = sync_state or load_or_rebuild_sync_state(output_dir).state
    if plan is None:
        scoped_notes = inventory.notes[:limit] if limit is not None else inventory.notes
        scoped_inventory = Inventory(
            notes=tuple(scoped_notes),
            resource_bytes_by_key=inventory.resource_bytes_by_key,
            attachment_bytes_by_key=inventory.attachment_bytes_by_key,
        )
        plan = plan_incremental_sync(scoped_inventory, output_dir, sync_state=base_state)
    else:
        if limit is not None and len(plan.notes_to_export) > limit:
            plan_notes = plan.notes_to_export[:limit]
            plan_guids = {n.doc_guid for n in plan_notes}
            extra_skipped = tuple(n.doc_guid for n in plan.notes_to_export[limit:])
            plan = IncrementalSyncPlan(
                notes_to_export=plan_notes,
                note_relative_paths_by_doc_guid={g: p for g, p in plan.note_relative_paths_by_doc_guid.items() if g in plan_guids},
                skipped_doc_guids=plan.skipped_doc_guids + extra_skipped,
                stale_paths_to_remove=plan.stale_paths_to_remove,
                reasons_by_doc_guid={g: r for g, r in plan.reasons_by_doc_guid.items() if g in plan_guids},
            )
        scoped_inventory = Inventory(
            notes=tuple(plan.notes_to_export),
            resource_bytes_by_key=inventory.resource_bytes_by_key,
            attachment_bytes_by_key=inventory.attachment_bytes_by_key,
        )

    if progress is not None:
        progress(
            "plan: "
            f"export={len(plan.notes_to_export)}, "
            f"skip={len(plan.skipped_doc_guids)}, "
            f"remove_old_paths={len(plan.stale_paths_to_remove)}"
        )
    changed_doc_guids = {note.doc_guid for note in plan.notes_to_export}
    changed_inventory = Inventory(
        notes=tuple(note for note in inventory.notes if note.doc_guid in changed_doc_guids),
        resource_bytes_by_key=inventory.resource_bytes_by_key,
        attachment_bytes_by_key=inventory.attachment_bytes_by_key,
    )

    export_result: ExportResult | None = None
    if plan.notes_to_export:
        export_result = export_inventory(
            inventory=changed_inventory,
            output_dir=output_dir,
            limit=None,
            note_relative_paths_by_doc_guid=dict(plan.note_relative_paths_by_doc_guid),
            existing_note_paths_by_doc_guid={
                doc_guid: output_dir / entry.relative_path
                for doc_guid, entry in base_state.notes_by_doc_guid.items()
                if doc_guid in changed_doc_guids
            },
            write_report=False,
            write_content_audit_files=False,
            progress=progress,
        )

    for stale_path in plan.stale_paths_to_remove:
        if not stale_path.exists():
            continue
        if progress is not None:
            progress(f"remove stale path {stale_path.relative_to(output_dir).as_posix()}")
        stale_path.unlink()
        _remove_empty_parent_dirs(stale_path, root=output_dir)

    new_notes = sum(1 for reason in plan.reasons_by_doc_guid.values() if reason == "new")
    updated_notes = sum(1 for reason in plan.reasons_by_doc_guid.values() if reason == "updated")
    moved_notes = sum(1 for reason in plan.reasons_by_doc_guid.values() if reason == "moved")
    repair_notes = sum(1 for reason in plan.reasons_by_doc_guid.values() if reason == "repair")
    remote_updated_notes = sum(1 for reason in plan.reasons_by_doc_guid.values() if reason == "remote_updated")

    exported_resources = 0
    exported_attachments = 0
    if export_result is not None:
        exported_resources = int(export_result.report["summary"]["exported_resources"])
        exported_attachments = int(export_result.report["summary"]["exported_attachments"])

    updated_state_notes_by_doc_guid = dict(base_state.notes_by_doc_guid)
    if export_result is not None and export_result.sync_state is not None:
        updated_state_notes_by_doc_guid.update(export_result.sync_state.notes_by_doc_guid)
    # Apply hydration repair status to override exporter's needs_repair if available
    if hydration_repair_status is not None:
        for doc_guid, needs_repair in hydration_repair_status.items():
            if doc_guid in updated_state_notes_by_doc_guid:
                existing = updated_state_notes_by_doc_guid[doc_guid]
                updated_state_notes_by_doc_guid[doc_guid] = SyncStateNote(
                    doc_guid=existing.doc_guid,
                    relative_path=existing.relative_path,
                    updated=existing.updated,
                    needs_repair=needs_repair,
                )
    effective_doc_version = max(int(base_state.doc_version or 0), int(doc_version or 0))
    updated_state = SyncState(notes_by_doc_guid=updated_state_notes_by_doc_guid, doc_version=effective_doc_version)
    write_sync_state(output_dir, updated_state)

    report = {
        "summary": {
            "total_notes": len(plan.notes_to_export) + len(plan.skipped_doc_guids),
            "exported_notes": len(plan.notes_to_export),
            "skipped_notes": len(plan.skipped_doc_guids),
            "new_notes": new_notes,
            "updated_notes": updated_notes,
            "moved_notes": moved_notes,
            "repair_notes": repair_notes,
            "remote_updated_notes": remote_updated_notes,
            "removed_old_paths": len(plan.stale_paths_to_remove),
            "exported_resources": exported_resources,
            "exported_attachments": exported_attachments,
        }
    }
    report_path = output_dir / "_wiz" / "report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return IncrementalSyncResult(
        output_dir=output_dir,
        report_path=report_path,
        report=report,
        plan=plan,
        sync_state=updated_state,
    )


__all__ = [
    "ExportedNoteIndexEntry",
    "IncrementalSyncPlan",
    "IncrementalSyncResult",
    "SyncStateLoadResult",
    "build_note_relative_paths",
    "incremental_sync_inventory",
    "index_exported_notes",
    "load_or_rebuild_sync_state",
    "load_sync_state",
    "plan_incremental_sync",
    "rebuild_sync_state_from_export",
    "sync_state_path",
    "write_sync_state",
]
