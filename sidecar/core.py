"""Security and protocol primitives for the local rustpush sidecar."""

from __future__ import annotations

import ctypes
import hashlib
import json
import mimetypes
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 1_048_576
ENVELOPE_MAGIC = b"HERMES_RUSTPUSH_STATE\x01"
ACCOUNT_PURPOSE = "account-state.v1"
APS_PURPOSE = "aps-state.v1"
TRANSPORT_PURPOSE = "transport-state.v1"

CAPABILITIES = frozenset(
    {
        "send",
        "reply",
        "send_file",
        "reply_file",
        "typing",
        "mark_read",
        "react",
        "unreact",
        "edit",
        "undo_send",
        "create_conversation",
        "rename_group",
        "change_participants",
        "materialize_attachment",
        "release_attachment",
        "health",
        "ack",
    }
)

_IDENTIFIER_RE = re.compile(r"^[^\x00\r\n]{1,320}$")
_OPAQUE_CHAT_RE = re.compile(r"^imessage:[0-9a-f]{32}$")
_ATTACHMENT_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def ensure_private_dir(path: Path) -> Path:
    path = path.expanduser()
    if not path.is_absolute():
        raise ValueError("path must be absolute")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            st = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            st = current.lstat()
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise RuntimeError("private path component is not a directory")
        if st.st_uid not in {0, os.getuid()}:
            raise RuntimeError("private path component has another owner")
        # Root-owned system ancestors such as /Users are trusted only when
        # they are not writable by arbitrary users. A sticky system temp
        # directory is also safe once the next component is user-owned.
        if st.st_uid == 0 and st.st_mode & 0o022 and not st.st_mode & stat.S_ISVTX:
            raise RuntimeError("private path has an unsafe system ancestor")
    final = path.lstat()
    if final.st_uid != os.getuid():
        raise RuntimeError("private directory has another owner")
    os.chmod(path, 0o700)
    return path


def atomic_private_write(path: Path, payload: bytes) -> None:
    ensure_private_dir(path.parent)
    if path.exists() or path.is_symlink():
        st = path.lstat()
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            raise RuntimeError("refusing unsafe state target")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        dirfd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def safe_unlink_owned(path: Path, *, require_socket: bool = False) -> None:
    try:
        st = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode) or st.st_uid != os.getuid():
        raise RuntimeError("refusing unsafe unlink")
    if require_socket and not stat.S_ISSOCK(st.st_mode):
        raise RuntimeError("refusing to replace non-socket path")
    if not require_socket and not stat.S_ISREG(st.st_mode):
        raise RuntimeError("refusing to unlink non-file")
    path.unlink()


def peer_ids(sock_fd: int) -> Tuple[int, int]:
    """Return effective peer uid/gid using macOS getpeereid(3)."""
    libc = ctypes.CDLL(None, use_errno=True)
    function = libc.getpeereid
    function.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
    function.restype = ctypes.c_int
    uid = ctypes.c_uint()
    gid = ctypes.c_uint()
    if function(sock_fd, ctypes.byref(uid), ctypes.byref(gid)) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return int(uid.value), int(gid.value)


