"""Private same-user Unix-domain socket protocol for the RustPush runtime."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict

from core import MAX_FRAME_BYTES, PROTOCOL_VERSION, ensure_private_dir, peer_ids, safe_unlink_owned
from runtime import CommandError, RustPushRuntime


class LocalProtocolServer:
    def __init__(self, runtime: RustPushRuntime, socket_path: Path):
        self.runtime = runtime
        self.socket_path = socket_path.expanduser().resolve()
        self.server: asyncio.AbstractServer | None = None
        self._clients: Dict[int, tuple[asyncio.StreamWriter, asyncio.Lock]] = {}
        self._next_client_id = 1
        runtime.set_broadcast(self.broadcast)

    async def start(self) -> None:
        ensure_private_dir(self.socket_path.parent)
        safe_unlink_owned(self.socket_path, require_socket=True)
        self.server = await asyncio.start_unix_server(
            self._accept,
            path=str(self.socket_path),
            limit=MAX_FRAME_BYTES + 1,
        )
        os.chmod(self.socket_path, 0o600)

    async def stop(self) -> None:
        server = self.server
        self.server = None
        if server is not None:
            server.close()
            await server.wait_closed()
        clients = list(self._clients.values())
        self._clients.clear()
        for writer, _lock in clients:
            writer.close()
        for writer, _lock in clients:
            try:
                await writer.wait_closed()
            except BaseException:
                pass
        safe_unlink_owned(self.socket_path, require_socket=True)

    async def _send(self, writer: asyncio.StreamWriter, lock: asyncio.Lock, payload: Dict[str, Any]) -> None:
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
        if len(encoded) > MAX_FRAME_BYTES:
            raise CommandError("frame_too_large")
        async with lock:
            writer.write(encoded)
            await writer.drain()

    async def broadcast(self, event: Dict[str, Any]) -> None:
        stale: list[int] = []
        for client_id, (writer, lock) in list(self._clients.items()):
            try:
                await self._send(writer, lock, {"event": event})
            except BaseException:
                stale.append(client_id)
        for client_id in stale:
            entry = self._clients.pop(client_id, None)
            if entry:
                entry[0].close()

    async def _read_request(self, reader: asyncio.StreamReader) -> Dict[str, Any] | None:
        try:
            line = await reader.readline()
        except (ValueError, asyncio.LimitOverrunError):
            raise CommandError("frame_too_large") from None
        if not line:
            return None
        if len(line) > MAX_FRAME_BYTES:
            raise CommandError("frame_too_large")
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise CommandError("invalid_json") from None
        if not isinstance(value, dict):
            raise CommandError("invalid_request")
        return value

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        lock = asyncio.Lock()
        client_id: int | None = None
        try:
            sock = writer.get_extra_info("socket")
            if sock is None:
                raise CommandError("unauthorized")
            peer_uid, _peer_gid = peer_ids(sock.fileno())
            if peer_uid != os.geteuid():
                raise CommandError("unauthorized")

            hello = await asyncio.wait_for(self._read_request(reader), timeout=5)
            if hello is None or hello.get("op") != "hello" or hello.get("protocol") != PROTOCOL_VERSION:
                raise CommandError("protocol_mismatch")
            request_id = hello.get("request_id")
            if request_id is not None and (not isinstance(request_id, str) or len(request_id) > 64):
                raise CommandError("invalid_request")
            hello_result = await self.runtime.dispatch({"op": "health"})
            await self._send(
                writer,
                lock,
                {
                    "request_id": request_id,
                    "ok": True,
                    "result": hello_result,
                },
            )
            client_id = self._next_client_id
            self._next_client_id += 1
            self._clients[client_id] = (writer, lock)
            for event in await self.runtime.pending_events():
                await self._send(writer, lock, {"event": event})

            while True:
                request = await self._read_request(reader)
                if request is None:
                    break
                request_id = request.get("request_id")
                if not isinstance(request_id, str) or not request_id or len(request_id) > 64:
                    raise CommandError("invalid_request")
                try:
                    result = await self.runtime.dispatch(request)
                    response = {"request_id": request_id, "ok": True, "result": result}
                except CommandError as exc:
                    response = {"request_id": request_id, "ok": False, "error": exc.code}
                except BaseException:
                    response = {"request_id": request_id, "ok": False, "error": "internal_error"}
                await self._send(writer, lock, response)
        except (CommandError, asyncio.TimeoutError):
            pass
        except BaseException:
            pass
        finally:
            if client_id is not None:
                self._clients.pop(client_id, None)
            writer.close()
            try:
                await writer.wait_closed()
            except BaseException:
                pass
