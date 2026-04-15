from __future__ import annotations

import codecs
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Iterable, Mapping, Protocol, Sequence

from .config import default_blob_dir, default_leveldb_dir
from .markdown_export import make_attachment_key, make_resource_key, render_collaboration_payload
from .models import AttachmentRecord, Inventory, NoteBody, WizNote


class WizRecordSource(Protocol):
    def iter_store_values(self, db_name: str, store_name: str, *, skip_bad: bool = False) -> Iterable[Mapping[str, object]]:
        ...


class IndexedDbWizSource:
    def __init__(self, *, leveldb_dir: Path, blob_dir: Path) -> None:
        try:
            from ccl_chromium_reader import ccl_chromium_indexeddb
        except ImportError as exc:
            raise RuntimeError("ccl_chromium_reader is required to scan local Wiz data") from exc

        self._wrapped = ccl_chromium_indexeddb.WrappedIndexDB(leveldb_dir, blob_dir)

    def iter_store_values(self, db_name: str, store_name: str, *, skip_bad: bool = False) -> Iterable[Mapping[str, object]]:
        store = self._wrapped[db_name][store_name]
        iterate_kwargs: dict[str, object] = {"live_only": True}
        if skip_bad:
            iterate_kwargs["bad_deserializer_data_handler"] = lambda key, data: None

        for record in store.iterate_records(**iterate_kwargs):
            if record is None:
                continue
            value = getattr(record, "value", None)
            if isinstance(value, Mapping):
                yield value


def _parse_datetime(value: object | None) -> datetime | None:
    if value in (None, "", 0):
        return None

    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

    if isinstance(value, (int, float)):
        timestamp = float(value)
        if abs(timestamp) >= 1_000_000_000_000:
            timestamp /= 1000.0
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)

    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return _parse_datetime(int(text))
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _flatten_folders(
    folders: Sequence[Mapping[str, object]],
    lookup: dict[str, tuple[str, ...]] | None = None,
) -> dict[str, tuple[str, ...]]:
    if lookup is None:
        lookup = {}
    for folder in folders:
        location = str(folder.get("location") or "")
        name = str(folder.get("name") or "").strip("/")
        existing = tuple(part for part in location.strip("/").split("/") if part)
        if name and (not existing or existing[-1] != name):
            existing = (*existing, name)
        lookup[location] = existing
        children = folder.get("children") or []
        if isinstance(children, Sequence) and not isinstance(children, (str, bytes, bytearray)):
            _flatten_folders(children, lookup)
    return lookup


def _merge_doc_records(existing: Mapping[str, object], incoming: Mapping[str, object]) -> dict[str, object]:
    merged = dict(existing)
    for key, value in incoming.items():
        if value in (None, ""):
            continue
        merged[key] = value
    return merged


def _dedupe_docs(docs: Sequence[Mapping[str, object]]) -> list[Mapping[str, object]]:
    docs_by_guid: dict[str, Mapping[str, object]] = {}
    ordered_guids: list[str] = []
    anonymous_docs: list[Mapping[str, object]] = []

    for doc in docs:
        doc_guid = str(doc.get("docGuid") or "")
        if not doc_guid:
            anonymous_docs.append(doc)
            continue
        if doc_guid not in docs_by_guid:
            docs_by_guid[doc_guid] = doc
            ordered_guids.append(doc_guid)
            continue
        docs_by_guid[doc_guid] = _merge_doc_records(docs_by_guid[doc_guid], doc)

    return [*anonymous_docs, *(docs_by_guid[doc_guid] for doc_guid in ordered_guids)]


