from __future__ import annotations

import importlib
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


class HydrationTests(unittest.TestCase):
    @staticmethod
    def _placeholder_gif() -> bytes:
        return b"GIF89a\x01\x00\x01\x00" + (b"\x00" * (807 - 10))

    @staticmethod
    def _placeholder_note_html(doc_guid: str) -> str:
        return (
            "<!DOCTYPE html><html><body>"
            "当前客户端版本较低，无法编辑协作笔记<br>"
            "The current client version is too low to edit collaborative notes<br>"
            f'<a href="https://as.wiz.cn/note-plus/note/kb-1/{doc_guid}">查看笔记</a>'
            "</body></html>"
        )

    def test_hydrate_inventory_fills_missing_bodies_resources_and_attachments(self) -> None:
        models = import_or_fail(self, "wiz_to_obsidian.models")
        hydration = import_or_fail(self, "wiz_to_obsidian.wiz_hydration")

        note = models.WizNote(
            kb_name="Main KB",
            kb_guid="kb-1",
            doc_guid="doc-1",
            title="Roadmap",
            body=models.NoteBody(),
            attachments=(
                models.AttachmentRecord(
                    att_guid="att-1",
                    doc_guid="doc-1",
                    name="spec.pdf",
                    size=4,
                ),
            ),
        )
        inventory = models.Inventory(notes=(note,))

        class FakeClient:
            def fetch_note_body(self, note):
                return models.NoteBody(markdown="# Roadmap\n![](wiz-resource://doc-1/cover.png)")

            def fetch_resource(self, note, resource_name: str):
                self.last_resource_name = resource_name
                return b"img" if resource_name == "cover.png" else None

            def fetch_attachment(self, note, attachment):
                return b"pdf!" if attachment.att_guid == "att-1" else None

        result = hydration.hydrate_inventory(inventory=inventory, client=FakeClient())

        self.assertEqual("# Roadmap\n![](wiz-resource://doc-1/cover.png)", result.inventory.notes[0].body.markdown)
        self.assertEqual(b"img", result.inventory.resource_bytes_by_key["wiz-resource://doc-1/cover.png"])
        self.assertEqual(b"pdf!", result.inventory.attachment_bytes_by_key["wiz-attachment://doc-1/att-1"])
        self.assertEqual(b"pdf!", result.inventory.attachment_bytes_by_key["wiz-attachment://doc-1/spec.pdf"])
        self.assertEqual(1, result.summary["hydrated_notes"])
        self.assertEqual(1, result.summary["hydrated_resources"])
        self.assertEqual(1, result.summary["hydrated_attachments"])

    def test_hydrate_inventory_continues_when_client_fetch_raises(self) -> None:
        models = import_or_fail(self, "wiz_to_obsidian.models")
        hydration = import_or_fail(self, "wiz_to_obsidian.wiz_hydration")

        notes = (
            models.WizNote(
                kb_name="Main KB",
                kb_guid="kb-1",
                doc_guid="doc-fail",
                title="Broken",
                body=models.NoteBody(),
            ),
            models.WizNote(
                kb_name="Main KB",
                kb_guid="kb-1",
                doc_guid="doc-ok",
                title="Healthy",
                body=models.NoteBody(),
            ),
        )
        inventory = models.Inventory(notes=notes)

        class FlakyClient:
            def fetch_note_body(self, note):
                if note.doc_guid == "doc-fail":
                    raise TimeoutError("simulated timeout")
                return models.NoteBody(html="<p>Recovered</p>")

            def fetch_resource(self, note, resource_name: str):
                return None

            def fetch_attachment(self, note, attachment):
                return None

        result = hydration.hydrate_inventory(inventory=inventory, client=FlakyClient())

        note_by_guid = {note.doc_guid: note for note in result.inventory.notes}
        self.assertFalse(note_by_guid["doc-fail"].body.has_content)
        self.assertEqual("<p>Recovered</p>", note_by_guid["doc-ok"].body.html)
        self.assertEqual(1, result.summary["hydrated_notes"])

    def test_hydrate_inventory_reports_progress_for_each_note(self) -> None:
        models = import_or_fail(self, "wiz_to_obsidian.models")
        hydration = import_or_fail(self, "wiz_to_obsidian.wiz_hydration")

        inventory = models.Inventory(
            notes=(
                models.WizNote(
                    kb_name="Main KB",
                    kb_guid="kb-1",
                    doc_guid="doc-1",
                    title="Alpha",
                    body=models.NoteBody(),
                ),
                models.WizNote(
                    kb_name="Main KB",
                    kb_guid="kb-1",
                    doc_guid="doc-2",
                    title="Beta",
                    body=models.NoteBody(),
                ),
            )
        )

        class FakeClient:
            def fetch_note_body(self, note):
                return models.NoteBody(markdown=f"# {note.title}")

            def fetch_resource(self, note, resource_name: str):
                return None

            def fetch_attachment(self, note, attachment):
                return None

        progress_messages: list[str] = []
        hydration.hydrate_inventory(
            inventory=inventory,
            client=FakeClient(),
            progress=progress_messages.append,
        )

        self.assertEqual(
            [
                "1/2 Alpha",
                "2/2 Beta",
            ],
            progress_messages,
        )

    def test_hydrate_inventory_retries_transient_note_body_failure(self) -> None:
        models = import_or_fail(self, "wiz_to_obsidian.models")
        hydration = import_or_fail(self, "wiz_to_obsidian.wiz_hydration")

        note = models.WizNote(
            kb_name="Main KB",
            kb_guid="kb-1",
            doc_guid="doc-retry",
            title="Retry",
            body=models.NoteBody(),
        )
        inventory = models.Inventory(notes=(note,))

        class FlakyClient:
            def __init__(self) -> None:
                self.calls = 0

            def fetch_note_body(self, note):
                self.calls += 1
                if self.calls == 1:
                    raise TimeoutError("temporary")
                return models.NoteBody(html="<p>Recovered</p>")

            def fetch_resource(self, note, resource_name: str):
                return None

            def fetch_attachment(self, note, attachment):
                return None

        client = FlakyClient()
        result = hydration.hydrate_inventory(inventory=inventory, client=client)

        self.assertEqual("<p>Recovered</p>", result.inventory.notes[0].body.html)
        self.assertEqual(2, client.calls)
        self.assertEqual(1, result.summary["hydrated_notes"])

    def test_hydrate_inventory_retries_missing_bodies_in_second_pass(self) -> None:
        models = import_or_fail(self, "wiz_to_obsidian.models")
        hydration = import_or_fail(self, "wiz_to_obsidian.wiz_hydration")

        note = models.WizNote(
            kb_name="Main KB",
            kb_guid="kb-1",
            doc_guid="doc-late-retry",
            title="Late Retry",
            body=models.NoteBody(),
        )
        inventory = models.Inventory(notes=(note,))

        class FlakyClient:
            def __init__(self) -> None:
                self.calls = 0

            def fetch_note_body(self, note):
                self.calls += 1
                if self.calls <= 2:
                    raise TimeoutError("temporary")
                return models.NoteBody(markdown="# Recovered Later")

            def fetch_resource(self, note, resource_name: str):
                return None

            def fetch_attachment(self, note, attachment):
                return None

        client = FlakyClient()
        result = hydration.hydrate_inventory(inventory=inventory, client=client)

        self.assertEqual("# Recovered Later", result.inventory.notes[0].body.markdown)
        self.assertEqual(3, client.calls)
        self.assertEqual(1, result.summary["hydrated_notes"])

    def test_hydrate_inventory_retries_transient_resource_failure(self) -> None:
        models = import_or_fail(self, "wiz_to_obsidian.models")
        hydration = import_or_fail(self, "wiz_to_obsidian.wiz_hydration")

        note = models.WizNote(
            kb_name="Main KB",
            kb_guid="kb-1",
            doc_guid="doc-retry",
            title="Retry",
            body=models.NoteBody(markdown="![](wiz-resource://doc-retry/cover.png)"),
        )
        inventory = models.Inventory(notes=(note,))

        class FlakyClient:
            def __init__(self) -> None:
                self.calls = 0

            def fetch_note_body(self, note):
                return models.NoteBody()

            def fetch_resource(self, note, resource_name: str):
                self.calls += 1
                if self.calls == 1:
                    raise TimeoutError("temporary")
                return b"img"

            def fetch_attachment(self, note, attachment):
                return None

        client = FlakyClient()
        result = hydration.hydrate_inventory(inventory=inventory, client=client)

        self.assertEqual(b"img", result.inventory.resource_bytes_by_key["wiz-resource://doc-retry/cover.png"])
        self.assertEqual(2, client.calls)
        self.assertEqual(1, result.summary["hydrated_resources"])

    def test_composite_client_skips_placeholder_resource_payloads(self) -> None:
        models = import_or_fail(self, "wiz_to_obsidian.models")
        hydration = import_or_fail(self, "wiz_to_obsidian.wiz_hydration")

        note = models.WizNote(
            kb_name="Main KB",
            kb_guid="kb-1",
            doc_guid="doc-1",
            title="Roadmap",
            note_type="collaboration",
        )

        class PlaceholderClient:
            def fetch_note_body(self, note):
                return models.NoteBody()

            def fetch_resource(self, note, resource_name: str):
                return HydrationTests._placeholder_gif()

            def fetch_attachment(self, note, attachment):
                return None

        class RealClient:
            def fetch_note_body(self, note):
                return models.NoteBody()

            def fetch_resource(self, note, resource_name: str):
                return b"\x89PNG\r\n\x1a\nactual"

            def fetch_attachment(self, note, attachment):
                return None

        client = hydration.CompositeWizContentClient((PlaceholderClient(), RealClient()))

        self.assertEqual(b"\x89PNG\r\n\x1a\nactual", client.fetch_resource(note, "cover.png"))

    def test_composite_client_skips_placeholder_note_bodies(self) -> None:
        models = import_or_fail(self, "wiz_to_obsidian.models")
        hydration = import_or_fail(self, "wiz_to_obsidian.wiz_hydration")

        note = models.WizNote(
            kb_name="Main KB",
            kb_guid="kb-1",
            doc_guid="doc-1",
            title="Roadmap",
            note_type="collaboration",
        )

        class PlaceholderClient:
            def fetch_note_body(self, note):
                return models.NoteBody(html=HydrationTests._placeholder_note_html(note.doc_guid))

            def fetch_resource(self, note, resource_name: str):
                return None

            def fetch_attachment(self, note, attachment):
                return None

        class RealClient:
            def fetch_note_body(self, note):
                return models.NoteBody(markdown="# Actual Body")

            def fetch_resource(self, note, resource_name: str):
                return None

            def fetch_attachment(self, note, attachment):
                return None

        client = hydration.CompositeWizContentClient((PlaceholderClient(), RealClient()))

        self.assertEqual("# Actual Body", client.fetch_note_body(note).markdown)

    def test_hydrate_inventory_does_not_store_placeholder_resource_payloads(self) -> None:
        models = import_or_fail(self, "wiz_to_obsidian.models")
        hydration = import_or_fail(self, "wiz_to_obsidian.wiz_hydration")

        note = models.WizNote(
            kb_name="Main KB",
            kb_guid="kb-1",
            doc_guid="doc-1",
            title="Roadmap",
            body=models.NoteBody(markdown="# Roadmap\n![](wiz-resource://doc-1/cover.png)"),
            note_type="collaboration",
        )
        inventory = models.Inventory(notes=(note,))

        class PlaceholderOnlyClient:
            def fetch_note_body(self, note):
                return models.NoteBody()

            def fetch_resource(self, note, resource_name: str):
                return HydrationTests._placeholder_gif()

            def fetch_attachment(self, note, attachment):
                return None

        result = hydration.hydrate_inventory(inventory=inventory, client=PlaceholderOnlyClient())

        self.assertNotIn("wiz-resource://doc-1/cover.png", result.inventory.resource_bytes_by_key)
        self.assertEqual(0, result.summary["hydrated_resources"])
        self.assertEqual(1, result.summary["hydration_failures"])

    def test_hydrate_inventory_replaces_placeholder_note_body(self) -> None:
        models = import_or_fail(self, "wiz_to_obsidian.models")
        hydration = import_or_fail(self, "wiz_to_obsidian.wiz_hydration")

        note = models.WizNote(
            kb_name="Main KB",
            kb_guid="kb-1",
            doc_guid="doc-1",
            title="Roadmap",
            note_type="collaboration",
            body=models.NoteBody(html=self._placeholder_note_html("doc-1")),
        )
        inventory = models.Inventory(notes=(note,))

        class FakeClient:
            def fetch_note_body(self, note):
                return models.NoteBody(markdown="# Recovered Body")

            def fetch_resource(self, note, resource_name: str):
                return None

            def fetch_attachment(self, note, attachment):
                return None

        result = hydration.hydrate_inventory(inventory=inventory, client=FakeClient())

        self.assertEqual("# Recovered Body", result.inventory.notes[0].body.markdown)
        self.assertEqual(1, result.summary["hydrated_notes"])

    def test_hydrate_inventory_fetches_markdown_resource_paths_from_hydrated_html(self) -> None:
        models = import_or_fail(self, "wiz_to_obsidian.models")
        hydration = import_or_fail(self, "wiz_to_obsidian.wiz_hydration")

        note = models.WizNote(
            kb_name="Main KB",
            kb_guid="kb-1",
            doc_guid="doc-1",
            title="Hydrated",
            body=models.NoteBody(),
        )
        inventory = models.Inventory(notes=(note,))

        class FakeClient:
            def __init__(self) -> None:
                self.requested_resources: list[str] = []

            def fetch_note_body(self, note):
                return models.NoteBody(
                    html=(
                        "<!doctype html><html><body><pre># Heading\n\n"
                        "![first](index_files/cover.png)\n"
                        "![second](doc-1_files/diagram.png)\n"
                        "</pre></body></html>"
                    )
                )

            def fetch_resource(self, note, resource_name: str):
                self.requested_resources.append(resource_name)
                return resource_name.encode("utf-8")

            def fetch_attachment(self, note, attachment):
                return None

        client = FakeClient()
        result = hydration.hydrate_inventory(inventory=inventory, client=client)

        self.assertEqual({"cover.png", "diagram.png"}, set(client.requested_resources))
        self.assertEqual(
            b"cover.png",
            result.inventory.resource_bytes_by_key["wiz-resource://doc-1/cover.png"],
        )
        self.assertEqual(
            b"diagram.png",
            result.inventory.resource_bytes_by_key["wiz-resource://doc-1/diagram.png"],
        )
        self.assertEqual(2, result.summary["hydrated_resources"])