def normalize_identifier(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("identifier must be a string")
    value = value.strip()
    if not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError("invalid identifier")
    lowered = value.lower()
    if lowered.startswith("tel:"):
        number = value[4:].replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
        if not re.fullmatch(r"\+?[0-9]{7,18}", number):
            raise ValueError("invalid telephone identifier")
        return "tel:" + number
    if lowered.startswith("mailto:"):
        address = value[7:].strip()
        if "@" not in address or address.startswith("@") or address.endswith("@"):
            raise ValueError("invalid email identifier")
        return "mailto:" + address.lower()
    if "@" in value:
        if value.startswith("@") or value.endswith("@"):
            raise ValueError("invalid email identifier")
        return "mailto:" + value.lower()
    number = value.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if re.fullmatch(r"\+?[0-9]{7,18}", number):
        return "tel:" + number
    raise ValueError("identifier requires tel: or mailto:")


def normalize_participants(values: Iterable[str], own_handles: Iterable[str] = ()) -> list[str]:
    own = {value.lower() for value in own_handles}
    result: list[str] = []
    for raw in values:
        try:
            normalized = normalize_identifier(raw)
        except (TypeError, ValueError):
            continue
        if normalized.lower() in own or normalized in result:
            continue
        result.append(normalized)
        if len(result) >= 64:
            break
    return result


def conversation_id(participants: Sequence[str], group_name: Optional[str], sender_guid: Optional[str], is_sms: bool) -> str:
    # A group name is mutable, so it must never define conversation identity.
    # RustPush's sender GUID is stable for established groups; participant
    # membership is the fallback for direct/new conversations.
    canonical = json.dumps(
        {
            "participants": [] if sender_guid else sorted(participants),
            "sender_guid": sender_guid or "",
            "is_sms": bool(is_sms),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return "imessage:" + hashlib.sha256(canonical).hexdigest()[:32]


def valid_opaque_chat_id(value: Any) -> bool:
    return isinstance(value, str) and _OPAQUE_CHAT_RE.fullmatch(value) is not None


def valid_attachment_id(value: Any) -> bool:
    return isinstance(value, str) and _ATTACHMENT_ID_RE.fullmatch(value) is not None


def safe_filename(value: Optional[str]) -> str:
    name = Path(value or "attachment").name.replace("\x00", "")
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip(" .")
    return (name or "attachment")[:180]


def content_types(path: Path, explicit_mime: Optional[str] = None) -> Tuple[str, str]:
    mime = explicit_mime or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    uti_map = {
        "image/jpeg": "public.jpeg",
        "image/png": "public.png",
        "image/gif": "com.compuserve.gif",
        "image/heic": "public.heic",
        "image/tiff": "public.tiff",
        "video/mp4": "public.mpeg-4",
        "video/quicktime": "com.apple.quicktime-movie",
        "audio/mpeg": "public.mp3",
        "audio/mp4": "public.mpeg-4-audio",
        "audio/x-m4a": "public.mpeg-4-audio",
        "audio/x-caf": "com.apple.coreaudio-format",
        "audio/caf": "com.apple.coreaudio-format",
        "application/pdf": "com.adobe.pdf",
        "text/plain": "public.plain-text",
    }
    return mime, uti_map.get(mime, "public.data")


def tapback_values(value: str) -> Tuple[int, Optional[str]]:
    mapping = {
        "❤️": (0, None),
        "♥️": (0, None),
        "👍": (1, None),
        "👎": (2, None),
        "😂": (3, None),
        "❗": (4, None),
        "‼️": (4, None),
        "❓": (5, None),
    }
    if not isinstance(value, str) or not value or len(value) > 32 or "\x00" in value:
        raise ValueError("invalid reaction")
    return mapping.get(value, (6, value))


def public_attachment(record: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "attachment_id": record["id"],
        "filename": record.get("name") or "attachment",
        "mime": record.get("mime") or "application/octet-stream",
        "uti": record.get("uti") or "public.data",
        "size": int(record.get("size") or 0),
        "available": bool(record.get("stored")),
    }


class SecureStore:
    def __init__(self, binding: Any, root: Path):
        self.binding = binding
        self.root = ensure_private_dir(root)

    def _path(self, name: str) -> Path:
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,80}", name):
            raise ValueError("invalid state name")
        return self.root / name

    def read_bytes(self, name: str, purpose: str) -> bytes:
        protected = self._path(name).read_bytes()
        if not protected.startswith(ENVELOPE_MAGIC):
            raise RuntimeError("plaintext state is forbidden")
        return bytes(self.binding.unprotect_state(purpose, protected))

    def read_json(self, name: str, purpose: str, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        path = self._path(name)
        if not path.exists():
            if default is None:
                raise FileNotFoundError(path)
            return default
        plaintext = bytearray(self.read_bytes(name, purpose))
        try:
            value = json.loads(plaintext.decode("utf-8"))
        finally:
            for index in range(len(plaintext)):
                plaintext[index] = 0
        if not isinstance(value, dict):
            raise RuntimeError("state root must be an object")
        return value

    def write_bytes(self, name: str, purpose: str, plaintext: bytes) -> None:
        protected = bytes(self.binding.protect_state(purpose, plaintext))
        if not protected.startswith(ENVELOPE_MAGIC) or protected == plaintext:
            raise RuntimeError("state encryption failed closed")
        atomic_private_write(self._path(name), protected)

    def write_json(self, name: str, purpose: str, value: Dict[str, Any]) -> None:
        raw = bytearray(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        try:
            self.write_bytes(name, purpose, bytes(raw))
        finally:
            for index in range(len(raw)):
                raw[index] = 0