def build_inventory_from_records(
    *,
    kb: Mapping[str, object],
    folders: Sequence[Mapping[str, object]],
    docs: Sequence[Mapping[str, object]],
    attachments: Sequence[Mapping[str, object]],
    body_by_doc: Mapping[str, NoteBody],
    resource_bytes_by_key: Mapping[str, bytes],
    attachment_bytes_by_key: Mapping[str, bytes] | None = None,
) -> Inventory:
    folder_lookup = _flatten_folders(folders)
    attachments_by_doc: dict[str, list[AttachmentRecord]] = defaultdict(list)
    for attachment in attachments:
        attachments_by_doc[str(attachment.get("docGuid") or "")].append(
            AttachmentRecord(
                att_guid=str(attachment.get("attGuid") or ""),
                doc_guid=str(attachment.get("docGuid") or ""),
                name=str(attachment.get("name") or ""),
                size=int(attachment.get("dataSize") or 0),
            )
        )

    kb_name = str(kb.get("name") or kb.get("displayName") or kb.get("userId") or "")
    kb_guid = str(kb.get("kbGuid") or "")

    notes: list[WizNote] = []
    for doc in _dedupe_docs(docs):
        doc_guid = str(doc.get("docGuid") or "")
        if not doc_guid:
            continue
        tags = tuple(sorted(str(tag) for tag in (doc.get("tags") or [])))
        created_at = _parse_datetime(doc.get("dateCreated") or doc.get("created"))
        updated_at = _parse_datetime(doc.get("dateModified") or doc.get("dataModified") or doc.get("modified"))
        notes.append(
            WizNote(
                kb_name=kb_name,
                kb_guid=kb_guid,
                doc_guid=doc_guid,
                title=str(doc.get("title") or doc_guid),
                folder_parts=folder_lookup.get(str(doc.get("category") or ""), ()),
                tags=tags,
                note_type=str(doc.get("type") or ""),
                created_at=created_at,
                updated_at=updated_at,
                abstract=str(doc.get("abstractText") or "") or None,
                body=body_by_doc.get(doc_guid, NoteBody()),
                attachments=tuple(attachments_by_doc.get(doc_guid, [])),
            )
        )

    notes.sort(key=lambda note: (note.folder_parts, note.title.lower(), note.doc_guid))
    return Inventory(
        notes=tuple(notes),
        resource_bytes_by_key=dict(resource_bytes_by_key),
        attachment_bytes_by_key=dict(attachment_bytes_by_key or {}),
    )


def _pick_account(accounts: Sequence[Mapping[str, object]]) -> Mapping[str, object]:
    if not accounts:
        raise RuntimeError("No Wiz account records found in local IndexedDB data")
    for account in accounts:
        if account.get("current"):
            return account
    return accounts[0]


def _pick_kb(account: Mapping[str, object], kbs: Sequence[Mapping[str, object]]) -> Mapping[str, object]:
    preferred_kb_guid = str(account.get("kbGuid") or "")
    for kb in kbs:
        if str(kb.get("kbGuid") or "") == preferred_kb_guid:
            break
    else:
        kb = next((item for item in kbs if str(item.get("type") or "") == "person"), kbs[0] if kbs else {})

    if not kb:
        return {
            "kbGuid": preferred_kb_guid,
            "name": str(account.get("displayName") or account.get("userId") or preferred_kb_guid),
        }

    if kb.get("name"):
        return kb
    hydrated = dict(kb)
    hydrated["name"] = str(account.get("displayName") or account.get("userId") or hydrated.get("kbGuid") or "")
    return hydrated


def _load_account_context(source: WizRecordSource) -> tuple[Mapping[str, object], Mapping[str, object], str, str]:
    accounts = list(source.iter_store_values("wiz-account", "accounts"))
    account = _pick_account(accounts)
    user_guid = str(account.get("userGuid") or "")
    if not user_guid:
        raise RuntimeError("Wiz account record is missing userGuid")

    user_db = f"wiz-{user_guid}"
    kbs = list(source.iter_store_values(user_db, "kbs"))
    kb = _pick_kb(account, kbs)
    kb_guid = str(kb.get("kbGuid") or "")
    return account, kb, user_db, kb_guid


def _matches_kb(record: Mapping[str, object], kb_guid: str) -> bool:
    record_kb_guid = str(record.get("kbGuid") or "")
    return not record_kb_guid or record_kb_guid == kb_guid


def _parse_editor_doc_guid(raw_id: object) -> tuple[str, str]:
    text = str(raw_id or "")
    if ":" in text:
        return tuple(text.split(":", 1))  # type: ignore[return-value]
    return "", text


HTML_CHARSET_RE = re.compile(
    r"""<meta[^>]+charset\s*=\s*["']?\s*(?P<charset>[A-Za-z0-9._-]+)""",
    re.IGNORECASE,
)
HTML_CONTENT_TYPE_RE = re.compile(
    r"""<meta[^>]+content\s*=\s*["'][^"']*charset\s*=\s*(?P<charset>[A-Za-z0-9._-]+)""",
    re.IGNORECASE,
)


def _normalize_declared_charset(charset: str) -> str:
    normalized = charset.strip().strip(";").strip("\"'").lower()
    if normalized in {"gbk", "gb2312", "gb18030"}:
        return "gb18030"
    if normalized in {"utf8", "utf-8"}:
        return "utf-8"
    return normalized


