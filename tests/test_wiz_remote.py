from __future__ import annotations

import importlib
import json
import unittest
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def import_or_fail(testcase: unittest.TestCase, module_name: str):
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        testcase.fail(f"expected module {module_name!r} to exist: {exc}")


class WizRemoteTests(unittest.TestCase):
    @staticmethod
    def _placeholder_note_html(doc_guid: str) -> str:
        return (
            "<!DOCTYPE html><html><body>"
            "当前客户端版本较低，无法编辑协作笔记<br>"
            "The current client version is too low to edit collaborative notes<br>"
            f'<a href="https://as.wiz.cn/note-plus/note/kb-1/{doc_guid}">查看笔记</a>'
            "</body></html>"
        )

    def test_fetch_note_body_requests_download_flags_as_one(self) -> None:
        models = import_or_fail(self, "wiz_to_obsidian.models")
        remote = import_or_fail(self, "wiz_to_obsidian.wiz_remote")

        note = models.WizNote(
            kb_name="Main KB",
            kb_guid="kb-1",
            doc_guid="doc-1",
            title="Roadmap",
        )

        class FakeRemoteClient(remote.RemoteWizClient):
            def __init__(self) -> None:
                super().__init__(remote.RemoteWizConfig(token="token"))
                self.last_url = None

            def _request_json(self, url: str, **kwargs):
                self.last_url = url
                return {
                    "returnCode": 200,
                    "html": "<p>Hello</p>",
                }

        client = FakeRemoteClient()
        body = client.fetch_note_body(note)

        self.assertEqual(
            "https://ks.wiz.cn/ks/note/download/kb-1/doc-1?downloadInfo=1&downloadData=1",
            client.last_url,
        )
        self.assertEqual("<p>Hello</p>", body.html)

    def test_fetch_resource_uses_editor_auth_for_collaboration_notes(self) -> None:
        models = import_or_fail(self, "wiz_to_obsidian.models")
        remote = import_or_fail(self, "wiz_to_obsidian.wiz_remote")

        note = models.WizNote(
            kb_name="Main KB",
            kb_guid="kb-1",
            doc_guid="doc-1",
            title="Roadmap",
            note_type="collaboration",
        )

        class FakeRemoteClient(remote.RemoteWizClient):
            def __init__(self) -> None:
                super().__init__(remote.RemoteWizConfig(token="account-token", ks_server_url="https://vipkshttps7.wiz.cn"))
                self.json_calls = []
                self.raw_calls = []

            def _request_json(self, url: str, **kwargs):
                self.json_calls.append((url, kwargs))
                if url.endswith("/ks/note/kb-1/doc-1/tokens"):
                    return {
                        "returnCode": 200,
                        "result": {
                            "editorToken": "editor-token",
                        },
                    }
                raise AssertionError(f"unexpected JSON request: {url}")

            def _request_raw(self, url: str, **kwargs):
                self.raw_calls.append((url, kwargs))
                if url.endswith("/editor/kb-1/doc-1/auth"):
                    return b'{\"read\":\"resource-token\",\"user\":\"user-token\"}'
                if url.endswith("/editor/kb-1/doc-1/resources/cover.png?token=resource-token"):
                    return b"\x89PNG\r\n\x1a\nactual"
                raise AssertionError(f"unexpected raw request: {url}")

        client = FakeRemoteClient()
        payload = client.fetch_resource(note, "cover.png")

        self.assertEqual(b"\x89PNG\r\n\x1a\nactual", payload)
        self.assertEqual(
            "https://vipkshttps7.wiz.cn/ks/note/kb-1/doc-1/tokens",
            client.json_calls[0][0],
        )
        self.assertEqual(
            "POST",
            client.json_calls[0][1]["method"],
        )
        self.assertTrue(client.json_calls[0][1]["require_auth"])
        self.assertEqual(
            "https://vipkshttps7.wiz.cn/editor/kb-1/doc-1/auth",
            client.raw_calls[0][0],
        )
        self.assertEqual("editor-token", client.raw_calls[0][1]["headers"]["x-live-editor-token"])
        self.assertEqual(
            "https://vipkshttps7.wiz.cn/editor/kb-1/doc-1/resources/cover.png?token=resource-token",
            client.raw_calls[1][0],
        )

    def test_fetch_note_body_uses_editor_snapshot_for_placeholder_collaboration_html(self) -> None:
        models = import_or_fail(self, "wiz_to_obsidian.models")
        remote = import_or_fail(self, "wiz_to_obsidian.wiz_remote")

        note = models.WizNote(
            kb_name="Main KB",
            kb_guid="kb-1",
            doc_guid="doc-1",
            title="Roadmap",
            note_type="collaboration",
        )

        class FakeWebSocket:
            def __init__(self) -> None:
                self.sent_messages = []
                self.closed = False
                self._responses = [
                    json.dumps({"a": "hs", "protocol": 1, "type": "http://sharejs.org/types/JSONv0", "id": "client-1"}),
                    json.dumps(
                        {
                            "a": "nf",
                            "id": 1,
                            "v": 7,
                            "type": "http://sharejs.org/types/JSONv0",
                            "data": {
                                "blocks": [
                                    {
                                        "id": "h1",
                                        "type": "text",
                                        "heading": 1,
                                        "text": [{"insert": "Roadmap"}],
                                    }
                                ]
                            },
                        }
                    ),
                ]

            def send(self, payload: str) -> None:
                self.sent_messages.append(json.loads(payload))

            def recv(self) -> str:
                if not self._responses:
                    raise AssertionError("unexpected websocket recv")
                return self._responses.pop(0)

            def close(self) -> None:
                self.closed = True

        class FakeRemoteClient(remote.RemoteWizClient):
            def __init__(self) -> None:
                super().__init__(remote.RemoteWizConfig(token="account-token", ks_server_url="https://vipkshttps7.wiz.cn"))
                self.websocket = FakeWebSocket()

            def _request_json(self, url: str, **kwargs):
                if url.endswith("/ks/note/download/kb-1/doc-1?downloadInfo=1&downloadData=1"):
                    return {
                        "returnCode": 200,
                        "html": WizRemoteTests._placeholder_note_html("doc-1"),
                    }
                if url.endswith("/ks/note/kb-1/doc-1/tokens"):
                    return {
                        "returnCode": 200,
                        "result": {
                            "editorToken": "editor-token",
                        },
                    }
                raise AssertionError(f"unexpected JSON request: {url}")

            def _request_raw(self, url: str, **kwargs):
                if url.endswith("/editor/kb-1/doc-1/auth"):
                    return b'{"read":"resource-token","user":"user-token"}'
                raise AssertionError(f"unexpected raw request: {url}")

            def _create_editor_websocket(self, url: str):
                self.websocket_url = url
                return self.websocket

        client = FakeRemoteClient()
        body = client.fetch_note_body(note)

        self.assertEqual("# Roadmap", body.markdown)
        self.assertEqual("wss://vipkshttps7.wiz.cn/editor/kb-1/doc-1", client.websocket_url)
        self.assertEqual(
            {
                "a": "hs",
                "id": None,
                "auth": {
                    "appId": "kb-1",
                    "docId": "doc-1",
                    "userId": "",
                    "permission": "r",
                    "displayName": "",
                    "avatarUrl": "",
                    "token": "editor-token",
                },
            },
            client.websocket.sent_messages[0],
        )
        self.assertEqual(
            {"a": "nf", "id": 1, "c": "kb-1", "d": "doc-1", "v": None},
            client.websocket.sent_messages[1],
        )
        self.assertTrue(client.websocket.closed)


if __name__ == "__main__":
    unittest.main()
