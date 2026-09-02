from __future__ import annotations

import asyncio
import importlib.util
import os
from pathlib import Path
import stat
import tempfile
import types
import unittest

MODULE_PATH = Path(__file__).parents[1] / "scripts" / "standalone_preflight.py"
SPEC = importlib.util.spec_from_file_location("standalone_preflight", MODULE_PATH)
assert SPEC and SPEC.loader
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)


class FakeState:
    def __init__(self, serialized: str | None):
        self.serialized = serialized

    def to_string(self) -> str:
        return "private-aps-state"


class FakeConnection:
    def __init__(self):
        self.closed = False

    async def state(self):
        return FakeState(None)

    def close(self):
        self.closed = True


class FakeBinding:
    WrappedApsState = FakeState

    def __init__(self):
        self.connection = FakeConnection()
        self.initialized = False
        self.secure_initialized = False

    def init_secure_runtime(self):
        self.secure_initialized = True

    def protect_state(self, purpose, plaintext):
        if not self.secure_initialized:
            raise RuntimeError("secure runtime is not initialized")
        return b"HERMES_RUSTPUSH_STATE\x01" + purpose.encode() + b"\x00" + bytes(plaintext)

    def unprotect_state(self, purpose, protected):
        prefix = b"HERMES_RUSTPUSH_STATE\x01" + purpose.encode() + b"\x00"
        if not bytes(protected).startswith(prefix):
            raise RuntimeError("invalid protected state")
        return bytes(protected)[len(prefix) :]

    def init_logger(self):
        self.initialized = True

    def create_local_macos_config(self):
        return object()

    async def connect(self, config, state):
        return self.connection


class StandalonePreflightTests(unittest.TestCase):
    def test_preflight_persists_private_state_and_closes(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = preflight.private_dir(Path(temporary) / "state")
            fake = FakeBinding()
            result = asyncio.run(preflight.preflight(fake, state, timeout=1, settle=0))
            aps = state / "aps-state.enc"
            self.assertTrue(result["ok"])
            self.assertTrue(result["aps_state_persisted"])
            self.assertTrue(result["aps_state_encrypted"])
            self.assertNotEqual(aps.read_bytes(), b"private-aps-state")
            self.assertEqual(fake.unprotect_state("aps-state.v1", aps.read_bytes()), b"private-aps-state")
            self.assertEqual(stat.S_IMODE(aps.stat().st_mode), 0o600)
            self.assertTrue(fake.initialized)
            self.assertTrue(fake.secure_initialized)
            self.assertTrue(fake.connection.closed)

    def test_private_dir_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            target.mkdir()
            link = root / "state"
            link.symlink_to(target, target_is_directory=True)
            with self.assertRaises(RuntimeError):
                preflight.private_dir(link)

    def test_atomic_write_replaces_without_broad_permissions(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "state"
            preflight.atomic_private_write(target, b"one")
            preflight.atomic_private_write(target, b"two")
            self.assertEqual(target.read_bytes(), b"two")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
