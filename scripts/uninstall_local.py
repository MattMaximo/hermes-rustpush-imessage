#!/usr/bin/env python3
"""Stop and remove the local RustPush sidecar while preserving identity state."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

HOME = Path.home().resolve()
BASE = HOME / ".hermes" / "rustpush"
APP = BASE / "app"
RUNTIME = BASE / "runtime"
RUN = BASE / "run"
STATE = BASE / "state"
PLIST = HOME / "Library" / "LaunchAgents" / "ai.hermes.rustpush-sidecar.plist"
LABEL = "ai.hermes.rustpush-sidecar"
KEYCHAIN_SERVICE = "ai.hermes.rustpush"
KEYCHAIN_ACCOUNT = "state-encryption-key.v1"


def refuse_unsafe_environment() -> None:
    if sys.platform != "darwin":
        raise SystemExit("this uninstaller supports macOS only")
    if os.geteuid() == 0:
        raise SystemExit("refusing to run as root")
    if BASE.is_symlink():
        raise SystemExit(f"refusing symlinked base directory: {BASE}")


def remove_owned_tree(path: Path) -> None:
    if not path.exists():
        return
    if path.is_symlink():
        raise SystemExit(f"refusing symlinked path: {path}")
    if path.stat().st_uid != os.geteuid():
        raise SystemExit(f"refusing path not owned by current user: {path}")
    if path.parent != BASE:
        raise SystemExit(f"refusing unexpected path: {path}")
    shutil.rmtree(path)


def unload_service() -> None:
    domain = f"gui/{os.getuid()}"
    subprocess.run(
        ["/bin/launchctl", "bootout", domain, str(PLIST)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if PLIST.exists():
        if PLIST.is_symlink() or PLIST.stat().st_uid != os.geteuid():
            raise SystemExit(f"refusing unsafe launch agent path: {PLIST}")
        PLIST.unlink()


def purge_keychain_item() -> None:
    result = subprocess.run(
        [
            "/usr/bin/security",
            "delete-generic-password",
            "-s",
            KEYCHAIN_SERVICE,
            "-a",
            KEYCHAIN_ACCOUNT,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode not in (0, 44):
        raise SystemExit("could not remove the RustPush Keychain item")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--purge-state",
        action="store_true",
        help="also delete encrypted Apple account/device state and its Keychain key",
    )
    args = parser.parse_args()
    refuse_unsafe_environment()
    unload_service()
    for path in (APP, RUNTIME, RUN):
        remove_owned_tree(path)
    if args.purge_state:
        remove_owned_tree(STATE)
        purge_keychain_item()
    state_status = "removed" if args.purge_state else "preserved"
    print(f"removed {LABEL}; encrypted identity state {state_status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
