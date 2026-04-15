from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
import re
import threading
from typing import Callable, Protocol, Sequence

from .markdown_export import make_attachment_key, make_resource_key
from .models import AttachmentRecord, Inventory, NoteBody, WizNote


WIZ_RESOURCE_RE = re.compile(r"wiz-resource://(?:[^/]+/)?(?P<name>[^\s)\]\"'>]+)")
HTML_ASSET_RE = re.compile(r"""(?:src|href)\s*=\s*["'](?P<value>[^"']+)["']""", re.IGNORECASE)
MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\((?P<value>[^)\r\n]+)\)")
SKIP_ASSET_PREFIXES = ("http://", "https://", "data:", "javascript:", "mailto:", "#", "obsidian://")
FETCH_RETRY_ATTEMPTS = 2


def _is_placeholder_resource_payload(payload: bytes | None) -> bool:
    return bool(payload) and len(payload) == 807 and payload.startswith(b"GIF89a\x01\x00\x01\x00")


class WizContentClient(Protocol):
    def fetch_note_body(self, note: WizNote) -> NoteBody:
        ...

    def fetch_resource(self, note: WizNote, resource_name: str) -> bytes | None:
        ...

    def fetch_attachment(self, note: WizNote, attachment: AttachmentRecord) -> bytes | None:
        ...


@dataclass(frozen=True)
class HydrationResult:
    inventory: Inventory
    summary: dict[str, int]
    note_repair_status: dict[str, bool] | None = None
    hydration_source_summary: dict[str, int] | None = None
    cache_unavailable: bool = False


SOURCE_CACHE = "cache"
SOURCE_REMOTE = "remote"


class HydrationSourceTracker:
    """Thread-safe tracker for which hydration source (cache or remote) provided results."""

    def __init__(self, cache_available: bool) -> None:
        self.cache_available = cache_available
        self._note_sources: dict[str, str] = {}
        self._resource_sources: dict[str, str] = {}
        self._lock = threading.Lock()

    def record_note_source(self, doc_guid: str, source: str) -> None:
        with self._lock:
            self._note_sources[doc_guid] = source

    def record_resource_source(self, key: str, source: str) -> None:
        with self._lock:
            self._resource_sources[key] = source

    @property
    def source_summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for source in self._note_sources.values():
            counts[source] = counts.get(source, 0) + 1
        for source in self._resource_sources.values():
            counts[source] = counts.get(source, 0) + 1
        return counts


class CompositeWizContentClient:
    def __init__(self, clients: Sequence[WizContentClient]) -> None:
        self._clients = tuple(client for client in clients)
        self.source_tracker: HydrationSourceTracker | None = None

    def fetch_note_body(self, note: WizNote, *, force_refresh: bool = False) -> NoteBody:
        for i, client in enumerate(self._clients):
            if force_refresh and i == 0:
                continue
            body = client.fetch_note_body(note)
            if body.has_meaningful_content:
                if self.source_tracker is not None:
                    self.source_tracker.record_note_source(note.doc_guid, SOURCE_CACHE if i == 0 else SOURCE_REMOTE)
                return body
        return NoteBody()

    def fetch_resource(self, note: WizNote, resource_name: str) -> bytes | None:
        for i, client in enumerate(self._clients):
            payload = client.fetch_resource(note, resource_name)
            if payload is not None and not _is_placeholder_resource_payload(payload):
                if self.source_tracker is not None:
                    self.source_tracker.record_resource_source(
                        make_resource_key(note.doc_guid, resource_name),
                        SOURCE_CACHE if i == 0 else SOURCE_REMOTE,
                    )
                return payload
        return None

    def fetch_attachment(self, note: WizNote, attachment: AttachmentRecord) -> bytes | None:
        for i, client in enumerate(self._clients):
            payload = client.fetch_attachment(note, attachment)
            if payload is not None:
                if self.source_tracker is not None:
                    self.source_tracker.record_resource_source(
                        make_attachment_key(note.doc_guid, attachment.name),
                        SOURCE_CACHE if i == 0 else SOURCE_REMOTE,
                    )
                return payload
        return None

    def close(self) -> None:
        for client in self._clients:
            close = getattr(client, "close", None)
            if callable(close):
                close()


