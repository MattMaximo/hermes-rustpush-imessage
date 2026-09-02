from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if hermes_root := os.environ.get("HERMES_AGENT_ROOT"):
    sys.path.insert(0, str(Path(hermes_root).expanduser().resolve()))

try:
    from gateway.config import Platform  # noqa: E402
except ImportError as exc:  # pragma: no cover - depends on external checkout
    raise unittest.SkipTest(
        "set HERMES_AGENT_ROOT to a Hermes Agent checkout to run adapter contract tests"
    ) from exc

from adapter import RustPushIMessageAdapter, validate_config  # noqa: E402


class AdapterContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        socket_path = str(Path(self.tmp.name) / "bridge.sock")
        config = SimpleNamespace(extra={"socket_path": socket_path})
        self.adapter = RustPushIMessageAdapter(config, _platform=Platform.LOCAL)
        self.calls: list[tuple[str, dict]] = []

        async def request(operation: str, **fields):
            self.calls.append((operation, fields))
            if operation in {"send", "reply", "send_file", "reply_file", "edit", "undo_send"}:
                return {"native_id": f"id-{operation}"}
            return {"ok": True}

        self.adapter._request = request  # type: ignore[method-assign]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def test_outbound_fields_match_sidecar_protocol(self) -> None:
        sent = await self.adapter.send("mailto:peer@example.com", "hello", reply_to="orig")
        self.assertTrue(sent.success)
        self.assertEqual(
            self.calls[-1],
            (
                "reply",
                {
                    "chat_id": "mailto:peer@example.com",
                    "text": "hello",
                    "reply_to": "orig",
                },
            ),
        )

        with tempfile.NamedTemporaryFile() as attachment:
            result = await self.adapter.send_document(
                "mailto:peer@example.com",
                attachment.name,
                caption="caption",
                file_name="report.bin",
                reply_to="orig",
            )
        self.assertTrue(result.success)
        operation, fields = self.calls[-1]
        self.assertEqual(operation, "reply_file")
        self.assertEqual(fields["caption"], "caption")
        self.assertEqual(fields["filename"], "report.bin")
        self.assertEqual(fields["reply_to"], "orig")
        self.assertNotIn("body", fields)
        self.assertNotIn("message_id", fields)

        await self.adapter.send_typing("mailto:peer@example.com")
        self.assertEqual(self.calls[-1][1]["active"], True)
        await self.adapter.stop_typing("mailto:peer@example.com")
        self.assertEqual(self.calls[-1][1]["active"], False)

        self.adapter._last_inbound["chat"] = "incoming"
        self.assertTrue(await self.adapter.mark_read("chat"))
        self.assertEqual(self.calls[-1][1]["native_id"], "incoming")

        reaction = await self.adapter.add_reaction("chat", "love", "incoming")
        self.assertTrue(reaction["success"])
        self.assertEqual(self.calls[-1][1]["native_id"], "incoming")
        removed = await self.adapter.remove_reaction("chat", "incoming")
        self.assertTrue(removed["success"])
        self.assertEqual(self.calls[-1], ("unreact", {"chat_id": "chat", "native_id": "incoming"}))

        edit = await self.adapter.edit_message("chat", "mine", "revised")
        self.assertTrue(edit.success)
        self.assertEqual(self.calls[-1][1]["native_id"], "mine")
        self.assertTrue(await self.adapter.delete_message("chat", "mine"))
        self.assertEqual(self.calls[-1][1]["native_id"], "mine")

    async def test_connect_accepts_gateway_reconnect_keyword(self) -> None:
        async def ready_supervisor() -> None:
            self.adapter._ready.set()
            await self.adapter._stop.wait()

        self.adapter._connection_supervisor = ready_supervisor  # type: ignore[method-assign]
        self.assertTrue(await self.adapter.connect(is_reconnect=True))
        await self.adapter.disconnect()

    async def test_inbound_message_is_accepted_marked_read_then_acked(self) -> None:
        accepted = []

        async def handle(event):
            accepted.append(event)

        self.adapter.handle_message = handle  # type: ignore[method-assign]
        event = {
            "event_id": "event-1",
            "kind": "message",
            "chat_id": "imessage:" + "1" * 32,
            "native_id": "native-1",
            "sender": "mailto:peer@example.com",
            "participants": ["mailto:peer@example.com"],
            "timestamp_ms": 1_700_000_000_000,
            "received_monotonic_ns": 100,
            "from_me": False,
            "text": "hello",
            "attachments": [],
        }
        await self.adapter._on_event(event)
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0].text, "hello")
        operations = [item[0] for item in self.calls]
        self.assertIn("mark_read", operations)
        self.assertEqual(operations[-1], "ack")

    async def test_inbound_attachment_materializes_and_removes_object_replacement_text(self) -> None:
        accepted = []
        source = Path(self.tmp.name) / "photo.jpg"
        source.write_bytes(b"jpeg")

        async def handle(event):
            accepted.append(event)

        async def request(operation: str, **fields):
            self.calls.append((operation, fields))
            if operation == "materialize_attachment":
                return {"path": str(source)}
            return {"ok": True}

        self.adapter.handle_message = handle  # type: ignore[method-assign]
        self.adapter._request = request  # type: ignore[method-assign]
        event = {
            "event_id": "event-attachment",
            "kind": "message",
            "chat_id": "imessage:" + "3" * 32,
            "native_id": "native-attachment",
            "sender": "mailto:peer@example.com",
            "participants": ["mailto:peer@example.com"],
            "timestamp_ms": 1_700_000_000_000,
            "received_monotonic_ns": 100,
            "from_me": False,
            "text": "\ufffc",
            "attachments": [
                {
                    "attachment_id": "a" * 32,
                    "filename": "photo.jpg",
                    "mime": "image/jpeg",
                    "size": 4,
                    "available": True,
                }
            ],
        }
        cached = SimpleNamespace(path="/private/cache/photo.jpg", mime_type="image/jpeg")
        with patch("adapter.cache_media_bytes", return_value=cached):
            await self.adapter._on_event(event)
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0].text, "[Attachment]")
        self.assertEqual(accepted[0].media_urls, ["/private/cache/photo.jpg"])
        self.assertEqual(accepted[0].raw_message["declared_attachment_count"], 1)
        self.assertEqual(accepted[0].raw_message["unavailable_attachment_count"], 0)
        operations = [item[0] for item in self.calls]
        self.assertIn("materialize_attachment", operations)
        self.assertIn("release_attachment", operations)
        self.assertEqual(operations[-1], "ack")

    async def test_unavailable_attachment_is_explicit_not_raw_object_replacement_text(self) -> None:
        accepted = []

        async def handle(event):
            accepted.append(event)

        self.adapter.handle_message = handle  # type: ignore[method-assign]
        event = {
            "event_id": "event-unavailable",
            "kind": "message",
            "chat_id": "imessage:" + "4" * 32,
            "native_id": "native-unavailable",
            "sender": "mailto:peer@example.com",
            "participants": ["mailto:peer@example.com"],
            "timestamp_ms": 1_700_000_000_000,
            "received_monotonic_ns": 100,
            "from_me": False,
            "text": "\ufffc",
            "attachments": [
                {
                    "attachment_id": "b" * 32,
                    "filename": "photo.jpg",
                    "mime": "image/jpeg",
                    "size": 4,
                    "available": False,
                }
            ],
        }
        await self.adapter._on_event(event)
        self.assertEqual(accepted[0].text, "[Attachment unavailable]")
        self.assertEqual(accepted[0].raw_message["declared_attachment_count"], 1)
        self.assertEqual(accepted[0].raw_message["unavailable_attachment_count"], 1)

    async def test_reaction_to_prior_own_message_reaches_generic_handler(self) -> None:
        accepted = []

        async def handle(event):
            accepted.append(event)

        self.adapter.handle_message = handle  # type: ignore[method-assign]
        event = {
            "event_id": "event-2",
            "kind": "reaction",
            "chat_id": "imessage:" + "2" * 32,
            "native_id": "reaction-1",
            "sender": "tel:+15555550100",
            "participants": ["tel:+15555550100"],
            "timestamp_ms": 1_700_000_000_000,
            "target_id": "mine-before-restart",
            "target_from_me": True,
            "emoji": "❤️",
            "remove": False,
            "from_me": False,
        }
        await self.adapter._on_event(event)
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0].text, "reaction:added:❤️")
        self.assertEqual(self.calls[-1][0], "ack")

    def test_config_rejects_network_options(self) -> None:
        self.assertEqual(
            validate_config({"extra": {"socket_path": "/tmp/a.sock"}}),
            (True, ""),
        )
        error = validate_config(
            {"extra": {"socket_path": "/tmp/a.sock", "bridge_url": "http://127.0.0.1"}}
        )
        self.assertEqual(error[0], False)


if __name__ == "__main__":
    unittest.main()
