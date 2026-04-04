from __future__ import annotations

import base64
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import tempfile
from typing import Protocol
from urllib.parse import parse_qs, urlsplit

from .markdown_export import render_collaboration_payload
from .models import AttachmentRecord, NoteBody, WizNote


@dataclass(frozen=True)
class CachedEntry:
    key: str
    payload: bytes


@dataclass(frozen=True)
class CachedAuth:
    token: str = ""
    ks_server_url: str | None = None


class WizCacheBackend(Protocol):
    def iter_entries(self) -> Iterable[CachedEntry]:
        ...


def _load_json_payload(payload: bytes) -> Mapping[str, object] | None:
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, Mapping) else None


def _payload_is_success(payload: Mapping[str, object]) -> bool:
    code = payload.get("returnCode")
    return code in (None, 0, 200, "0", "200")


def _walk_mappings(value: object) -> Iterable[Mapping[str, object]]:
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _walk_mappings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_mappings(child)


def _first_string_field(payload: Mapping[str, object], *field_names: str) -> str | None:
    names = {name.lower() for name in field_names}
    for mapping in _walk_mappings(payload):
        for key, value in mapping.items():
            if str(key).lower() not in names or not isinstance(value, str) or not value:
                continue
            return value
    return None


def _extract_auth_from_payload(payload: Mapping[str, object]) -> CachedAuth | None:
    if not _payload_is_success(payload):
        return None

    token = _first_string_field(payload, "token", "wizToken", "xWizToken")
    if not token:
        return None

    ks_server_url = None
    for candidate in (_first_string_field(payload, "kbServer", "ksServer", "ksUrl", "serverURL", "serverUrl"), None):
        if not candidate:
            continue
        if "ks" in candidate.lower():
            ks_server_url = candidate
            break
        if ks_server_url is None:
            ks_server_url = candidate
    return CachedAuth(token=token, ks_server_url=ks_server_url)


def extract_cached_auth(entries: Iterable[CachedEntry]) -> CachedAuth | None:
    for entry in entries:
        if "/as/user/login" not in entry.key:
            continue
        payload = _load_json_payload(entry.payload)
        if payload is None:
            continue
        auth = _extract_auth_from_payload(payload)
        if auth is not None:
            return auth
    return None


def _extract_object_bytes(payload: bytes) -> bytes | None:
    json_payload = _load_json_payload(payload)
    if json_payload is None:
        return payload
    if not _payload_is_success(json_payload):
        return None

    encoded = _first_string_field(json_payload, "data", "content", "raw")
    if not encoded:
        return None
    try:
        return base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        return encoded.encode("utf-8")


def _note_body_from_payload(payload: bytes, *, doc_guid: str) -> NoteBody:
    json_payload = _load_json_payload(payload)
    if json_payload is None or not _payload_is_success(json_payload):
        return NoteBody()

    html = _first_string_field(json_payload, "html", "noteHtml")
    note_data = next(
        (
            mapping[key]
            for mapping in _walk_mappings(json_payload)
            for key in mapping
            if str(key).lower() == "notedata"
        ),
        None,
    )

    if isinstance(note_data, (Mapping, str, bytes)):
        rendered = render_collaboration_payload(note_data, doc_guid=doc_guid)
        if rendered.markdown:
            return NoteBody(
                markdown=rendered.markdown,
                generated_assets=rendered.generated_assets,
                metadata=rendered.metadata,
            )
    if html:
        return NoteBody(html=html)
    return NoteBody()


def _select_payload(blobs: list[bytes] | tuple[bytes, ...]) -> bytes | None:
    candidates = [blob for blob in blobs if blob]
    if not candidates:
        return None
    return max(candidates, key=len)