def _basename(value: str) -> str:
    text = value.strip().replace("\\", "/")
    if not text:
        return ""
    return text.split("/")[-1]


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


def _is_legacy_markdown_asset_path(value: str) -> bool:
    return value.startswith("index_files/") or bool(re.match(r"[^/\s]+_files/", value))


def _iter_resource_names(body: NoteBody) -> set[str]:
    if body.is_placeholder:
        return set()

    names: set[str] = set()
    for text in (body.markdown, body.html):
        if not text:
            continue
        for match in WIZ_RESOURCE_RE.finditer(text):
            for option in match.group("name").split("|"):
                normalized = _basename(option)
                if normalized:
                    names.add(normalized)
        for match in HTML_ASSET_RE.finditer(text):
            value = match.group("value").strip()
            if not value or value.startswith("wiz-") or value.lower().startswith(SKIP_ASSET_PREFIXES):
                continue
            normalized = _basename(value)
            if normalized:
                names.add(normalized)
        for match in MARKDOWN_IMAGE_RE.finditer(text):
            value = _normalize_markdown_target(match.group("value"))
            if not value or value.startswith("wiz-") or value.lower().startswith(SKIP_ASSET_PREFIXES):
                continue
            if not _is_legacy_markdown_asset_path(value):
                continue
            normalized = _basename(value)
            if normalized:
                names.add(normalized)
    return names


def _attachment_keys(note: WizNote, attachment: AttachmentRecord) -> list[str]:
    keys: list[str] = []
    if attachment.att_guid:
        keys.append(make_attachment_key(note.doc_guid, attachment.att_guid))
    if attachment.name:
        name_key = make_attachment_key(note.doc_guid, attachment.name)
        if name_key not in keys:
            keys.append(name_key)
    return keys


_CONCURRENT_MAX_WORKERS = 6


def _hydrate_single_note(
    note: WizNote,
    client: WizContentClient,
    existing_resource_keys: set[str],
    existing_attachment_keys: set[str],
    force_refresh_note_body: bool,
) -> _SingleNoteHydrationResult:
    body = note.body
    resource_names = _iter_resource_names(body)
    missing_resource_names = {
        resource_name
        for resource_name in resource_names
        if make_resource_key(note.doc_guid, resource_name) not in existing_resource_keys
    }

    local_summary = {
        "hydrated_notes": 0,
        "hydrated_resources": 0,
        "hydrated_attachments": 0,
        "hydration_failures": 0,
    }
    new_resources: dict[str, bytes] = {}
    new_attachments: dict[str, bytes] = {}
    body_changed = False

    if force_refresh_note_body or not body.has_meaningful_content or missing_resource_names:
        fetched_body, fetch_failed = _safe_fetch_note_body(
            client, note, local_summary, force_refresh=force_refresh_note_body,
        )
        if fetched_body.has_meaningful_content and fetched_body != body:
            body = fetched_body
            local_summary["hydrated_notes"] += 1
            body_changed = True
        resource_names.update(_iter_resource_names(fetched_body))

    for resource_name in sorted(resource_names):
        resource_key = make_resource_key(note.doc_guid, resource_name)
        if resource_key in existing_resource_keys or resource_key in new_resources:
            continue
        payload = _safe_fetch_resource(client, note, resource_name, local_summary)
        if payload is None:
            continue
        new_resources[resource_key] = payload
        local_summary["hydrated_resources"] += 1

    for attachment in note.attachments:
        candidate_keys = _attachment_keys(note, attachment)
        if any(key in existing_attachment_keys or key in new_attachments for key in candidate_keys):
            continue
        payload = _safe_fetch_attachment(client, note, attachment, local_summary)
        if payload is None:
            continue
        for key in candidate_keys:
            new_attachments[key] = payload
        local_summary["hydrated_attachments"] += 1

    updated_note = replace(note, body=body) if body_changed else note

    needs_repair = (
        not body.has_meaningful_content
        or any(
            make_resource_key(note.doc_guid, name) not in existing_resource_keys
            and make_resource_key(note.doc_guid, name) not in new_resources
            for name in _iter_resource_names(body)
        )
        or any(
            key not in existing_attachment_keys and key not in new_attachments
            for key in _all_attachment_candidate_keys(note)
        )
    )

    return _SingleNoteHydrationResult(
        note=updated_note,
        new_resources=new_resources,
        new_attachments=new_attachments,
        summary=local_summary,
        needs_repair=needs_repair,
    )


