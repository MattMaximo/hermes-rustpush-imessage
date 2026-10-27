"""Persistent RustPush runtime and loss-aware local transport state."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
import shutil
import stat
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

from core import (
    ACCOUNT_PURPOSE,
    APS_PURPOSE,
    CAPABILITIES,
    PROTOCOL_VERSION,
    TRANSPORT_PURPOSE,
    SecureStore,
    atomic_private_write,
    content_types,
    conversation_id,
    ensure_private_dir,
    normalize_identifier,
    normalize_participants,
    public_attachment,
    safe_filename,
    tapback_values,
    valid_attachment_id,
    valid_opaque_chat_id,
)

TRANSPORT_FILE = "transport-state.enc"
ACCOUNT_FILE = "account-state.enc"
APS_FILE = "aps-state.enc"
TRANSPORT_SCHEMA = 1
MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024
MAX_SEEN_KEYS = 16_384
MAX_PARTICIPANTS = 64

Broadcast = Callable[[Dict[str, Any]], Awaitable[None]]


class CommandError(RuntimeError):
    """A deliberately sanitized command failure safe to return over the socket."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _required_string(value: Any, *, allow_empty: bool = False, maximum: int = 400_000) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not allow_empty and not value):
        raise CommandError("invalid_request")
    if "\x00" in value:
        raise CommandError("invalid_request")
    return value


def _optional_string(value: Any, *, maximum: int = 400_000) -> Optional[str]:
    if value is None:
        return None
    return _required_string(value, allow_empty=True, maximum=maximum)


def _reply_target(raw_guid: Any, raw_part: Any) -> tuple[str, str]:
    """Return the complete reply target rustpush requires.

    RustPush serializes replies as ``r:<part>:<guid>`` and assumes a part is
    present whenever a GUID is present. Hermes supplies only the native UUID
    for an ordinary reply, so use the text-body part. The wrapper represents
    attachment targets with an ``_attN`` suffix; translate that suffix to
    iMessage's one-based balloon-part index.
    """
    guid = _required_string(raw_guid, maximum=512)
    part = _optional_string(raw_part, maximum=32)
    base, marker, suffix = guid.rpartition("_att")
    if marker and base and suffix.isdigit():
        guid = base
        if part is None:
            part = f"{int(suffix) + 1}:0:0"
    return guid, part or "0:0:0"


