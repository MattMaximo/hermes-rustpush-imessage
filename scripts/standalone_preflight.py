#!/usr/bin/env python3
"""Standalone RustPush/APNs preflight with no Apple ID authentication."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BINDINGS = ROOT / "dist" / "bindings"
DEFAULT_STATE = Path.home() / ".hermes" / "state" / "rustpush"


def private_dir(path: Path) -> Path:
    path = path.expanduser()
    if path.is_symlink():
        raise RuntimeError(f"state directory must not be a symlink: {path}")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    stat = path.stat()
    if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise RuntimeError(f"unsafe state-directory ownership or mode: {path}")
    return path.resolve()


def atomic_private_write(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def load_binding(bindings: Path):
    bindings = bindings.expanduser().resolve()
    module = bindings / "rustpushgo.py"
    dylib = bindings / "librustpushgo.dylib"
    if not module.is_file() or not dylib.is_file():
        raise RuntimeError(f"RustPush bindings are incomplete: {bindings}")
    sys.path.insert(0, str(bindings))
    import rustpushgo  # type: ignore

    return rustpushgo


async def preflight(binding, state_dir: Path, timeout: float, settle: float) -> dict[str, object]:
    os.environ["XDG_DATA_HOME"] = str(state_dir / "xdg")
    os.environ["RUST_LOG"] = "off"
    (state_dir / "xdg").mkdir(mode=0o700, exist_ok=True)
    os.chmod(state_dir / "xdg", 0o700)

    binding.init_secure_runtime()
    binding.init_logger()
    config = binding.create_local_macos_config()
    aps_path = state_dir / "aps-state.enc"
    serialized = None
    if aps_path.exists():
        plaintext = binding.unprotect_state("aps-state.v1", aps_path.read_bytes())
        try:
            serialized = bytes(plaintext).decode("utf-8")
        finally:
            if isinstance(plaintext, bytearray):
                plaintext[:] = b"\x00" * len(plaintext)
    aps_state = binding.WrappedApsState(serialized)

    started = time.perf_counter_ns()
    connection = await asyncio.wait_for(binding.connect(config, aps_state), timeout=timeout)
    connected = time.perf_counter_ns()
    try:
        if settle:
            await asyncio.sleep(settle)
        current_state = await asyncio.wait_for(connection.state(), timeout=timeout)
        serialized_state = current_state.to_string().encode("utf-8")
        protected_state = bytes(binding.protect_state("aps-state.v1", serialized_state))
        atomic_private_write(aps_path, protected_state)
    finally:
        connection.close()
    finished = time.perf_counter_ns()

    return {
        "ok": True,
        "transport": "apple-push-service",
        "authenticated": False,
        "connect_ms": round((connected - started) / 1_000_000, 3),
        "total_ms": round((finished - started) / 1_000_000, 3),
        "aps_state_persisted": aps_path.is_file() and (aps_path.stat().st_mode & 0o077) == 0,
        "aps_state_encrypted": aps_path.is_file()
        and aps_path.read_bytes().startswith(b"HERMES_RUSTPUSH_STATE\x01"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bindings", type=Path, default=DEFAULT_BINDINGS)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--settle", type=float, default=3.0)
    args = parser.parse_args()

    os.umask(0o077)
    state_dir = private_dir(args.state_dir)
    try:
        binding = load_binding(args.bindings)
        result = asyncio.run(preflight(binding, state_dir, args.timeout, args.settle))
    except BaseException as exc:
        result = {"ok": False, "error_type": type(exc).__name__}
        print(json.dumps(result, separators=(",", ":")))
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