@dataclass
class _SingleNoteHydrationResult:
    note: WizNote
    new_resources: dict[str, bytes]
    new_attachments: dict[str, bytes]
    summary: dict[str, int]
    needs_repair: bool


def _safe_fetch_note_body(
    client: WizContentClient, note: WizNote, summary: dict[str, int], *, force_refresh: bool = False,
) -> tuple[NoteBody, bool]:
    for attempt in range(FETCH_RETRY_ATTEMPTS):
        try:
            try:
                return client.fetch_note_body(note, force_refresh=force_refresh), False
            except TypeError:
                return client.fetch_note_body(note), False
        except Exception:
            if attempt == FETCH_RETRY_ATTEMPTS - 1:
                summary["hydration_failures"] += 1
                return NoteBody(), True
    return NoteBody(), False


def _safe_fetch_resource(
    client: WizContentClient, note: WizNote, resource_name: str, summary: dict[str, int]
) -> bytes | None:
    for attempt in range(FETCH_RETRY_ATTEMPTS):
        try:
            payload = client.fetch_resource(note, resource_name)
        except Exception:
            if attempt == FETCH_RETRY_ATTEMPTS - 1:
                summary["hydration_failures"] += 1
                return None
            continue
        if _is_placeholder_resource_payload(payload):
            if attempt == FETCH_RETRY_ATTEMPTS - 1:
                summary["hydration_failures"] += 1
                return None
            continue
        return payload
    return None


def _safe_fetch_attachment(
    client: WizContentClient, note: WizNote, attachment: AttachmentRecord, summary: dict[str, int]
) -> bytes | None:
    for attempt in range(FETCH_RETRY_ATTEMPTS):
        try:
            return client.fetch_attachment(note, attachment)
        except Exception:
            if attempt == FETCH_RETRY_ATTEMPTS - 1:
                summary["hydration_failures"] += 1
                return None
    return None


def _all_attachment_candidate_keys(note: WizNote) -> list[str]:
    keys: list[str] = []
    for attachment in note.attachments:
        keys.extend(_attachment_keys(note, attachment))
    return keys


