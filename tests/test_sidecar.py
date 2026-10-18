from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SIDECAR = ROOT / "sidecar"
if str(SIDECAR) not in sys.path:
    sys.path.insert(0, str(SIDECAR))

from core import ENVELOPE_MAGIC, PROTOCOL_VERSION, conversation_id, normalize_identifier  # noqa: E402
from runtime import CommandError, RustPushRuntime, _reply_target, _transport_error  # noqa: E402
from server import LocalProtocolServer  # noqa: E402


class FakeConversation:
    def __init__(self, participants, group_name, sender_guid, is_sms):
        self.participants = participants
        self.group_name = group_name
        self.sender_guid = sender_guid
        self.is_sms = is_sms


class FakeBinding:
    WrappedConversation = FakeConversation

    @staticmethod
    def protect_state(purpose: str, payload: bytes) -> bytes:
        encoded = bytes(value ^ 0xA5 for value in payload)
        return ENVELOPE_MAGIC + purpose.encode() + b"\x00" + encoded

    @staticmethod
    def unprotect_state(purpose: str, payload: bytes) -> bytes:
        prefix = ENVELOPE_MAGIC + purpose.encode() + b"\x00"
        if not payload.startswith(prefix):
            raise RuntimeError("bad envelope")
        return bytes(value ^ 0xA5 for value in payload[len(prefix) :])


