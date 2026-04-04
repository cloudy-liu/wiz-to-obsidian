from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Callable, Mapping, Sequence

from .exporter import ExportResult, export_inventory
from .markdown_export import NotePathResolver
from .models import Inventory, NoteForExport, WizNote


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


def plan_incremental_sync(inventory: Inventory, output_dir: Path) -> IncrementalSyncPlan:
    desired_paths = build_note_relative_paths(inventory.notes)
    existing = index_exported_notes(output_dir)

    notes_to_export: list[WizNote] = []
    skipped_doc_guids: list[str] = []
    stale_paths_to_remove: list[Path] = []
    reasons_by_doc_guid: dict[str, str] = {}

    for note in inventory.notes:
        desired_relative_path = desired_paths[note.doc_guid]
        current = existing.get(note.doc_guid)
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
) -> IncrementalSyncResult:
    scoped_notes = inventory.notes[:limit] if limit is not None else inventory.notes
    scoped_inventory = Inventory(
        notes=tuple(scoped_notes),
        resource_bytes_by_key=inventory.resource_bytes_by_key,
        attachment_bytes_by_key=inventory.attachment_bytes_by_key,
    )

    if progress is not None:
        progress(f"indexing existing export under {output_dir}")
    existing = index_exported_notes(output_dir)
    if not existing:
        if progress is not None:
            progress("no previous export found, falling back to full export")
        export_result = export_inventory(
            inventory=scoped_inventory,
            output_dir=output_dir,
            limit=None,
            progress=progress,
        )
        plan = IncrementalSyncPlan(
            notes_to_export=scoped_inventory.notes,
            note_relative_paths_by_doc_guid=build_note_relative_paths(scoped_inventory.notes),
            skipped_doc_guids=(),
            stale_paths_to_remove=(),
            reasons_by_doc_guid={note.doc_guid: "new" for note in scoped_inventory.notes},
        )
        report = dict(export_result.report)
        report.setdefault("summary", {})
        report["summary"].update(
            {
                "skipped_notes": 0,
                "new_notes": len(scoped_inventory.notes),
                "updated_notes": 0,
                "moved_notes": 0,
                "removed_old_paths": 0,
            }
        )
        return IncrementalSyncResult(
            output_dir=export_result.output_dir,
            report_path=export_result.report_path,
            report=report,
            plan=plan,
        )

    plan = plan_incremental_sync(scoped_inventory, output_dir)
    if progress is not None:
        progress(
            "plan: "
            f"export={len(plan.notes_to_export)}, "
            f"skip={len(plan.skipped_doc_guids)}, "
            f"remove_old_paths={len(plan.stale_paths_to_remove)}"
        )
    changed_inventory = Inventory(
        notes=plan.notes_to_export,
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

    exported_resources = 0
    exported_attachments = 0
    if export_result is not None:
        exported_resources = int(export_result.report["summary"]["exported_resources"])
        exported_attachments = int(export_result.report["summary"]["exported_attachments"])

    report = {
        "summary": {
            "total_notes": len(scoped_inventory.notes),
            "exported_notes": len(plan.notes_to_export),
            "skipped_notes": len(plan.skipped_doc_guids),
            "new_notes": new_notes,
            "updated_notes": updated_notes,
            "moved_notes": moved_notes,
            "removed_old_paths": len(plan.stale_paths_to_remove),
            "exported_resources": exported_resources,
            "exported_attachments": exported_attachments,
        }
    }
    report_path = output_dir / "_wiz" / "report.json"
    return IncrementalSyncResult(
        output_dir=output_dir,
        report_path=report_path,
        report=report,
        plan=plan,
    )


__all__ = [
    "ExportedNoteIndexEntry",
    "IncrementalSyncPlan",
    "IncrementalSyncResult",
    "build_note_relative_paths",
    "incremental_sync_inventory",
    "index_exported_notes",
    "plan_incremental_sync",
]
