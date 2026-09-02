#!/usr/bin/env python3
"""Query the local sidecar without importing Hermes or exposing state."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any, Dict

PROTOCOL_VERSION = 1
DEFAULT_SOCKET = Path.home() / ".hermes" / "rustpush" / "run" / "bridge.sock"
SAFE_HEALTH_FIELDS = (
    "ready",
    "protocol",
    "capabilities",
    "handle_count",
    "pending_events",
    "started_ms",
)


async def query(socket_path: Path, timeout: float) -> Dict[str, Any]:
    reader, writer = await asyncio.wait_for(
        asyncio.open_unix_connection(str(socket_path.expanduser().resolve())),
        timeout=timeout,
    )
    try:
        request = {
            "op": "hello",
            "protocol": PROTOCOL_VERSION,
            "request_id": "healthcheck",
        }
        writer.write(json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\n")
        await asyncio.wait_for(writer.drain(), timeout=timeout)
        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        response = json.loads(line)
        if not isinstance(response, dict) or not response.get("ok"):
            raise RuntimeError("sidecar returned an unhealthy response")
        result = response.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("sidecar returned an invalid health payload")
        return {key: result.get(key) for key in SAFE_HEALTH_FIELDS}
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (AttributeError, OSError):
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET)
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args()
    try:
        result = asyncio.run(query(args.socket, args.timeout))
    except (OSError, ValueError, RuntimeError, asyncio.TimeoutError) as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__}, sort_keys=True))
        return 1
    print(json.dumps({"ok": bool(result.get("ready")), **result}, sort_keys=True))
    return 0 if result.get("ready") else 1


if __name__ == "__main__":
    raise SystemExit(main())
