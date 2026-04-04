from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import re
from typing import Mapping


PLACEHOLDER_BODY_MARKERS = (
    "当前客户端版本较低，无法编辑协作笔记",
    "请升级客户端",
    "The current client version is too low to edit collaborative notes",
    "Please click the button to upgrade the client",
    "升级客户端",
    "note-plus/note/",
)


def _is_placeholder_note_text(text: str) -> bool:
    normalized = text.strip()
    if not normalized:
        return False

    marker_hits = sum(1 for marker in PLACEHOLDER_BODY_MARKERS if marker in normalized)
    if marker_hits >= 2:
        return True

    return bool(
        re.search(r"https://as\.wiz\.cn/note-plus/note/[^/\s]+/[^)\s<]+", normalized)
        and (
            "协作笔记" in normalized
            or "collaborative notes" in normalized.lower()
            or "upgrade the client" in normalized.lower()
        )
    )


@dataclass(frozen=True)
class GeneratedAsset:
    key: str
    payload: bytes


@dataclass(frozen=True)
class BodyMetadata:
    source_text_length: int = 0
    collaboration_table_count: int = 0
    collaboration_drawio_count: int = 0
    unsupported_block_types: tuple[str, ...] = ()


@dataclass(frozen=True)
class NoteBody:
    markdown: str | None = None
    html: str | None = None
    generated_assets: tuple[GeneratedAsset, ...] = ()
    metadata: BodyMetadata = field(default_factory=BodyMetadata)

    @property
    def has_content(self) -> bool:
        return bool(self.markdown or self.html)

    @property
    def is_placeholder(self) -> bool:
        text = "\n".join(part for part in (self.markdown, self.html) if part)
        return _is_placeholder_note_text(text)

    @property
    def has_meaningful_content(self) -> bool:
        return self.has_content and not self.is_placeholder


@dataclass(frozen=True)
class AttachmentRecord:
    att_guid: str
    doc_guid: str
    name: str
    size: int = 0


@dataclass(frozen=True)
class WizNote:
    kb_name: str
    kb_guid: str
    doc_guid: str
    title: str
    folder_parts: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    note_type: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    abstract: str | None = None
    body: NoteBody = field(default_factory=NoteBody)
    attachments: tuple[AttachmentRecord, ...] = ()


@dataclass(frozen=True)
class NoteForExport:
    kb_name: str
    kb_guid: str
    doc_guid: str
    title: str
    folder_parts: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    note_type: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    body_markdown: str | None = None
    body_html: str | None = None
    attachments: tuple[AttachmentRecord, ...] = ()

    @classmethod
    def from_wiz_note(cls, note: WizNote) -> "NoteForExport":
        body_markdown = note.body.markdown if note.body.has_meaningful_content else None
        body_html = note.body.html if note.body.has_meaningful_content else None
        return cls(
            kb_name=note.kb_name,
            kb_guid=note.kb_guid,
            doc_guid=note.doc_guid,
            title=note.title,
            folder_parts=note.folder_parts,
            tags=note.tags,
            note_type=note.note_type,
            created_at=note.created_at,
            updated_at=note.updated_at,
            body_markdown=body_markdown,
            body_html=body_html,
            attachments=note.attachments,
        )


@dataclass(frozen=True)
class Inventory:
    notes: tuple[WizNote, ...]
    resource_bytes_by_key: Mapping[str, bytes] = field(default_factory=dict)
    attachment_bytes_by_key: Mapping[str, bytes] = field(default_factory=dict)

    @property
    def resource_count(self) -> int:
        return len(self.resource_bytes_by_key)

    @property
    def attachment_count(self) -> int:
        return len(self.attachment_bytes_by_key)