class FakeClient:
    def __init__(self):
        self.calls = []

    async def _record(self, name, *args, result=None):
        self.calls.append((name, args))
        return result

    async def send_message(self, *args):
        return await self._record("send_message", *args, result="out-message")

    async def send_attachment(self, *args):
        return await self._record("send_attachment", *args, result="out-file")

    async def send_typing(self, *args):
        return await self._record("send_typing", *args)

    async def send_read_receipt(self, *args):
        return await self._record("send_read_receipt", *args)

    async def send_delivery_receipt(self, *args):
        return await self._record("send_delivery_receipt", *args)

    async def send_tapback(self, *args):
        return await self._record("send_tapback", *args)

    async def send_edit(self, *args):
        return await self._record("send_edit", *args, result="out-edit")

    async def send_unsend(self, *args):
        return await self._record("send_unsend", *args, result="out-unsend")

    async def send_rename_group(self, *args):
        return await self._record("send_rename_group", *args, result="out-rename")

    async def send_change_participants(self, *args):
        return await self._record("send_change_participants", *args, result="out-members")

    async def retry_mmcs_from_descriptor(self, *args):
        return await self._record("retry_mmcs", *args, result=b"downloaded")


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.runtime = RustPushRuntime(FakeBinding(), self.root)
        self.runtime.transport = self.runtime._empty_transport()
        self.runtime.client = FakeClient()
        self.runtime.ready = True
        self.runtime.started_ms = 1
        self.runtime.own_handles = ["mailto:owner@example.com"]
        self.runtime.default_handle = "mailto:owner@example.com"
        self.events = []

        async def emit(event):
            self.events.append(event)

        async def no_checkpoint():
            return None

        self.runtime.set_broadcast(emit)
        self.runtime.checkpoint = no_checkpoint

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_conversation_identity_excludes_mutable_name(self):
        one = conversation_id(["mailto:a@example.com"], "Old", "guid", False)
        two = conversation_id(["mailto:b@example.com"], "New", "guid", False)
        self.assertEqual(one, two)
        self.assertEqual(normalize_identifier("Person@Example.com"), "mailto:person@example.com")

    async def test_transport_errors_are_reduced_to_fixed_safe_codes(self):
        cases = {
            "Failed to send message: Not Connected": "connection_unavailable",
            "Failed to send message: Lookup Error private-account-data": "ids_lookup_failed",
            "Failed to send message: Send error 13": "send_rejected",
            "Failed to send message: APS Ack error 4": "aps_ack_failed",
            "The handle private@example.com no longer exists on your account": "sender_handle_invalid",
            "Could not deliver message. rate limit": "rate_limited",
            "opaque upstream failure private@example.com": "transport_error",
        }
        for raw, expected in cases.items():
            with self.subTest(expected=expected):
                error = _transport_error(RuntimeError(raw))
                self.assertEqual(error.code, expected)
                self.assertNotIn("private", str(error))

    async def test_reply_target_defaults_and_attachment_suffix(self):
        self.assertEqual(_reply_target("MESSAGE-UUID", None), ("MESSAGE-UUID", "0:0:0"))
        self.assertEqual(_reply_target("MESSAGE-UUID_att0", None), ("MESSAGE-UUID", "1:0:0"))
        self.assertEqual(_reply_target("MESSAGE-UUID_att2", "9:0:4"), ("MESSAGE-UUID", "9:0:4"))

    async def test_native_self_chat_registration_supports_opaque_reply(self):
        chat_id, _ = await self.runtime._register_message_conversation(SimpleNamespace(
            sender="mailto:owner@example.com",
            participants=[" MAILTO:Owner@Example.COM "],
        ))
        self.assertRegex(chat_id, r"^imessage:[0-9a-f]{32}$")
        result = await self.runtime.dispatch({
            "op": "reply", "chat_id": chat_id,
            "text": "synthetic native self-chat reply", "reply_to": "self-message",
        })
        self.assertEqual(result["chat_id"], chat_id)
        self.assertEqual(result["native_id"], "out-message")
        name, args = self.runtime.client.calls[-1]
        self.assertEqual(name, "send_message")
        self.assertEqual(args[0].participants, ["mailto:owner@example.com"])
        self.assertEqual(args[4:6], ("self-message", "0:0:0"))
        self.assertEqual(self.runtime.transport["conversations"][chat_id]["participants"],
                         ["mailto:owner@example.com"])

    async def test_native_self_chat_with_missing_participants_uses_own_sender(self):
        for fields in ({}, {"participants": None}, {"participants": []}):
            with self.subTest(fields=fields):
                chat_id, record = await self.runtime._register_message_conversation(
                    SimpleNamespace(sender="Owner@Example.COM", **fields)
                )
                await self.runtime.dispatch({
                    "op": "reply", "chat_id": chat_id, "text": "synthetic reply",
                    "reply_to": "self-message",
                })
                self.assertEqual(record["participants"], ["mailto:owner@example.com"])
                self.assertEqual(self.runtime.client.calls[-1][1][0].participants,
                                 ["mailto:owner@example.com"])

    async def test_native_self_chat_preserves_only_one_normalized_own_recipient(self):
        self.runtime.own_handles.append("mailto:other-self@example.com")
        chat_id, record = await self.runtime._register_message_conversation(SimpleNamespace(
            sender="mailto:owner@example.com",
            participants=[" MAILTO:Other-Self@Example.COM ", "mailto:owner@example.com"],
        ))
        await self.runtime.dispatch({
            "op": "reply", "chat_id": chat_id, "text": "synthetic reply", "reply_to": "self-message",
        })
        self.assertEqual(record["participants"], ["mailto:other-self@example.com"])
        self.assertEqual(self.runtime.client.calls[-1][1][0].participants, record["participants"])

    async def test_native_mixed_group_registration_keeps_peers_and_identity(self):
        message = SimpleNamespace(
            sender="mailto:owner@example.com",
            participants=["mailto:owner@example.com", "Peer@Example.COM", "mailto:second@example.com"],
            sender_guid="synthetic-group-guid", group_name="Old name",
        )
        chat_id, record = await self.runtime._register_message_conversation(message)
        message.group_name = "New name"
        renamed_id, _ = await self.runtime._register_message_conversation(message)
        self.assertEqual(renamed_id, chat_id)
        await self.runtime.dispatch({
            "op": "reply", "chat_id": chat_id, "text": "synthetic group reply", "reply_to": "peer-message",
        })
        expected = ["mailto:peer@example.com", "mailto:second@example.com"]
        self.assertEqual(record["participants"], expected)
        conversation = self.runtime.client.calls[-1][1][0]
        self.assertEqual(conversation.participants, expected)
        self.assertEqual(conversation.sender_guid, message.sender_guid)
        self.assertEqual(conversation.group_name, "New name")

    async def test_empty_or_invalid_native_and_legacy_records_do_not_infer_own_target(self):
        for participants in ([], [None, "invalid-target"]):
            with self.subTest(participants=participants):
                chat_id, record = await self.runtime._register_message_conversation(
                    SimpleNamespace(sender=None, participants=participants)
                )
                self.assertEqual(record["participants"], [])
                # Also exercise an existing record without native own-handle evidence.
                self.runtime.transport["conversations"][chat_id]["participants"] = participants
                with self.assertRaisesRegex(CommandError, "^unknown_conversation$"):
                    await self.runtime.dispatch({
                        "op": "reply", "chat_id": chat_id, "text": "must not send", "reply_to": "source",
                    })
        self.assertEqual(self.runtime.client.calls, [])

    async def test_native_self_chat_sms_still_rejected(self):
        message = SimpleNamespace(sender="mailto:owner@example.com",
                                  participants=["mailto:owner@example.com"], is_sms=True)
        await self.runtime._process_message(message)
        self.assertEqual(self.events, [])
        self.assertEqual(self.runtime.transport["conversations"], {})
        chat_id, _ = await self.runtime._register_message_conversation(message)
        with self.assertRaisesRegex(CommandError, "^sms_not_supported$"):
            await self.runtime.dispatch({
                "op": "reply", "chat_id": chat_id, "text": "must not send", "reply_to": "source",
            })
        self.assertEqual(self.runtime.client.calls, [])

    async def test_self_chat_reply_preserves_own_recipient(self):
        result = await self.runtime.dispatch({
            "op": "reply", "chat_id": "mailto:owner@example.com",
            "text": "synthetic self-chat reply", "reply_to": "self-message",
        })
        self.assertEqual(result["native_id"], "out-message")
        name, args = self.runtime.client.calls[-1]
        self.assertEqual(name, "send_message")
        self.assertEqual(args[0].participants, ["mailto:owner@example.com"])
        self.assertEqual(args[4], "self-message")

    async def test_existing_self_chat_uses_only_one_own_handle(self):
        created = await self.runtime.dispatch({
            "op": "create_conversation", "participants": ["mailto:peer@example.com"],
        })
        self.runtime.own_handles = ["mailto:owner@example.com", "mailto:other-self@example.com"]
        self.runtime.transport["conversations"][created["chat_id"]]["participants"] = list(self.runtime.own_handles)
        await self.runtime.dispatch({
            "op": "send", "chat_id": created["chat_id"], "text": "synthetic self-chat send",
        })
        conversation = self.runtime.client.calls[-1][1][0]
        self.assertEqual(len(conversation.participants), 1)
        self.assertIn(conversation.participants[0], self.runtime.own_handles)

    async def test_peer_reply_still_excludes_own_handle(self):
        created = await self.runtime.dispatch({
            "op": "create_conversation", "participants": ["mailto:peer@example.com"],
        })
        self.runtime.transport["conversations"][created["chat_id"]]["participants"] = [
            "mailto:owner@example.com", "mailto:peer@example.com",
        ]
        await self.runtime.dispatch({
            "op": "send", "chat_id": created["chat_id"], "text": "synthetic peer send",
        })
        self.assertEqual(self.runtime.client.calls[-1][1][0].participants, ["mailto:peer@example.com"])

    async def test_all_outbound_operations_map_thinly_to_client(self):
        created = await self.runtime.dispatch(
            {"op": "create_conversation", "participants": ["peer@example.com"], "group_name": None}
        )
        chat_id = created["chat_id"]
        result = await self.runtime.dispatch({"op": "send", "chat_id": chat_id, "text": "hello"})
        self.assertEqual(result["native_id"], "out-message")
        await self.runtime.dispatch({"op": "reply", "chat_id": chat_id, "text": "reply", "reply_to": "source"})
        await self.runtime.dispatch({"op": "typing", "chat_id": chat_id, "active": True})
        await self.runtime.dispatch({"op": "mark_read", "chat_id": chat_id, "native_id": "source"})
        await self.runtime.dispatch(
            {"op": "react", "chat_id": chat_id, "native_id": "source", "reaction": "👍", "part": 0}
        )
        await self.runtime.dispatch(
            {"op": "unreact", "chat_id": chat_id, "native_id": "source", "reaction": "👍", "part": 0}
        )
        await self.runtime.dispatch(
            {"op": "edit", "chat_id": chat_id, "native_id": "out-message", "text": "edited", "part": 0}
        )
        await self.runtime.dispatch(
            {"op": "undo_send", "chat_id": chat_id, "native_id": "out-message", "part": 0}
        )
        await self.runtime.dispatch({"op": "rename_group", "chat_id": chat_id, "name": "Name"})
        await self.runtime.dispatch(
            {"op": "change_participants", "chat_id": chat_id, "participants": ["peer@example.com", "+12125550100"]}
        )
        attachment = self.root / "small.txt"
        attachment.write_text("attachment", encoding="utf-8")
        await self.runtime.dispatch(
            {"op": "send_file", "chat_id": chat_id, "path": str(attachment), "caption": "caption"}
        )
        names = [name for name, _args in self.runtime.client.calls]
        self.assertEqual(names.count("send_message"), 2)
        reply_args = next(args for name, args in self.runtime.client.calls if name == "send_message" and args[1] == "reply")
        self.assertEqual(reply_args[4:6], ("source", "0:0:0"))
        for expected in (
            "send_attachment",
            "send_typing",
            "send_read_receipt",
            "send_tapback",
            "send_edit",
            "send_unsend",
            "send_rename_group",
            "send_change_participants",
        ):
            self.assertIn(expected, names)

    async def test_inbound_event_persists_before_delivery_and_ack_cleans_attachment(self):
        attachment = SimpleNamespace(
            filename="photo.jpg",
            mime_type="image/jpeg",
            uti_type="public.jpeg",
            size=3,
            inline_data=b"abc",
            mmcs_descriptor_json=None,
        )
        message = SimpleNamespace(
            uuid="in-1",
            sender="mailto:peer@example.com",
            text="hi",
            subject=None,
            participants=["mailto:owner@example.com", "mailto:peer@example.com"],
            group_name=None,
            timestamp_ms=123,
            is_sms=False,
            is_typing=False,
            is_tapback=False,
            is_edit=False,
            is_unsend=False,
            is_rename=False,
            is_participant_change=False,
            is_read_receipt=False,
            is_delivered=False,
            is_error=False,
            is_icon_change=False,
            is_mark_unread=False,
            is_move_to_recycle_bin=False,
            is_recover_chat=False,
            is_permanent_delete=False,
            sender_guid=None,
            attachments=[attachment],
            reply_guid=None,
            reply_part=None,
            html=None,
            is_voice=False,
            effect=None,
            send_delivered=True,
        )
        await self.runtime._process_message(message)
        self.assertEqual(len(self.events), 1)
        event = self.events[0]
        self.assertEqual(event["kind"], "message")
        self.assertEqual(event["text"], "hi")
        self.assertTrue(self.runtime.transport["events"])
        state_bytes = (self.root / "transport-state.enc").read_bytes()
        self.assertTrue(state_bytes.startswith(ENVELOPE_MAGIC))
        self.assertNotIn(b'"text":"hi"', state_bytes)
        self.assertEqual(event["attachments"][0]["filename"], "photo.jpg")
        attachment_id = event["attachments"][0]["attachment_id"]
        materialized = await self.runtime.dispatch({"op": "materialize_attachment", "attachment_id": attachment_id})
        path = Path(materialized["path"])
        self.assertEqual(path.read_bytes(), b"abc")
        await self.runtime.dispatch({"op": "release_attachment", "attachment_id": attachment_id})
        self.assertFalse(path.exists())
        response = await self.runtime.dispatch({"op": "ack", "event_id": event["event_id"]})
        self.assertTrue(response["acked"])
        self.assertNotIn(attachment_id, self.runtime.transport["attachments"])
        self.assertEqual([name for name, _args in self.runtime.client.calls].count("send_delivery_receipt"), 1)

    async def test_typing_is_event_driven_but_not_replayed(self):
        message = SimpleNamespace(
            uuid="typing-1",
            sender="mailto:peer@example.com",
            participants=["mailto:peer@example.com"],
            group_name=None,
            timestamp_ms=123,
            is_sms=False,
            is_typing=True,
            sender_guid=None,
        )
        await self.runtime._process_message(message)
        self.assertEqual(self.events[0]["kind"], "typing")
        self.assertTrue(self.events[0]["ephemeral"])
        self.assertEqual(self.runtime.transport["events"], [])

    async def test_outgoing_text_to_someone_else_is_not_a_hermes_request(self):
        self.runtime.own_handles = ["mailto:owner@example.com", "tel:+15555550100"]
        message = SimpleNamespace(
            uuid="to-friend",
            sender="tel:+15555550100",
            participants=["tel:+15555550100", "tel:+15555550999"],
            group_name=None,
            timestamp_ms=123,
            is_sms=False,
            text="game",
            sender_guid="thread-friend",
        )
        await self.runtime._process_message(message)
        self.assertTrue(self.events[0]["from_me"])
        self.assertNotIn("tel:+15555550100", self.events[0]["participants"])
        self.assertIn("tel:+15555550999", self.events[0]["participants"])


