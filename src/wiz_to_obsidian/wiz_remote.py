from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass
import json
from typing import Any
import uuid
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .markdown_export import render_collaboration_payload
from .models import AttachmentRecord, NoteBody, WizNote


def _strip_trailing_slash(value: str) -> str:
    return value.rstrip("/")


def _load_json_payload(payload: bytes) -> Mapping[str, object] | None:
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, Mapping) else None


def _walk_mappings(value: object):
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
            if str(key).lower() in names and isinstance(value, str) and value:
                return value
    return None


def _payload_is_success(payload: Mapping[str, object]) -> bool:
    code = payload.get("returnCode")
    return code in (None, 0, 200, "0", "200")


def _is_placeholder_resource_payload(payload: bytes | None) -> bool:
    return bool(payload) and len(payload) == 807 and payload.startswith(b"GIF89a\x01\x00\x01\x00")


def _load_json_message(payload: str | bytes | bytearray) -> Mapping[str, Any] | None:
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, Mapping) else None
    return _load_json_payload(bytes(payload))


@dataclass(frozen=True)
class RemoteWizConfig:
    account_server_url: str = "https://as.wiz.cn"
    ks_server_url: str | None = None
    user_id: str | None = None
    password: str | None = None
    token: str | None = None
    auto_login_param: str | None = None
    timeout_seconds: int = 30


