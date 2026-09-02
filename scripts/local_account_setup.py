#!/usr/bin/env python3
"""One-shot local Apple Account and IDS setup for rustpush.

The Apple ID, password, and 2FA code are read from a real TTY with echo
disabled. No credential is accepted through argv or the environment. Only
Keychain-backed authenticated ciphertext is persisted.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import importlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from typing import Any

MAGIC = b"HERMES_RUSTPUSH_STATE\x01"
APS_PURPOSE = "aps-state.v1"
ACCOUNT_PURPOSE = "account-state.v1"
SCHEMA = 1


class SetupError(RuntimeError):
    pass


def ensure_private_dir(path: Path) -> None:
    if path.is_symlink():
        raise SetupError("state directory may not be a symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise SetupError("state path is not a directory")
    os.chmod(path, 0o700)


def atomic_private_write(path: Path, payload: bytes) -> None:
    ensure_private_dir(path.parent)
    if path.is_symlink():
        raise SetupError("state file may not be a symlink")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def load_binding(bindings: Path):
    if not (bindings / "rustpushgo.py").is_file():
        raise SetupError("rustpush Python binding is missing")
    if not (bindings / "librustpushgo.dylib").is_file():
        raise SetupError("rustpush native library is missing")
    sys.path.insert(0, str(bindings))
    return importlib.import_module("rustpushgo")


def require_envelope(protected: bytes, plaintext: bytes) -> bytes:
    protected = bytes(protected)
    if not protected.startswith(MAGIC) or protected == plaintext:
        raise SetupError("secure runtime returned an invalid state envelope")
    return protected


def protect_json(binding: Any, purpose: str, value: dict[str, Any]) -> bytes:
    raw = bytearray(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    try:
        protected = binding.protect_state(purpose, bytes(raw))
        return require_envelope(protected, bytes(raw))
    finally:
        for index in range(len(raw)):
            raw[index] = 0


def load_aps(binding: Any, path: Path):
    if not path.exists():
        return binding.WrappedApsState(None)
    if path.is_symlink():
        raise SetupError("APS state file may not be a symlink")
    protected = path.read_bytes()
    if not protected.startswith(MAGIC):
        raise SetupError("refusing plaintext or malformed APS state")
    raw = bytearray(binding.unprotect_state(APS_PURPOSE, protected))
    try:
        serialized = raw.decode("utf-8")
    finally:
        for index in range(len(raw)):
            raw[index] = 0
    return binding.WrappedApsState(serialized)


def write_status(path: Path, value: dict[str, Any]) -> None:
    safe = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    atomic_private_write(path, safe)


def classify_error(exc: BaseException) -> str:
    text = str(exc).lower()
    if "bad credential" in text or "incorrect" in text or "password" in text:
        return "bad_credentials"
    if "2fa" in text or "verification" in text or "code" in text:
        return "verification_failed"
    if "rate" in text or "too many" in text or "-22406" in text:
        return "apple_rate_limited"
    if "extra step" in text:
        return "unsupported_extra_step"
    if "network" in text or "timeout" in text or "http" in text:
        return "network_or_apple_service"
    return "unknown"


def account_bundle(config: Any, result: Any, persist_blob: str | None, mme_delegate: str | None) -> dict[str, Any]:
    account = result.account_persist
    if account is None:
        raise SetupError("login did not return restorable account state")
    users = result.users.to_string()
    identity = result.identity.to_string()
    device_id = config.get_device_id()
    required = (
        users,
        identity,
        device_id,
        account.username,
        account.hashed_password_hex,
        account.pet,
        account.adsid,
        account.dsid,
        account.spd_base64,
    )
    if any(not value for value in required):
        raise SetupError("login returned incomplete account or identity state")
    if not result.users.validate_keystore():
        raise SetupError("registered identity does not validate against the encrypted keystore")
    return {
        "schema": SCHEMA,
        "device_id": device_id,
        "ids_users": users,
        "ids_identity": identity,
        "account": {
            "username": account.username,
            "hashed_password_hex": account.hashed_password_hex,
            "pet": account.pet,
            "adsid": account.adsid,
            "dsid": account.dsid,
            "spd_base64": account.spd_base64,
            "persist_blob": persist_blob,
            "mme_delegate_json": mme_delegate,
        },
    }


async def run_setup(args: argparse.Namespace) -> dict[str, Any]:
    os.umask(0o077)
    state_dir = args.state_dir.expanduser().resolve()
    ensure_private_dir(state_dir)
    xdg_dir = state_dir / "xdg"
    ensure_private_dir(xdg_dir)
    os.environ["XDG_DATA_HOME"] = str(xdg_dir)
    os.environ["RUST_LOG"] = "off"
    os.environ["RUST_BACKTRACE"] = "0"

    status_path = state_dir / "setup-status.json"
    account_path = state_dir / "account-state.enc"
    aps_path = state_dir / "aps-state.enc"
    if account_path.exists() or account_path.is_symlink():
        raise SetupError("encrypted account state already exists; refusing to overwrite it")

    binding = load_binding(args.bindings.expanduser().resolve())
    binding.init_secure_runtime()
    config = binding.create_local_macos_config()
    aps_state = load_aps(binding, aps_path)

    print("Opening the persistent Apple Push Service connection…", flush=True)
    connection = await asyncio.wait_for(binding.connect(config, aps_state), timeout=args.connect_timeout)
    stage = "credentials"
    try:
        apple_id = getpass.getpass("Apple ID (hidden): ").strip()
        password = getpass.getpass("Apple ID password (hidden): ")
        if not apple_id or not password:
            raise SetupError("Apple ID and password are required")

        print("Authenticating directly with Apple…", flush=True)
        stage = "apple_login"
        try:
            session = await asyncio.wait_for(
                binding.login_start(apple_id, password, config, connection),
                timeout=args.login_timeout,
            )
        finally:
            apple_id = ""
            password = ""

        two_factor_used = session.needs_2fa()
        if two_factor_used:
            print("Apple requested two-factor authentication. Approve the sign-in on a trusted device, then enter the code here.", flush=True)
            stage = "two_factor"
            code = getpass.getpass("Verification code (hidden): ").strip()
            try:
                accepted = await asyncio.wait_for(session.submit_2fa(code), timeout=args.login_timeout)
            finally:
                code = ""
            if not accepted:
                raise SetupError("Apple did not accept the verification code")

        print("Registering the local rustpush identity with IDS…", flush=True)
        stage = "ids_registration"
        result = await asyncio.wait_for(
            session.finish(config, connection, None, None),
            timeout=args.registration_timeout,
        )
        token_provider = result.token_provider
        if token_provider is None:
            raise SetupError("login did not return a restorable token provider")
        persist_blob = await asyncio.wait_for(token_provider.get_account_persist_blob(), timeout=args.login_timeout)
        mme_delegate = await asyncio.wait_for(token_provider.get_mme_delegate_json(), timeout=args.login_timeout)
        bundle = account_bundle(config, result, persist_blob, mme_delegate)
        handle_count = len(result.users.get_handles())
        if handle_count < 1:
            raise SetupError("IDS registration returned no iMessage handles")

        stage = "state_commit"
        protected_account = protect_json(binding, ACCOUNT_PURPOSE, bundle)
        current_aps = await asyncio.wait_for(connection.state(), timeout=args.connect_timeout)
        aps_plain = bytearray(current_aps.to_string().encode("utf-8"))
        try:
            protected_aps = require_envelope(
                binding.protect_state(APS_PURPOSE, bytes(aps_plain)),
                bytes(aps_plain),
            )
        finally:
            for index in range(len(aps_plain)):
                aps_plain[index] = 0

        # APS first; account-state is the commit marker for a complete setup.
        atomic_private_write(aps_path, protected_aps)
        atomic_private_write(account_path, protected_account)
        result_summary = {
            "ok": True,
            "two_factor_used": two_factor_used,
            "ids_registered": True,
            "registered_handle_count": handle_count,
            "account_state_encrypted": True,
            "aps_state_encrypted": True,
            "keystore_valid": True,
        }
        write_status(status_path, result_summary)
        return result_summary
    except BaseException as exc:
        failure = {
            "ok": False,
            "stage": stage,
            "error_class": type(exc).__name__,
            "reason": classify_error(exc),
        }
        write_status(status_path, failure)
        raise
    finally:
        connection.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Secure local rustpush Apple Account setup")
    parser.add_argument("--bindings", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--connect-timeout", type=float, default=90.0)
    parser.add_argument("--login-timeout", type=float, default=300.0)
    parser.add_argument("--registration-timeout", type=float, default=300.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not sys.stdin.isatty():
        print("This setup must run in a local interactive terminal.", file=sys.stderr)
        return 2
    try:
        summary = asyncio.run(run_setup(args))
    except KeyboardInterrupt:
        print("\nSetup cancelled; no account state was committed.", file=sys.stderr)
        return 130
    except BaseException as exc:
        print(
            f"Setup stopped safely ({type(exc).__name__}, {classify_error(exc)}). "
            "No credentials were logged. Keep this window open and tell Hermes the stage shown in setup-status.json.",
            file=sys.stderr,
        )
        return 1
    print("RUSTPUSH_SETUP_OK " + json.dumps(summary, sort_keys=True, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