class FakeRuntime:
    ready = True

    def set_broadcast(self, callback):
        self.broadcast = callback

    async def pending_events(self):
        return []

    async def dispatch(self, request):
        return {"ready": True, "echo": request["op"]}


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_uid_protocol_handshake_and_request(self):
        with tempfile.TemporaryDirectory() as temp:
            socket_path = Path(temp).resolve() / "bridge.sock"
            server = LocalProtocolServer(FakeRuntime(), socket_path)
            await server.start()
            self.assertEqual(socket_path.stat().st_mode & 0o777, 0o600)
            reader, writer = await asyncio.open_unix_connection(str(socket_path))
            writer.write(
                json.dumps({"op": "hello", "protocol": PROTOCOL_VERSION, "request_id": "hello"}).encode() + b"\n"
            )
            await writer.drain()
            hello = json.loads(await reader.readline())
            self.assertTrue(hello["ok"])
            writer.write(json.dumps({"op": "health", "request_id": "health"}).encode() + b"\n")
            await writer.drain()
            health = json.loads(await reader.readline())
            self.assertTrue(health["ok"])
            self.assertEqual(health["result"]["echo"], "health")
            writer.close()
            await writer.wait_closed()
            await server.stop()
            self.assertFalse(socket_path.exists())


if __name__ == "__main__":
    unittest.main()