class RemoteWizClient:
    def __init__(self, config: RemoteWizConfig) -> None:
        self._config = config
        self._account_server_url = _strip_trailing_slash(config.account_server_url)
        self._ks_server_url = _strip_trailing_slash(config.ks_server_url or "https://ks.wiz.cn")
        self._token = config.token
        self._editor_tokens_by_doc_guid: dict[str, str] = {}
        self._editor_resource_tokens_by_doc_guid: dict[str, str] = {}

    def close(self) -> None:
        return None

    def _ensure_auth(self) -> None:
        if self._token:
            return
        if self._config.auto_login_param:
            payload = self._request_json(
                f"{self._account_server_url}/as/user/login/auto",
                headers={"wiz-auto-login-param": self._config.auto_login_param},
            )
            self._apply_login_payload(payload)
            if self._token:
                return
        if self._config.user_id and self._config.password:
            payload = self._request_json(
                f"{self._account_server_url}/as/user/login",
                method="POST",
                payload={
                    "userId": self._config.user_id,
                    "password": self._config.password,
                    "autoLogin": True,
                    "deviceId": str(uuid.uuid4()),
                },
            )
            self._apply_login_payload(payload)
            if self._token:
                return
        raise RuntimeError("Unable to authenticate with Wiz remote services")

    def _apply_login_payload(self, payload: Mapping[str, object]) -> None:
        if not _payload_is_success(payload):
            raise RuntimeError(str(payload.get("returnMessage") or "Wiz login failed"))

        token = _first_string_field(payload, "token", "wizToken", "xWizToken")
        if token:
            self._token = token
        ks_server = _first_string_field(payload, "kbServer", "ksServer", "ksUrl", "serverURL", "serverUrl")
        if ks_server and "ks" in ks_server.lower():
            self._ks_server_url = _strip_trailing_slash(ks_server)

    def _request_raw(
        self,
        url: str,
        *,
        method: str = "GET",
        payload: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
        require_auth: bool = False,
    ) -> bytes:
        request_headers = {"accept": "application/json, text/plain, */*"}
        if headers:
            request_headers.update(headers)
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            request_headers["content-type"] = "application/json"
        if require_auth:
            self._ensure_auth()
            request_headers["x-wiz-token"] = self._token or ""

        request = Request(url, data=data, headers=request_headers, method=method)
        try:
            with urlopen(request, timeout=self._config.timeout_seconds) as response:
                return response.read()
        except HTTPError as exc:
            body = exc.read()
            payload = _load_json_payload(body)
            if payload is not None:
                message = str(payload.get("returnMessage") or payload.get("message") or exc)
                raise RuntimeError(message) from exc
            raise

    def _request_json(
        self,
        url: str,
        *,
        method: str = "GET",
        payload: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
        require_auth: bool = False,
    ) -> Mapping[str, object]:
        response_body = self._request_raw(
            url,
            method=method,
            payload=payload,
            headers=headers,
            require_auth=require_auth,
        )
        response_payload = _load_json_payload(response_body)
        if response_payload is None:
            raise RuntimeError(f"Wiz returned a non-JSON response for {url}")
        return response_payload

    def fetch_note_body(self, note: WizNote) -> NoteBody:
        payload = self._request_json(
            f"{self._ks_server_url}/ks/note/download/{note.kb_guid}/{note.doc_guid}?downloadInfo=1&downloadData=1",
            require_auth=True,
        )
        if not _payload_is_success(payload):
            return NoteBody()

        note_data = next(
            (
                mapping[key]
                for mapping in _walk_mappings(payload)
                for key in mapping
                if str(key).lower() == "notedata"
            ),
            None,
        )
        if isinstance(note_data, (Mapping, str, bytes)):
            rendered = render_collaboration_payload(note_data, doc_guid=note.doc_guid)
            if rendered.markdown:
                return NoteBody(
                    markdown=rendered.markdown,
                    generated_assets=rendered.generated_assets,
                    metadata=rendered.metadata,
                )

        html = _first_string_field(payload, "html", "noteHtml")
        body = NoteBody(html=html) if html else NoteBody()
        if body.has_meaningful_content:
            return body

        if note.note_type == "collaboration":
            snapshot = self._fetch_editor_snapshot(note)
            if isinstance(snapshot, (Mapping, str, bytes)):
                rendered = render_collaboration_payload(snapshot, doc_guid=note.doc_guid)
                if rendered.markdown:
                    return NoteBody(
                        markdown=rendered.markdown,
                        generated_assets=rendered.generated_assets,
                        metadata=rendered.metadata,
                    )

        return NoteBody()

    def fetch_resource(self, note: WizNote, resource_name: str) -> bytes | None:
        if note.note_type == "collaboration":
            payload = self._fetch_editor_resource(note, resource_name)
            if payload is not None:
                return payload

        payload = self._fetch_object(note, obj_type="resource", obj_id=resource_name)
        if payload is not None and not _is_placeholder_resource_payload(payload):
            return payload

        if note.note_type != "collaboration":
            return self._fetch_editor_resource(note, resource_name)
        return None

    def fetch_attachment(self, note: WizNote, attachment: AttachmentRecord) -> bytes | None:
        for obj_id in (attachment.att_guid, attachment.name):
            if not obj_id:
                continue
            payload = self._fetch_object(note, obj_type="attachment", obj_id=obj_id)
            if payload is not None:
                return payload
        return None

    def _fetch_object(self, note: WizNote, *, obj_type: str, obj_id: str) -> bytes | None:
        encoded_obj_id = quote(obj_id, safe="")
        response_body = self._request_raw(
            f"{self._ks_server_url}/ks/object/download/{note.kb_guid}/{note.doc_guid}?objType={obj_type}&objId={encoded_obj_id}",
            require_auth=True,
        )
        payload = _load_json_payload(response_body)
        if payload is not None and not _payload_is_success(payload):
            return None
        return response_body

    def _editor_base_url(self, note: WizNote) -> str:
        return f"{self._ks_server_url}/editor/{note.kb_guid}/{note.doc_guid}"

    def _editor_websocket_url(self, note: WizNote) -> str:
        return self._editor_base_url(note).replace("https://", "wss://").replace("http://", "ws://")

    def _create_editor_websocket(self, url: str):
        try:
            import websocket
        except ImportError as exc:
            raise RuntimeError("websocket-client is required to fetch collaboration note snapshots") from exc
        return websocket.create_connection(url, timeout=self._config.timeout_seconds)

    def _fetch_editor_token(self, note: WizNote, *, force_refresh: bool = False) -> str | None:
        if not force_refresh:
            cached_token = self._editor_tokens_by_doc_guid.get(note.doc_guid)
            if cached_token:
                return cached_token

        payload = self._request_json(
            f"{self._ks_server_url}/ks/note/{note.kb_guid}/{note.doc_guid}/tokens",
            method="POST",
            require_auth=True,
        )
        if not _payload_is_success(payload):
            return None

        editor_token = _first_string_field(payload, "editorToken")
        if editor_token:
            self._editor_tokens_by_doc_guid[note.doc_guid] = editor_token
        return editor_token

    def _fetch_editor_resource_token(self, note: WizNote, *, force_refresh: bool = False) -> str | None:
        if not force_refresh:
            cached_token = self._editor_resource_tokens_by_doc_guid.get(note.doc_guid)
            if cached_token:
                return cached_token

        editor_token = self._fetch_editor_token(note, force_refresh=force_refresh)
        if not editor_token:
            return None

        editor_base_url = self._editor_base_url(note)
        response_body = self._request_raw(
            f"{editor_base_url}/auth",
            headers={
                "x-live-editor-token": editor_token,
                "x-live-editor-base-url": base64.b64encode(editor_base_url.encode("utf-8")).decode("ascii"),
            },
        )
        payload = _load_json_payload(response_body)
        if payload is None:
            return None

        resource_token = _first_string_field(payload, "read")
        if resource_token:
            self._editor_resource_tokens_by_doc_guid[note.doc_guid] = resource_token
        return resource_token

    def _fetch_editor_resource(self, note: WizNote, resource_name: str) -> bytes | None:
        encoded_name = quote(resource_name, safe="")
        for attempt in range(2):
            try:
                resource_token = self._fetch_editor_resource_token(note, force_refresh=attempt > 0)
                if not resource_token:
                    return None
                return self._request_raw(
                    f"{self._editor_base_url(note)}/resources/{encoded_name}?token={resource_token}",
                )
            except (HTTPError, RuntimeError):
                self._editor_tokens_by_doc_guid.pop(note.doc_guid, None)
                self._editor_resource_tokens_by_doc_guid.pop(note.doc_guid, None)
        return None

    def _fetch_editor_snapshot(self, note: WizNote) -> Mapping[str, Any] | str | bytes | None:
        editor_token = self._fetch_editor_token(note)
        if not editor_token:
            return None
        if not self._fetch_editor_resource_token(note):
            return None

        auth_payload = {
            "appId": note.kb_guid,
            "docId": note.doc_guid,
            "userId": "",
            "permission": "r",
            "displayName": "",
            "avatarUrl": "",
            "token": editor_token,
        }
        request_id = 1
        websocket_client = None
        try:
            websocket_client = self._create_editor_websocket(self._editor_websocket_url(note))
            websocket_client.send(json.dumps({"a": "hs", "id": None, "auth": auth_payload}, ensure_ascii=False))

            handshake_complete = False
            while True:
                message = _load_json_message(websocket_client.recv())
                if message is None:
                    continue
                if message.get("error"):
                    return None

                action = str(message.get("a") or "")
                if action == "hs":
                    if handshake_complete:
                        continue
                    handshake_complete = True
                    websocket_client.send(
                        json.dumps(
                            {"a": "nf", "id": request_id, "c": note.kb_guid, "d": note.doc_guid, "v": None},
                            ensure_ascii=False,
                        )
                    )
                    continue
                if action == "nf" and message.get("id") == request_id:
                    snapshot = message.get("data")
                    return snapshot if isinstance(snapshot, (Mapping, str, bytes)) else None
        except Exception:
            return None
        finally:
            if websocket_client is not None:
                try:
                    websocket_client.close()
                except Exception:
                    pass
        return None


__all__ = ["RemoteWizClient", "RemoteWizConfig"]