def _integer(value: Any, *, minimum: int = 0, maximum: int = 2**63 - 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or value > maximum:
        raise CommandError("invalid_request")
    return value


def _bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise CommandError("invalid_request")
    return value


def _transport_error(exc: BaseException) -> CommandError:
    # Inspect only to classify; never propagate or log the upstream text because
    # rustpush errors can contain handles, tokens, or account metadata. Every
    # value returned here is a fixed, non-sensitive code.
    text = str(exc).lower()
    if "no registered imessage" in text or "no valid targets" in text:
        return CommandError("not_imessage")
    if "temporarily disabled" in text or "customer message" in text:
        return CommandError("account_restricted")
    if "authentication" in text or "unauthorized" in text or "401" in text or "bad auth cert" in text:
        return CommandError("authentication_required")
    if "too many requests" in text or "rate-limit" in text or "rate limit" in text:
        return CommandError("rate_limited")
    if "no longer exists on your account" in text or "missing handle" in text:
        return CommandError("sender_handle_invalid")
    if "registration error" in text:
        return CommandError("registration_invalid")
    if "lookup error" in text:
        return CommandError("ids_lookup_failed")
    if "ids key missing" in text or "decryption key not found" in text:
        return CommandError("ids_key_missing")
    if (
        "not connected" in text
        or "connectionclosed" in text
        or "other side hung up" in text
        or "aps not ready" in text
        or "aps connection failed" in text
    ):
        return CommandError("connection_unavailable")
    if "timeout" in text or isinstance(exc, asyncio.TimeoutError):
        return CommandError("timeout")
    if "aps ack error" in text:
        return CommandError("aps_ack_failed")
    if "send error" in text:
        return CommandError("send_rejected")
    if "http error" in text or "response error" in text:
        return CommandError("apple_service_error")
    if "attachment" in text or "mmcs" in text:
        return CommandError("attachment_failed")
    if "bad message" in text or "plist parsing" in text or "keyed archive" in text or "protobuf" in text:
        return CommandError("serialization_error")
    if "cryptography error" in text or "aes key error" in text or "aes gcm error" in text:
        return CommandError("cryptography_error")
    return CommandError("transport_error")


def load_binding(bindings_dir: Path) -> Any:
    bindings_dir = bindings_dir.expanduser().resolve()
    if not (bindings_dir / "rustpushgo.py").is_file() or not (bindings_dir / "librustpushgo.dylib").is_file():
        raise RuntimeError("rustpush runtime is incomplete")
    sys.path.insert(0, str(bindings_dir))
    return importlib.import_module("rustpushgo")


class RustPushRuntime:
    def __init__(self, binding: Any, state_dir: Path, *, broadcast: Optional[Broadcast] = None):
        self.binding = binding
        self.state_dir = ensure_private_dir(state_dir.expanduser().resolve())
        self.spool_dir = ensure_private_dir(self.state_dir / "runtime-spool")
        self.store = SecureStore(binding, self.state_dir)
        self.broadcast: Broadcast = broadcast or self._discard_event

        self.account: Dict[str, Any] = {}
        self.transport: Dict[str, Any] = {}
        self.config: Any = None
        self.connection: Any = None
        self.users: Any = None
        self.identity: Any = None
        self.token_provider: Any = None
        self.client: Any = None
        self.own_handles: list[str] = []
        self.default_handle: Optional[str] = None

        self.ready = False
        self.started_ms: Optional[int] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._message_queue: asyncio.Queue[Any] = asyncio.Queue()
        self._message_task: Optional[asyncio.Task[None]] = None
        self._checkpoint_lock = asyncio.Lock()
        self._transport_lock = asyncio.Lock()
        self._materialized: Dict[str, Path] = {}
        self._callbacks: list[Any] = []

    async def _discard_event(self, _event: Dict[str, Any]) -> None:
        return None

    def set_broadcast(self, broadcast: Broadcast) -> None:
        self.broadcast = broadcast

    def _empty_transport(self) -> Dict[str, Any]:
        return {
            "schema": TRANSPORT_SCHEMA,
            "conversations": {},
            "events": [],
            "seen": [],
            "attachments": {},
            "sent_ids": [],
            "outbound_reactions": {},
        }

    def _load_transport(self) -> Dict[str, Any]:
        state = self.store.read_json(TRANSPORT_FILE, TRANSPORT_PURPOSE, self._empty_transport())
        if state.get("schema") != TRANSPORT_SCHEMA:
            raise RuntimeError("unsupported transport state")
        if not isinstance(state.get("conversations"), dict):
            raise RuntimeError("invalid conversation state")
        if not isinstance(state.get("events"), list):
            raise RuntimeError("invalid event state")
        if not isinstance(state.get("seen"), list):
            raise RuntimeError("invalid deduplication state")
        if not isinstance(state.get("attachments"), dict):
            raise RuntimeError("invalid attachment state")
        state.setdefault("sent_ids", [])
        state.setdefault("outbound_reactions", {})
        if not isinstance(state["sent_ids"], list) or not isinstance(state["outbound_reactions"], dict):
            raise RuntimeError("invalid outbound state")
        return state

    def _clear_runtime_spool(self) -> None:
        for child in list(self.spool_dir.iterdir()):
            st = child.lstat()
            if st.st_uid != os.getuid():
                raise RuntimeError("runtime spool contains a foreign-owned entry")
            if stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode):
                shutil.rmtree(child)
            else:
                child.unlink()

    def _reconcile_attachment_files(self) -> None:
        expected = {
            record.get("stored")
            for record in self.transport["attachments"].values()
            if isinstance(record, dict) and isinstance(record.get("stored"), str)
        }
        for path in self.state_dir.glob("attachment-*.enc"):
            if path.name not in expected:
                st = path.lstat()
                if stat.S_ISREG(st.st_mode) and not stat.S_ISLNK(st.st_mode) and st.st_uid == os.getuid():
                    path.unlink()

    async def start(self) -> None:
        os.umask(0o077)
        os.environ["XDG_DATA_HOME"] = str(ensure_private_dir(self.state_dir / "xdg"))
        os.environ["RUST_LOG"] = "off"
        os.environ["RUST_BACKTRACE"] = "0"
        self.binding.init_secure_runtime()
        self._clear_runtime_spool()
        self.transport = self._load_transport()
        self._reconcile_attachment_files()
        self.account = self.store.read_json(ACCOUNT_FILE, ACCOUNT_PURPOSE)
        self._validate_account()

        self._loop = asyncio.get_running_loop()
        self.config = self.binding.create_local_macos_config_with_device_id(self.account["device_id"])
        aps_string = self.store.read_bytes(APS_FILE, APS_PURPOSE).decode("utf-8")
        aps_state = self.binding.WrappedApsState(aps_string)
        self.connection = await self.binding.connect(self.config, aps_state)
        try:
            account = self.account["account"]
            self.token_provider = await self.binding.restore_token_provider(
                self.config,
                self.connection,
                account["username"],
                account["hashed_password_hex"],
                account["pet"],
                account["spd_base64"],
                account.get("persist_blob"),
            )
            delegate = account.get("mme_delegate_json")
            if delegate:
                await self.token_provider.seed_mme_delegate_json(delegate)

            self.users = self.binding.WrappedIdsUsers(self.account["ids_users"])
            if not self.users.validate_keystore():
                raise RuntimeError("registered identity does not match the encrypted keystore")
            self.identity = self.binding.WrappedIdsngmIdentity(self.account["ids_identity"])
            message_callback = self._make_message_callback()
            users_callback = self._make_users_callback()
            self._callbacks = [message_callback, users_callback]
            self._message_task = asyncio.create_task(self._message_worker(), name="rustpush-message-worker")
            self.client = await self.binding.new_client(
                self.connection,
                self.users,
                self.identity,
                self.config,
                self.token_provider,
                message_callback,
                users_callback,
            )
            self.own_handles = normalize_participants(await self.client.get_handles())
            if not self.own_handles:
                raise RuntimeError("rustpush returned no registered iMessage handles")
            self.default_handle = self._choose_default_handle(account["username"])
            self.started_ms = _now_ms()
            self.ready = True
            await self.checkpoint()
        except BaseException:
            await self.stop()
            raise

    def _validate_account(self) -> None:
        if self.account.get("schema") != 1:
            raise RuntimeError("unsupported account state")
        for key in ("device_id", "ids_users", "ids_identity"):
            if not isinstance(self.account.get(key), str) or not self.account[key]:
                raise RuntimeError("incomplete account state")
        account = self.account.get("account")
        if not isinstance(account, dict):
            raise RuntimeError("incomplete account state")
        for key in ("username", "hashed_password_hex", "pet", "spd_base64"):
            if not isinstance(account.get(key), str) or not account[key]:
                raise RuntimeError("incomplete account state")

    def _choose_default_handle(self, username: str) -> str:
        try:
            preferred = normalize_identifier(username)
        except ValueError:
            preferred = ""
        by_lower = {handle.lower(): handle for handle in self.own_handles}
        if preferred.lower() in by_lower:
            return by_lower[preferred.lower()]
        return sorted(self.own_handles)[0]

    def _make_message_callback(self) -> Any:
        runtime = self

        class Callback(self.binding.MessageCallback):
            def on_message(self, msg: Any) -> None:
                loop = runtime._loop
                if loop is not None and not loop.is_closed():
                    loop.call_soon_threadsafe(runtime._message_queue.put_nowait, msg)

        return Callback()

    def _make_users_callback(self) -> Any:
        runtime = self

        class Callback(self.binding.UpdateUsersCallback):
            def update_users(self, users: Any) -> None:
                try:
                    serialized = users.to_string()
                except BaseException:
                    return
                loop = runtime._loop
                if loop is not None and not loop.is_closed():
                    loop.call_soon_threadsafe(runtime._accept_users_update, users, serialized)

        return Callback()

    def _accept_users_update(self, users: Any, serialized: str) -> None:
        self.users = users
        self.account["ids_users"] = serialized
        if self.ready:
            asyncio.create_task(self.checkpoint(), name="rustpush-users-checkpoint")

    async def stop(self) -> None:
        was_started = self.connection is not None
        self.ready = False
        if was_started:
            try:
                await self.checkpoint()
            except BaseException:
                pass
        task = self._message_task
        self._message_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self.connection is not None:
            try:
                self.connection.close()
            except BaseException:
                pass
        self.connection = None
        self.client = None
        self.token_provider = None
        self._callbacks = []
        self._clear_runtime_spool()
        self._materialized.clear()

    async def checkpoint(self) -> None:
        if self.connection is None:
            return
        async with self._checkpoint_lock:
            aps = await self.connection.state()
            aps_raw = bytearray(aps.to_string().encode("utf-8"))
            try:
                self.store.write_bytes(APS_FILE, APS_PURPOSE, bytes(aps_raw))
            finally:
                aps_raw[:] = b"\x00" * len(aps_raw)

            if self.token_provider is not None:
                account = self.account["account"]
                account["persist_blob"] = await self.token_provider.get_account_persist_blob()
                account["mme_delegate_json"] = await self.token_provider.get_mme_delegate_json()
            if self.users is not None:
                self.account["ids_users"] = self.users.to_string()
            self.store.write_json(ACCOUNT_FILE, ACCOUNT_PURPOSE, self.account)

    async def _persist_transport_locked(self) -> None:
        self.store.write_json(TRANSPORT_FILE, TRANSPORT_PURPOSE, self.transport)

    async def pending_events(self) -> list[Dict[str, Any]]:
        async with self._transport_lock:
            return [dict(event) for event in self.transport["events"] if isinstance(event, dict)]

    async def _message_worker(self) -> None:
        while True:
            msg = await self._message_queue.get()
            try:
                await self._process_message(msg)
            except asyncio.CancelledError:
                raise
            except BaseException:
                # Fail closed without printing the upstream exception. APS may
                # redeliver; otherwise the process supervisor restarts on fatal
                # connection failure.
                continue

    def _message_kind(self, msg: Any) -> str:
        checks = (
            ("is_typing", "typing"),
            ("is_tapback", "reaction"),
            ("is_edit", "edit"),
            ("is_unsend", "unsend"),
            ("is_rename", "rename_group"),
            ("is_participant_change", "participants_changed"),
            ("is_read_receipt", "read_receipt"),
            ("is_delivered", "delivery_receipt"),
            ("is_error", "send_error"),
            ("is_icon_change", "group_icon_changed"),
            ("is_mark_unread", "marked_unread"),
            ("is_move_to_recycle_bin", "chat_deleted"),
            ("is_recover_chat", "chat_recovered"),
            ("is_permanent_delete", "permanent_delete"),
        )
        for field, kind in checks:
            if bool(getattr(msg, field, False)):
                return kind
        return "message"

    def _dedupe_key(self, msg: Any, kind: str) -> str:
        identifying = {
            "kind": kind,
            "uuid": getattr(msg, "uuid", None),
            "target": (
                getattr(msg, "tapback_target_uuid", None)
                or getattr(msg, "edit_target_uuid", None)
                or getattr(msg, "unsend_target_uuid", None)
                or getattr(msg, "error_for_uuid", None)
            ),
            "part": getattr(msg, "edit_part", None) or getattr(msg, "tapback_target_part", None),
            "remove": bool(getattr(msg, "tapback_remove", False)),
            "timestamp": int(getattr(msg, "timestamp_ms", 0) or 0),
        }
        raw = json.dumps(identifying, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def _is_own_handle(self, value: Optional[str]) -> bool:
        if not value:
            return False
        try:
            normalized = normalize_identifier(value)
        except ValueError:
            return False
        return normalized.lower() in {handle.lower() for handle in self.own_handles}

    async def _register_message_conversation(self, msg: Any) -> tuple[str, Dict[str, Any]]:
        sender = getattr(msg, "sender", None)
        raw_participants = getattr(msg, "participants", []) or []
        participants = normalize_participants(raw_participants, self.own_handles)
        if sender and not self._is_own_handle(sender):
            participants = normalize_participants([*participants, sender], self.own_handles)
        if not participants:
            # Keep only an observed own recipient when this is a self-chat.
            participants = [
                item for item in normalize_participants([*raw_participants, sender] if sender else raw_participants)
                if self._is_own_handle(item)
            ][:1]
        sender_guid = getattr(msg, "sender_guid", None)
        group_name = getattr(msg, "group_name", None)
        is_sms = bool(getattr(msg, "is_sms", False))
        proposed = conversation_id(participants, group_name, sender_guid, is_sms)

        async with self._transport_lock:
            conversations = self.transport["conversations"]
            chat_id = proposed
            if sender_guid:
                for existing_id, record in conversations.items():
                    if isinstance(record, dict) and record.get("sender_guid") == sender_guid:
                        chat_id = existing_id
                        break
                else:
                    for existing_id, record in conversations.items():
                        if (
                            isinstance(record, dict)
                            and not record.get("sender_guid")
                            and sorted(record.get("participants") or []) == sorted(participants)
                            and not record.get("is_sms")
                        ):
                            chat_id = existing_id
                            break
            else:
                for existing_id, record in conversations.items():
                    if (
                        isinstance(record, dict)
                        and not record.get("sender_guid")
                        and sorted(record.get("participants") or []) == sorted(participants)
                        and bool(record.get("is_sms")) == is_sms
                    ):
                        chat_id = existing_id
                        break

            record = {
                "participants": participants,
                "group_name": group_name,
                "sender_guid": sender_guid,
                "is_sms": is_sms,
                "updated_ms": _now_ms(),
            }
            conversations[chat_id] = record
            await self._persist_transport_locked()
            return chat_id, dict(record)

    async def _store_attachment(self, attachment: Any) -> Dict[str, Any]:
        attachment_id = uuid.uuid4().hex
        name = safe_filename(getattr(attachment, "filename", None) or getattr(attachment, "name", None))
        mime = getattr(attachment, "mime_type", None) or getattr(attachment, "mime", None) or "application/octet-stream"
        uti = getattr(attachment, "uti_type", None) or "public.data"
        declared_size = int(getattr(attachment, "size", 0) or 0)
        descriptor = getattr(attachment, "mmcs_descriptor_json", None)
        inline = getattr(attachment, "inline_data", None)
        data: Optional[bytes] = bytes(inline) if inline is not None else None
        if data is None and descriptor and self.client is not None:
            try:
                data = bytes(await self.client.retry_mmcs_from_descriptor(descriptor, name))
            except BaseException:
                data = None
        stored: Optional[str] = None
        if data is not None:
            if len(data) <= MAX_ATTACHMENT_BYTES:
                stored = f"attachment-{attachment_id}.enc"
                self.store.write_bytes(stored, f"attachment.v1.{attachment_id}", data)
                declared_size = len(data)
            data = None
        record = {
            "id": attachment_id,
            "name": name,
            "mime": str(mime)[:255],
            "uti": str(uti)[:255],
            "size": declared_size,
            "stored": stored,
            "descriptor": descriptor,
        }
        async with self._transport_lock:
            self.transport["attachments"][attachment_id] = record
            await self._persist_transport_locked()
        return public_attachment(record)

    async def _process_message(self, msg: Any) -> None:
        if bool(getattr(msg, "is_sms", False)):
            return
        kind = self._message_kind(msg)
        chat_id, conversation = await self._register_message_conversation(msg)
        event_id = uuid.uuid4().hex
        sender = getattr(msg, "sender", None)
        native_id = str(getattr(msg, "uuid", "") or "")
        event: Dict[str, Any] = {
            "event_id": event_id,
            "kind": kind,
            "chat_id": chat_id,
            "native_id": native_id,
            "sender": sender,
            "participants": conversation["participants"],
            "group_name": conversation.get("group_name"),
            "timestamp_ms": int(getattr(msg, "timestamp_ms", 0) or _now_ms()),
            "received_monotonic_ns": time.monotonic_ns(),
            "from_me": self._is_own_handle(sender),
        }
        if event["from_me"] and event["native_id"]:
            await self._remember_sent(event["native_id"])

        if kind == "message":
            event.update(
                {
                    "text": getattr(msg, "text", None),
                    "subject": getattr(msg, "subject", None),
                    "html": getattr(msg, "html", None),
                    "reply_to": getattr(msg, "reply_guid", None),
                    "reply_part": getattr(msg, "reply_part", None),
                    "is_voice": bool(getattr(msg, "is_voice", False)),
                    "effect": getattr(msg, "effect", None),
                    "attachments": [],
                }
            )
            for attachment in list(getattr(msg, "attachments", []) or []):
                event["attachments"].append(await self._store_attachment(attachment))
        elif kind == "typing":
            event["typing"] = True
        elif kind == "reaction":
            target_id = getattr(msg, "tapback_target_uuid", None)
            event.update(
                {
                    "target_id": target_id,
                    "target_part": getattr(msg, "tapback_target_part", None),
                    "reaction_type": getattr(msg, "tapback_type", None),
                    "emoji": getattr(msg, "tapback_emoji", None),
                    "remove": bool(getattr(msg, "tapback_remove", False)),
                    "target_from_me": target_id in self.transport["sent_ids"],
                }
            )
        elif kind == "edit":
            event.update(
                {
                    "target_id": getattr(msg, "edit_target_uuid", None),
                    "target_part": getattr(msg, "edit_part", None),
                    "text": getattr(msg, "edit_new_text", None),
                }
            )
        elif kind == "unsend":
            event.update(
                {
                    "target_id": getattr(msg, "unsend_target_uuid", None),
                    "target_part": getattr(msg, "unsend_edit_part", None),
                }
            )
        elif kind == "rename_group":
            event["group_name"] = getattr(msg, "new_chat_name", None)
        elif kind == "participants_changed":
            event["participants"] = normalize_participants(getattr(msg, "new_participants", []) or [], self.own_handles)
        elif kind in {"read_receipt", "delivery_receipt"}:
            event["target_id"] = str(getattr(msg, "uuid", "") or "")
        elif kind == "send_error":
            event["target_id"] = getattr(msg, "error_for_uuid", None)
            event["error_code"] = "delivery_failed"

        if kind == "typing":
            event["ephemeral"] = True
            await self.broadcast(event)
            return

        dedupe = self._dedupe_key(msg, kind)
        async with self._transport_lock:
            if dedupe in self.transport["seen"]:
                return
            self.transport["seen"].append(dedupe)
            if len(self.transport["seen"]) > MAX_SEEN_KEYS:
                del self.transport["seen"][:-MAX_SEEN_KEYS]
            self.transport["events"].append(event)
            await self._persist_transport_locked()
        await self.broadcast(event)

        if kind == "message" and not event["from_me"] and bool(getattr(msg, "send_delivered", False)):
            try:
                await self.client.send_delivery_receipt(self._wrapped_conversation(conversation), self.default_handle)
            except BaseException:
                pass
        await self.checkpoint()

    async def _remember_sent(self, native_id: str) -> None:
        async with self._transport_lock:
            sent_ids = self.transport["sent_ids"]
            if native_id in sent_ids:
                sent_ids.remove(native_id)
            sent_ids.append(native_id)
            if len(sent_ids) > 4_096:
                del sent_ids[:-4_096]
            await self._persist_transport_locked()

    @staticmethod
    def _reaction_key(chat_id: str, native_id: str) -> str:
        return hashlib.sha256(f"{chat_id}\x00{native_id}".encode()).hexdigest()

    def _wrapped_conversation(self, record: Dict[str, Any]) -> Any:
        if bool(record.get("is_sms")):
            raise CommandError("sms_not_supported")
        raw = normalize_participants(record.get("participants") or [])
        own = {handle.lower() for handle in self.own_handles}
        participants = [item for item in raw if item.lower() not in own]
        if not participants:
            # A self-chat's only participants are our own handles. Keep one so
            # the reply fans out to the other devices on this account.
            participants = [item for item in raw if item.lower() in own][:1]
        if not participants:
            raise CommandError("unknown_conversation")
        return self.binding.WrappedConversation(
            participants,
            _optional_string(record.get("group_name"), maximum=512),
            _optional_string(record.get("sender_guid"), maximum=512),
            False,
        )

    async def _resolve_conversation(self, value: Any) -> tuple[str, Dict[str, Any], Any]:
        target = _required_string(value, maximum=512)
        async with self._transport_lock:
            if valid_opaque_chat_id(target):
                record = self.transport["conversations"].get(target)
                if not isinstance(record, dict):
                    raise CommandError("unknown_conversation")
                record = dict(record)
                return target, record, self._wrapped_conversation(record)
        try:
            participant = normalize_identifier(target)
        except ValueError:
            raise CommandError("invalid_target") from None
        record = {
            "participants": [participant],
            "group_name": None,
            "sender_guid": None,
            "is_sms": False,
            "updated_ms": _now_ms(),
        }
        chat_id = conversation_id([participant], None, None, False)
        async with self._transport_lock:
            self.transport["conversations"][chat_id] = record
            await self._persist_transport_locked()
        return chat_id, record, self._wrapped_conversation(record)

    async def _call_transport(self, awaitable: Awaitable[Any]) -> Any:
        try:
            return await awaitable
        except BaseException as exc:
            raise _transport_error(exc) from None

    async def dispatch(self, request: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(request, dict):
            raise CommandError("invalid_request")
        op = request.get("op")
        if op == "health":
            return {
                "ready": self.ready,
                "protocol": PROTOCOL_VERSION,
                "capabilities": sorted(CAPABILITIES),
                "handle_count": len(self.own_handles),
                "pending_events": len(self.transport.get("events", [])),
                "started_ms": self.started_ms,
            }
        if not self.ready or self.client is None or self.default_handle is None:
            raise CommandError("not_ready")
        if op == "ack":
            return await self._ack(request.get("event_id"))
        if op == "create_conversation":
            return await self._create_conversation(request)
        if op == "materialize_attachment":
            return await self._materialize(request.get("attachment_id"))
        if op == "release_attachment":
            return await self._release(request.get("attachment_id"))

        chat_id, record, conversation = await self._resolve_conversation(request.get("chat_id"))
        handle = self.default_handle
        native_id: Optional[str] = None

        if op in {"send", "reply"}:
            text = _required_string(request.get("text"), allow_empty=True)
            if not text and not request.get("html"):
                raise CommandError("invalid_request")
            if op == "reply":
                reply_guid, reply_part = _reply_target(request.get("reply_to"), request.get("reply_part"))
            else:
                reply_guid, reply_part = None, None
            native_id = await self._call_transport(
                self.client.send_message(
                    conversation,
                    text,
                    _optional_string(request.get("html")),
                    handle,
                    reply_guid,
                    reply_part,
                    None,
                )
            )
        elif op in {"send_file", "reply_file"}:
            source = Path(_required_string(request.get("path"), maximum=4096)).expanduser()
            try:
                st = source.lstat()
            except FileNotFoundError:
                raise CommandError("attachment_missing") from None
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
                raise CommandError("attachment_unsafe")
            if st.st_size > MAX_ATTACHMENT_BYTES:
                raise CommandError("attachment_too_large")
            data = source.read_bytes()
            mime, uti = content_types(source, _optional_string(request.get("mime"), maximum=255))
            if op == "reply_file":
                reply_guid, reply_part = _reply_target(request.get("reply_to"), request.get("reply_part"))
            else:
                reply_guid, reply_part = None, None
            try:
                native_id = await self._call_transport(
                    self.client.send_attachment(
                        conversation,
                        data,
                        mime,
                        uti,
                        safe_filename(request.get("filename") or source.name),
                        handle,
                        reply_guid,
                        reply_part,
                        _optional_string(request.get("caption")),
                    )
                )
            finally:
                data = b""
        elif op == "typing":
            active = _bool(request.get("active"))
            await self._call_transport(self.client.send_typing(conversation, active, handle))
        elif op == "mark_read":
            for_uuid = _optional_string(request.get("native_id"), maximum=512)
            await self._call_transport(self.client.send_read_receipt(conversation, handle, for_uuid))
        elif op in {"react", "unreact"}:
            target_id = _required_string(request.get("native_id"), maximum=512)
            part = _integer(request.get("part", 0), maximum=65_535)
            reaction_key = self._reaction_key(chat_id, target_id)
            reaction_value = _optional_string(request.get("reaction"), maximum=32)
            if op == "unreact" and reaction_value is None:
                reaction_value = self.transport["outbound_reactions"].get(reaction_key)
            reaction, emoji = tapback_values(_required_string(reaction_value, maximum=32))
            await self._call_transport(
                self.client.send_tapback(conversation, target_id, part, reaction, emoji, op == "unreact", handle)
            )
            async with self._transport_lock:
                if op == "react":
                    self.transport["outbound_reactions"][reaction_key] = reaction_value
                else:
                    self.transport["outbound_reactions"].pop(reaction_key, None)
                await self._persist_transport_locked()
        elif op == "edit":
            target_id = _required_string(request.get("native_id"), maximum=512)
            part = _integer(request.get("part", 0), maximum=65_535)
            text = _required_string(request.get("text"), allow_empty=True)
            native_id = await self._call_transport(self.client.send_edit(conversation, target_id, part, text, handle))
        elif op == "undo_send":
            target_id = _required_string(request.get("native_id"), maximum=512)
            part = _integer(request.get("part", 0), maximum=65_535)
            native_id = await self._call_transport(self.client.send_unsend(conversation, target_id, part, handle))
        elif op == "rename_group":
            name = _required_string(request.get("name"), maximum=512)
            native_id = await self._call_transport(self.client.send_rename_group(conversation, name, handle))
            async with self._transport_lock:
                self.transport["conversations"][chat_id]["group_name"] = name
                await self._persist_transport_locked()
        elif op == "change_participants":
            raw = request.get("participants")
            if not isinstance(raw, list) or not raw or len(raw) > MAX_PARTICIPANTS:
                raise CommandError("invalid_request")
            participants = normalize_participants(raw, self.own_handles)
            if not participants:
                raise CommandError("invalid_request")
            native_id = await self._call_transport(
                self.client.send_change_participants(conversation, participants, int(time.time()), handle)
            )
            async with self._transport_lock:
                self.transport["conversations"][chat_id]["participants"] = participants
                await self._persist_transport_locked()
        else:
            raise CommandError("unsupported_operation")

        await self.checkpoint()
        if native_id is not None and op in {"send", "reply", "send_file", "reply_file"}:
            await self._remember_sent(str(native_id))
        result: Dict[str, Any] = {"chat_id": chat_id, "completed_ms": _now_ms()}
        if native_id is not None:
            result["native_id"] = str(native_id)
        return result

    async def _create_conversation(self, request: Dict[str, Any]) -> Dict[str, Any]:
        raw = request.get("participants")
        if not isinstance(raw, list) or not raw or len(raw) > MAX_PARTICIPANTS:
            raise CommandError("invalid_request")
        participants = normalize_participants(raw, self.own_handles)
        if not participants:
            raise CommandError("invalid_request")
        group_name = _optional_string(request.get("group_name"), maximum=512)
        chat_id = conversation_id(participants, group_name, None, False)
        record = {
            "participants": participants,
            "group_name": group_name,
            "sender_guid": None,
            "is_sms": False,
            "updated_ms": _now_ms(),
        }
        async with self._transport_lock:
            self.transport["conversations"][chat_id] = record
            await self._persist_transport_locked()
        return {"chat_id": chat_id, "participant_count": len(participants)}

    async def _ack(self, event_id: Any) -> Dict[str, Any]:
        event_id = _required_string(event_id, maximum=64)
        removed: Optional[Dict[str, Any]] = None
        async with self._transport_lock:
            kept = []
            for event in self.transport["events"]:
                if isinstance(event, dict) and event.get("event_id") == event_id and removed is None:
                    removed = event
                else:
                    kept.append(event)
            if removed is None:
                return {"acked": False}
            self.transport["events"] = kept
            attachment_ids = {
                item.get("attachment_id") or item.get("id")
                for item in removed.get("attachments", [])
                if isinstance(item, dict)
                and valid_attachment_id(item.get("attachment_id") or item.get("id"))
            }
            still_referenced = {
                item.get("attachment_id") or item.get("id")
                for event in kept
                if isinstance(event, dict)
                for item in event.get("attachments", [])
                if isinstance(item, dict)
            }
            for attachment_id in attachment_ids - still_referenced:
                record = self.transport["attachments"].pop(attachment_id, None)
                if isinstance(record, dict) and isinstance(record.get("stored"), str):
                    path = self.state_dir / record["stored"]
                    if path.exists() and path.is_file() and not path.is_symlink():
                        path.unlink()
                await self._release(attachment_id, persist=False)
            await self._persist_transport_locked()
        return {"acked": True}

    async def _materialize(self, attachment_id: Any) -> Dict[str, Any]:
        if not valid_attachment_id(attachment_id):
            raise CommandError("invalid_request")
        async with self._transport_lock:
            record = self.transport["attachments"].get(attachment_id)
            if not isinstance(record, dict):
                raise CommandError("attachment_missing")
            record = dict(record)
        stored = record.get("stored")
        if not stored and record.get("descriptor"):
            try:
                data = bytes(await self.client.retry_mmcs_from_descriptor(record["descriptor"], record["name"]))
            except BaseException as exc:
                raise _transport_error(exc) from None
            if len(data) > MAX_ATTACHMENT_BYTES:
                raise CommandError("attachment_too_large")
            stored = f"attachment-{attachment_id}.enc"
            self.store.write_bytes(stored, f"attachment.v1.{attachment_id}", data)
            data = b""
            async with self._transport_lock:
                current = self.transport["attachments"].get(attachment_id)
                if isinstance(current, dict):
                    current["stored"] = stored
                    current["size"] = (self.state_dir / stored).stat().st_size
                    await self._persist_transport_locked()
        if not isinstance(stored, str):
            raise CommandError("attachment_unavailable")
        plaintext = bytearray(self.store.read_bytes(stored, f"attachment.v1.{attachment_id}"))
        target_dir = ensure_private_dir(self.spool_dir / attachment_id)
        target = target_dir / safe_filename(record.get("name"))
        try:
            atomic_private_write(target, bytes(plaintext))
        finally:
            plaintext[:] = b"\x00" * len(plaintext)
        self._materialized[attachment_id] = target
        return {"path": str(target), "name": target.name, "size": target.stat().st_size}

    async def _release(self, attachment_id: Any, *, persist: bool = True) -> Dict[str, Any]:
        if not valid_attachment_id(attachment_id):
            if persist:
                raise CommandError("invalid_request")
            return {"released": False}
        path = self._materialized.pop(attachment_id, self.spool_dir / attachment_id / "missing")
        target_dir = self.spool_dir / attachment_id
        released = False
        if path.exists() and path.is_file() and not path.is_symlink():
            path.unlink()
            released = True
        if target_dir.exists() and target_dir.is_dir() and not target_dir.is_symlink():
            try:
                target_dir.rmdir()
            except OSError:
                pass
        return {"released": released}