class ChromiumCacheBackend:
    def __init__(self, cache_dir: Path) -> None:
        self._cache_dir = cache_dir

    def _snapshot_cache_dir(self) -> Path:
        if not self._cache_dir.exists():
            raise RuntimeError(f"Wiz cache directory does not exist: {self._cache_dir}")

        temp_dir = Path(tempfile.mkdtemp(prefix="wiz-cache-"))
        try:
            for entry in self._cache_dir.iterdir():
                target = temp_dir / entry.name
                if entry.is_dir():
                    shutil.copytree(entry, target)
                else:
                    shutil.copy2(entry, target)
        except PermissionError as exc:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise RuntimeError("Wiz cache is locked; close WizNote to enable cache hydration") from exc
        return temp_dir

    def iter_entries(self) -> Iterable[CachedEntry]:
        try:
            from ccl_chromium_reader.ccl_chromium_cache import guess_cache_class
        except ImportError as exc:
            raise RuntimeError("ccl_chromium_reader is required to read Wiz cache data") from exc

        cache_dir = self._cache_dir
        cleanup_dir: Path | None = None
        cache_class = guess_cache_class(cache_dir)
        if cache_class is None:
            snapshot_dir = self._snapshot_cache_dir()
            cache_dir = snapshot_dir
            cleanup_dir = snapshot_dir
            cache_class = guess_cache_class(cache_dir)
        if cache_class is None:
            if cleanup_dir is not None:
                shutil.rmtree(cleanup_dir, ignore_errors=True)
            raise RuntimeError(f"Unsupported Chromium cache layout: {cache_dir}")

        try:
            cache = cache_class(cache_dir)
        except PermissionError:
            snapshot_dir = self._snapshot_cache_dir()
            cache_dir = snapshot_dir
            cleanup_dir = snapshot_dir
            cache_class = guess_cache_class(cache_dir)
            if cache_class is None:
                shutil.rmtree(snapshot_dir, ignore_errors=True)
                raise RuntimeError(f"Unsupported Chromium cache layout: {cache_dir}")
            cache = cache_class(cache_dir)

        try:
            for key in cache.keys():
                payload = _select_payload(cache.get_cachefile(key))
                if payload is None:
                    continue
                yield CachedEntry(key=key, payload=payload)
        finally:
            cache.close()
            if cleanup_dir is not None:
                shutil.rmtree(cleanup_dir, ignore_errors=True)


class CachedWizClient:
    def __init__(self, backend: WizCacheBackend) -> None:
        self._entries = tuple(backend.iter_entries())
        self.cached_auth = extract_cached_auth(self._entries) or CachedAuth()

    def _find_entry(self, predicate) -> CachedEntry | None:
        for entry in self._entries:
            if predicate(entry):
                return entry
        return None

    def fetch_note_body(self, note: WizNote) -> NoteBody:
        entry = self._find_entry(
            lambda item: urlsplit(item.key).path.endswith(f"/ks/note/download/{note.kb_guid}/{note.doc_guid}")
        )
        if entry is None:
            return NoteBody()
        return _note_body_from_payload(entry.payload, doc_guid=note.doc_guid)

    def fetch_resource(self, note: WizNote, resource_name: str) -> bytes | None:
        return self._fetch_object_bytes(note, obj_type="resource", obj_id=resource_name)

    def fetch_attachment(self, note: WizNote, attachment: AttachmentRecord) -> bytes | None:
        for obj_id in (attachment.att_guid, attachment.name):
            if not obj_id:
                continue
            payload = self._fetch_object_bytes(note, obj_type="attachment", obj_id=obj_id)
            if payload is not None:
                return payload
        return None

    def _fetch_object_bytes(self, note: WizNote, *, obj_type: str, obj_id: str) -> bytes | None:
        def predicate(entry: CachedEntry) -> bool:
            parsed = urlsplit(entry.key)
            if not parsed.path.endswith(f"/ks/object/download/{note.kb_guid}/{note.doc_guid}"):
                return False
            query = parse_qs(parsed.query)
            return query.get("objType", [""])[0] == obj_type and query.get("objId", [""])[0] == obj_id

        entry = self._find_entry(predicate)
        if entry is None:
            return None
        return _extract_object_bytes(entry.payload)


__all__ = [
    "CachedAuth",
    "CachedEntry",
    "CachedWizClient",
    "ChromiumCacheBackend",
    "extract_cached_auth",
]