def _extract_declared_charset(payload: bytes) -> str | None:
    head = payload[:4096].decode("ascii", errors="ignore")
    for pattern in (HTML_CHARSET_RE, HTML_CONTENT_TYPE_RE):
        match = pattern.search(head)
        if match:
            charset = _normalize_declared_charset(match.group("charset"))
            if charset:
                return charset
    return None


def _decode_html_payload(payload: bytes) -> str:
    if payload.startswith(codecs.BOM_UTF8):
        return payload.decode("utf-8-sig")
    if payload.startswith(codecs.BOM_UTF16_LE) or payload.startswith(codecs.BOM_UTF16_BE):
        return payload.decode("utf-16")
    if payload.startswith(codecs.BOM_UTF32_LE) or payload.startswith(codecs.BOM_UTF32_BE):
        return payload.decode("utf-32")

    candidate_encodings: list[str] = []
    declared_charset = _extract_declared_charset(payload)
    if declared_charset:
        candidate_encodings.append(declared_charset)
    candidate_encodings.extend(["utf-8", "gb18030"])

    tried: set[str] = set()
    for encoding in candidate_encodings:
        if encoding in tried:
            continue
        tried.add(encoding)
        try:
            return payload.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode(candidate_encodings[0] if candidate_encodings else "utf-8", errors="replace")


def _collect_note_bodies_from_data(
    *,
    data_records: Sequence[Mapping[str, object]],
    kb_guid: str,
    doc_guids: set[str] | None = None,
) -> tuple[dict[str, NoteBody], dict[str, bytes], dict[str, bytes]]:
    body_by_doc: dict[str, NoteBody] = {}
    resource_bytes_by_key: dict[str, bytes] = {}
    attachment_bytes_by_key: dict[str, bytes] = {}

    for record in data_records:
        if not _matches_kb(record, kb_guid):
            continue
        doc_guid = str(record.get("docGuid") or "")
        if doc_guids is not None and doc_guid not in doc_guids:
            continue
        data_id = str(record.get("dataId") or "")
        data_type = str(record.get("dataType") or "")
        payload = record.get("data")

        if data_type == "html" and isinstance(payload, (bytes, bytearray)) and payload:
            body_by_doc[doc_guid] = NoteBody(html=_decode_html_payload(bytes(payload)))
        elif data_type == "resource" and isinstance(payload, (bytes, bytearray)) and payload:
            resource_bytes_by_key[make_resource_key(doc_guid, data_id)] = bytes(payload)
        elif data_type == "attachment" and isinstance(payload, (bytes, bytearray)) and payload:
            attachment_bytes_by_key[make_attachment_key(doc_guid, data_id)] = bytes(payload)

    return body_by_doc, resource_bytes_by_key, attachment_bytes_by_key


def _stream_editor_bodies(
    *,
    source: WizRecordSource,
    kb_guid: str,
    live_doc_guids: set[str],
) -> dict[str, NoteBody]:
    bodies: dict[str, NoteBody] = {}
    remaining = set(live_doc_guids)
    for record in source.iter_store_values("wiz-editor-ot", "docs", skip_bad=True):
        if not remaining:
            break
        raw_kb_guid, doc_guid = _parse_editor_doc_guid(record.get("id"))
        if not doc_guid or doc_guid not in remaining:
            continue
        if raw_kb_guid and raw_kb_guid != kb_guid:
            continue

        payload = record.get("data")
        if not isinstance(payload, (bytes, bytearray)):
            continue
        rendered = render_collaboration_payload(payload, doc_guid=doc_guid)
        if rendered.markdown:
            bodies[doc_guid] = NoteBody(
                markdown=rendered.markdown,
                generated_assets=rendered.generated_assets,
                metadata=rendered.metadata,
            )
            remaining.discard(doc_guid)
    return bodies


def scan_local_wiz(
    *,
    leveldb_dir: Path | None = None,
    blob_dir: Path | None = None,
    source: WizRecordSource | None = None,
) -> Inventory:
    if source is None:
        source = IndexedDbWizSource(
            leveldb_dir=leveldb_dir or default_leveldb_dir(),
            blob_dir=blob_dir or default_blob_dir(),
        )

    metadata_inventory = scan_local_wiz_metadata(
        leveldb_dir=leveldb_dir,
        blob_dir=blob_dir,
        source=source,
    )
    return load_local_note_payloads(
        metadata_inventory=metadata_inventory,
        doc_guids={note.doc_guid for note in metadata_inventory.notes},
        leveldb_dir=leveldb_dir,
        blob_dir=blob_dir,
        source=source,
    )