def hydrate_inventory(
    *,
    inventory: Inventory,
    client: WizContentClient,
    progress: Callable[[str], None] | None = None,
    refresh_note_bodies: bool = False,
    refresh_note_bodies_for_doc_guids: set[str] | None = None,
) -> HydrationResult:
    resource_bytes_by_key = dict(inventory.resource_bytes_by_key)
    attachment_bytes_by_key = dict(inventory.attachment_bytes_by_key)
    summary = {
        "hydrated_notes": 0,
        "hydrated_resources": 0,
        "hydrated_attachments": 0,
        "hydration_failures": 0,
    }
    note_repair_status: dict[str, bool] = {}

    # Phase 1: concurrent hydration per note
    total_notes = len(inventory.notes)
    existing_resource_keys = set(resource_bytes_by_key.keys())
    existing_attachment_keys = set(attachment_bytes_by_key.keys())

    note_force_refresh = {}
    for note in inventory.notes:
        force_refresh_note_body = refresh_note_bodies or (
            refresh_note_bodies_for_doc_guids is not None and note.doc_guid in refresh_note_bodies_for_doc_guids
        )
        note_force_refresh[note.doc_guid] = force_refresh_note_body

    # Use ThreadPoolExecutor for concurrent hydration
    results_by_index: dict[int, _SingleNoteHydrationResult] = {}
    with ThreadPoolExecutor(max_workers=_CONCURRENT_MAX_WORKERS) as executor:
        futures = {}
        for index, note in enumerate(inventory.notes):
            future = executor.submit(
                _hydrate_single_note,
                note=note,
                client=client,
                existing_resource_keys=existing_resource_keys,
                existing_attachment_keys=existing_attachment_keys,
                force_refresh_note_body=note_force_refresh[note.doc_guid],
            )
            futures[future] = index

        for future in as_completed(futures):
            index = futures[future]
            try:
                results_by_index[index] = future.result()
            except Exception:
                # Isolate failure: create a minimal result preserving the original note
                note = inventory.notes[index]
                results_by_index[index] = _SingleNoteHydrationResult(
                    note=note,
                    new_resources={},
                    new_attachments={},
                    summary={"hydrated_notes": 0, "hydrated_resources": 0, "hydrated_attachments": 0, "hydration_failures": 1},
                    needs_repair=True,
                )

    # Merge results in input order
    notes: list[WizNote] = []
    for index in range(total_notes):
        result = results_by_index[index]
        notes.append(result.note)
        resource_bytes_by_key.update(result.new_resources)
        attachment_bytes_by_key.update(result.new_attachments)
        for key in ("hydrated_notes", "hydrated_resources", "hydrated_attachments", "hydration_failures"):
            summary[key] += result.summary.get(key, 0)
        note_repair_status[inventory.notes[index].doc_guid] = result.needs_repair

        if progress is not None:
            progress(f"{index + 1}/{total_notes} {inventory.notes[index].title}")

    # Phase 2: sequential retry for notes still lacking meaningful content
    for index, note in enumerate(notes):
        if note.body.has_meaningful_content:
            continue

        fetched_body, fetch_failed = _safe_fetch_note_body(client, note, summary)
        if fetch_failed:
            continue
        if not fetched_body.has_meaningful_content:
            continue

        body = fetched_body
        summary["hydrated_notes"] += 1
        summary["hydration_failures"] = max(0, summary["hydration_failures"] - 1)

        for resource_name in sorted(_iter_resource_names(body)):
            resource_key = make_resource_key(note.doc_guid, resource_name)
            if resource_key in resource_bytes_by_key:
                continue
            payload = _safe_fetch_resource(client, note, resource_name, summary)
            if payload is None:
                continue
            resource_bytes_by_key[resource_key] = payload
            summary["hydrated_resources"] += 1

        notes[index] = replace(note, body=body)
        note_repair_status[note.doc_guid] = not body.has_meaningful_content or any(
            make_resource_key(note.doc_guid, name) not in resource_bytes_by_key
            for name in _iter_resource_names(body)
        )

    hydrated_inventory = Inventory(
        notes=tuple(notes),
        resource_bytes_by_key=resource_bytes_by_key,
        attachment_bytes_by_key=attachment_bytes_by_key,
    )
    # Extract source tracking info if available
    source_tracker = getattr(client, "source_tracker", None)
    source_summary = source_tracker.source_summary if source_tracker is not None else None
    cache_unavailable = not source_tracker.cache_available if source_tracker is not None else False
    return HydrationResult(
        inventory=hydrated_inventory,
        summary=summary,
        note_repair_status=note_repair_status,
        hydration_source_summary=source_summary,
        cache_unavailable=cache_unavailable,
    )


__all__ = [
    "CompositeWizContentClient",
    "HydrationResult",
    "HydrationSourceTracker",
    "SOURCE_CACHE",
    "SOURCE_REMOTE",
    "WizContentClient",
    "hydrate_inventory",
]
