from __future__ import annotations

from dataclasses import dataclass, replace
import re
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


class CompositeWizContentClient:
    def __init__(self, clients: Sequence[WizContentClient]) -> None:
        self._clients = tuple(client for client in clients)

    def fetch_note_body(self, note: WizNote) -> NoteBody:
        for client in self._clients:
            body = client.fetch_note_body(note)
            if body.has_meaningful_content:
                return body
        return NoteBody()

    def fetch_resource(self, note: WizNote, resource_name: str) -> bytes | None:
        for client in self._clients:
            payload = client.fetch_resource(note, resource_name)
            if payload is not None and not _is_placeholder_resource_payload(payload):
                return payload
        return None

    def fetch_attachment(self, note: WizNote, attachment: AttachmentRecord) -> bytes | None:
        for client in self._clients:
            payload = client.fetch_attachment(note, attachment)
            if payload is not None:
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


def hydrate_inventory(
    *,
    inventory: Inventory,
    client: WizContentClient,
    progress: Callable[[str], None] | None = None,
    refresh_note_bodies: bool = False,
) -> HydrationResult:
    notes: list[WizNote] = []
    resource_bytes_by_key = dict(inventory.resource_bytes_by_key)
    attachment_bytes_by_key = dict(inventory.attachment_bytes_by_key)
    summary = {
        "hydrated_notes": 0,
        "hydrated_resources": 0,
        "hydrated_attachments": 0,
        "hydration_failures": 0,
    }

    body_retry_failures: set[str] = set()

    def safe_fetch_note_body(note: WizNote) -> tuple[NoteBody, bool]:
        for attempt in range(FETCH_RETRY_ATTEMPTS):
            try:
                return client.fetch_note_body(note), False
            except Exception:
                if attempt == FETCH_RETRY_ATTEMPTS - 1:
                    return NoteBody(), True
        return NoteBody(), False

    def safe_fetch_resource(note: WizNote, resource_name: str) -> bytes | None:
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

    def safe_fetch_attachment(note: WizNote, attachment: AttachmentRecord) -> bytes | None:
        for attempt in range(FETCH_RETRY_ATTEMPTS):
            try:
                return client.fetch_attachment(note, attachment)
            except Exception:
                if attempt == FETCH_RETRY_ATTEMPTS - 1:
                    summary["hydration_failures"] += 1
                    return None
        return None

    total_notes = len(inventory.notes)
    for index, note in enumerate(inventory.notes, start=1):
        if progress is not None:
            progress(f"{index}/{total_notes} {note.title}")

        body = note.body
        resource_names = _iter_resource_names(body)
        missing_resource_names = {
            resource_name
            for resource_name in resource_names
            if make_resource_key(note.doc_guid, resource_name) not in resource_bytes_by_key
        }

        if refresh_note_bodies or not body.has_meaningful_content or missing_resource_names:
            fetched_body, fetch_failed = safe_fetch_note_body(note)
            if fetch_failed and note.doc_guid not in body_retry_failures:
                body_retry_failures.add(note.doc_guid)
                summary["hydration_failures"] += 1
            if fetched_body.has_meaningful_content and fetched_body != body:
                body = fetched_body
                summary["hydrated_notes"] += 1
            resource_names.update(_iter_resource_names(fetched_body))

        for resource_name in sorted(resource_names):
            resource_key = make_resource_key(note.doc_guid, resource_name)
            if resource_key in resource_bytes_by_key:
                continue
            payload = safe_fetch_resource(note, resource_name)
            if payload is None:
                continue
            resource_bytes_by_key[resource_key] = payload
            summary["hydrated_resources"] += 1

        for attachment in note.attachments:
            candidate_keys = _attachment_keys(note, attachment)
            if any(key in attachment_bytes_by_key for key in candidate_keys):
                continue
            payload = safe_fetch_attachment(note, attachment)
            if payload is None:
                continue
            for key in candidate_keys:
                attachment_bytes_by_key[key] = payload
            summary["hydrated_attachments"] += 1

        notes.append(replace(note, body=body) if body != note.body else note)

    for index, note in enumerate(notes):
        if note.body.has_meaningful_content:
            continue

        fetched_body, fetch_failed = safe_fetch_note_body(note)
        if fetch_failed:
            continue
        if not fetched_body.has_meaningful_content:
            continue

        body = fetched_body
        summary["hydrated_notes"] += 1
        if note.doc_guid in body_retry_failures:
            body_retry_failures.remove(note.doc_guid)
            summary["hydration_failures"] = max(0, summary["hydration_failures"] - 1)

        for resource_name in sorted(_iter_resource_names(body)):
            resource_key = make_resource_key(note.doc_guid, resource_name)
            if resource_key in resource_bytes_by_key:
                continue
            payload = safe_fetch_resource(note, resource_name)
            if payload is None:
                continue
            resource_bytes_by_key[resource_key] = payload
            summary["hydrated_resources"] += 1

        notes[index] = replace(note, body=body)

    hydrated_inventory = Inventory(
        notes=tuple(notes),
        resource_bytes_by_key=resource_bytes_by_key,
        attachment_bytes_by_key=attachment_bytes_by_key,
    )
    return HydrationResult(inventory=hydrated_inventory, summary=summary)


__all__ = [
    "CompositeWizContentClient",
    "HydrationResult",
    "WizContentClient",
    "hydrate_inventory",
]