def scan_local_wiz_metadata(
    *,
    leveldb_dir: Path | None = None,
    blob_dir: Path | None = None,
    source: WizRecordSource | None = None,
) -> Inventory:
    if source is None:
        source = IndexedDbWizSource(
            leveldb_dir=leveldb_dir or default_leveldb_dir(),
            blob_dir=blob_dir or default_blob_dir(),
        )

    account, kb, user_db, kb_guid = _load_account_context(source)

    folders = [folder for folder in source.iter_store_values(user_db, "folders") if _matches_kb(folder, kb_guid)]
    docs = [doc for doc in source.iter_store_values(user_db, "docs") if _matches_kb(doc, kb_guid)]
    attachments = [
        attachment
        for attachment in source.iter_store_values(user_db, "attachments")
        if _matches_kb(attachment, kb_guid)
    ]

    kb_record = dict(kb)
    kb_record.setdefault("displayName", account.get("displayName"))
    kb_record.setdefault("userId", account.get("userId"))
    return build_inventory_from_records(
        kb=kb_record,
        folders=folders,
        docs=docs,
        attachments=attachments,
        body_by_doc={},
        resource_bytes_by_key={},
        attachment_bytes_by_key={},
    )


def load_local_note_payloads(
    *,
    metadata_inventory: Inventory,
    doc_guids: set[str] | None = None,
    leveldb_dir: Path | None = None,
    blob_dir: Path | None = None,
    source: WizRecordSource | None = None,
    account_context: tuple | None = None,
) -> Inventory:
    if source is None:
        source = IndexedDbWizSource(
            leveldb_dir=leveldb_dir or default_leveldb_dir(),
            blob_dir=blob_dir or default_blob_dir(),
        )

    if not metadata_inventory.notes:
        return metadata_inventory

    if account_context is not None:
        _, _, user_db, kb_guid = account_context
    else:
        _, _, user_db, kb_guid = _load_account_context(source)
    target_doc_guids = set(doc_guids) if doc_guids is not None else {note.doc_guid for note in metadata_inventory.notes}
    if not target_doc_guids:
        return metadata_inventory

    data_records = [
        record
        for record in source.iter_store_values(user_db, "data")
        if _matches_kb(record, kb_guid) and str(record.get("docGuid") or "") in target_doc_guids
    ]

    body_by_doc, resource_bytes_by_key, attachment_bytes_by_key = _collect_note_bodies_from_data(
        data_records=data_records,
        kb_guid=kb_guid,
        doc_guids=target_doc_guids,
    )
    body_by_doc.update(
        _stream_editor_bodies(
            source=source,
            kb_guid=kb_guid,
            live_doc_guids=target_doc_guids,
        )
    )

    notes = [
        replace(note, body=body_by_doc[note.doc_guid]) if note.doc_guid in body_by_doc else note
        for note in metadata_inventory.notes
    ]
    merged_resources = dict(metadata_inventory.resource_bytes_by_key)
    merged_resources.update(resource_bytes_by_key)
    merged_attachments = dict(metadata_inventory.attachment_bytes_by_key)
    merged_attachments.update(attachment_bytes_by_key)
    return Inventory(
        notes=tuple(notes),
        resource_bytes_by_key=merged_resources,
        attachment_bytes_by_key=merged_attachments,
    )


def summarize_inventory(inventory: Inventory) -> dict[str, object]:
    note_types = Counter(note.note_type or "<empty>" for note in inventory.notes)
    return {
        "summary": {
            "total_notes": len(inventory.notes),
            "notes_with_body": sum(1 for note in inventory.notes if note.body.has_meaningful_content),
            "total_attachments": sum(len(note.attachments) for note in inventory.notes),
            "cached_resources": inventory.resource_count,
            "cached_attachments": inventory.attachment_count,
        },
        "note_types": dict(sorted(note_types.items())),
    }


__all__ = [
    "IndexedDbWizSource",
    "NoteBody",
    "build_inventory_from_records",
    "load_local_note_payloads",
    "scan_local_wiz",
    "scan_local_wiz_metadata",
    "summarize_inventory",
]
