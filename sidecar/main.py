#!/usr/bin/env python3
"""Run or safely probe the local Hermes RustPush sidecar."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from core import PROTOCOL_VERSION, ensure_private_dir  # noqa: E402
from runtime import RustPushRuntime, load_binding  # noqa: E402
from server import LocalProtocolServer  # noqa: E402

DEFAULT_ROOT = Path.home() / ".hermes" / "rustpush" / "state"
DEFAULT_RUNTIME = Path.home() / ".hermes" / "rustpush" / "runtime"
DEFAULT_SOCKET = Path.home() / ".hermes" / "rustpush" / "run" / "bridge.sock"


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Hermes local RustPush transport")
    value.add_argument("--state-dir", type=Path, default=DEFAULT_ROOT)
    value.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME)
    value.add_argument("--socket", type=Path, default=DEFAULT_SOCKET)
    mode = value.add_mutually_exclusive_group()
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--probe", action="store_true")
    return value


async def probe(socket_path: Path) -> int:
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(socket_path)), timeout=3)
        writer.write(json.dumps({"op": "hello", "protocol": PROTOCOL_VERSION, "request_id": "probe-hello"}).encode() + b"\n")
        await writer.drain()
        hello = json.loads(await asyncio.wait_for(reader.readline(), timeout=3))
        if not hello.get("ok"):
            return 1
        writer.write(json.dumps({"op": "health", "request_id": "probe-health"}).encode() + b"\n")
        await writer.drain()
        while True:
            response = json.loads(await asyncio.wait_for(reader.readline(), timeout=3))
            if response.get("request_id") == "probe-health":
                safe = response.get("result") if response.get("ok") else {"ready": False}
                print(json.dumps(safe, sort_keys=True, separators=(",", ":")))
                return 0 if safe.get("ready") else 1
    except BaseException:
        print('{"ready":false}')
        return 1
    finally:
        if "writer" in locals():
            writer.close()
            try:
                await writer.wait_closed()
            except BaseException:
                pass


async def run(args: argparse.Namespace) -> int:
    os.umask(0o077)
    ensure_private_dir(args.state_dir)
    ensure_private_dir(args.runtime_dir)
    ensure_private_dir(args.socket.parent)
    binding = load_binding(args.runtime_dir)
    runtime = RustPushRuntime(binding, args.state_dir)
    server = LocalProtocolServer(runtime, args.socket)
    try:
        await runtime.start()
        if args.preflight:
            health = await runtime.dispatch({"op": "health"})
            print(json.dumps(health, sort_keys=True, separators=(",", ":")))
            return 0
        await server.start()
        stopped = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, stopped.set)
            except NotImplementedError:
                pass
        await stopped.wait()
        return 0
    except BaseException:
        print("rustpush-sidecar: startup failed", file=sys.stderr)
        return 1
    finally:
        await server.stop()
        await runtime.stop()


def main() -> int:
    args = parser().parse_args()
    if args.probe:
        return asyncio.run(probe(args.socket.expanduser().resolve()))
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
