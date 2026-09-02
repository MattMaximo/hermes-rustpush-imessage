#!/usr/bin/env python3
"""Install the local RustPush sidecar and Hermes platform plugin."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
LABEL = "ai.hermes.rustpush-sidecar"
HOME = Path.home()
BASE = HOME / ".hermes" / "rustpush"
APP = BASE / "app"
RUNTIME = BASE / "runtime"
STATE = BASE / "state"
RUN = BASE / "run"
SOCKET = RUN / "bridge.sock"
PLUGIN_DEST = HOME / ".hermes" / "plugins" / "imessage_cli-platform"
PLIST = HOME / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir() or path.stat().st_uid != os.getuid():
        raise RuntimeError("unsafe install directory")
    path.chmod(0o700)


def atomic_copy(source: Path, destination: Path, mode: int) -> None:
    if source.is_symlink() or not source.is_file():
        raise RuntimeError("invalid install source")
    private_dir(destination.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    temp_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_file:
            shutil.copyfileobj(input_file, output)
            output.flush()
            os.fsync(output.fileno())
        temp_path.chmod(mode)
        os.replace(temp_path, destination)
        destination.chmod(mode)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def atomic_bytes(payload: bytes, destination: Path, mode: int) -> None:
    private_dir(destination.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    temp_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        temp_path.chmod(mode)
        os.replace(temp_path, destination)
        destination.chmod(mode)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def verify_sources() -> None:
    required = [
        ROOT / "sidecar" / "main.py",
        ROOT / "sidecar" / "runtime.py",
        ROOT / "sidecar" / "server.py",
        ROOT / "sidecar" / "core.py",
        ROOT / "dist" / "bindings" / "rustpushgo.py",
        ROOT / "dist" / "bindings" / "librustpushgo.dylib",
        ROOT / "plugin.yaml",
        ROOT / "adapter.py",
    ]
    if any(not path.is_file() or path.is_symlink() for path in required):
        raise RuntimeError("build output is incomplete")


def deploy_files() -> None:
    for directory in (BASE, APP, RUNTIME, STATE, RUN):
        private_dir(directory)
    for name in ("__init__.py", "core.py", "runtime.py", "server.py", "main.py"):
        atomic_copy(ROOT / "sidecar" / name, APP / name, 0o500 if name == "main.py" else 0o400)
    atomic_copy(ROOT / "dist" / "bindings" / "rustpushgo.py", RUNTIME / "rustpushgo.py", 0o400)
    atomic_copy(
        ROOT / "dist" / "bindings" / "librustpushgo.dylib",
        RUNTIME / "librustpushgo.dylib",
        0o500,
    )
    # ``hermes plugins install`` clones this repository directly into
    # PLUGIN_DEST. Preserve that checkout so future plugin updates remain
    # possible. When invoked from a separate source checkout, copy only the
    # actual plugin entrypoint files into Hermes's plugin directory.
    if ROOT.resolve() != PLUGIN_DEST.resolve():
        private_dir(PLUGIN_DEST)
        for name in ("__init__.py", "adapter.py", "plugin.yaml", "after-install.md"):
            source = ROOT / name
            if source.exists():
                atomic_copy(source, PLUGIN_DEST / name, 0o400)


def launch_agent_payload() -> bytes:
    payload = {
        "Label": LABEL,
        "ProgramArguments": [
            "/usr/bin/python3",
            str(APP / "main.py"),
            "--state-dir",
            str(STATE),
            "--runtime-dir",
            str(RUNTIME),
            "--socket",
            str(SOCKET),
        ],
        "WorkingDirectory": str(APP),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 5,
        "ProcessType": "Background",
        "Umask": 0o077,
        "EnvironmentVariables": {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "RUST_LOG": "off",
        },
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)


def launchctl(*arguments: str, allow_failure: bool = False) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["/bin/launchctl", *arguments],
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode and not allow_failure:
        raise RuntimeError("launchd operation failed")
    return result


def start_service() -> None:
    domain = f"gui/{os.getuid()}"
    if launchctl("print", f"{domain}/{LABEL}", allow_failure=True).returncode == 0:
        launchctl("bootout", domain, str(PLIST))
    launchctl("bootstrap", domain, str(PLIST))
    launchctl("enable", f"{domain}/{LABEL}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", action="store_true")
    args = parser.parse_args()
    if sys.platform != "darwin" or os.geteuid() == 0:
        raise SystemExit("must run as a non-root macOS user")
    verify_sources()
    deploy_files()
    private_dir(PLIST.parent)
    atomic_bytes(launch_agent_payload(), PLIST, 0o600)
    if args.start:
        start_service()
    print("installed local RustPush sidecar and Hermes plugin")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print("local RustPush installation failed", file=sys.stderr)
        raise SystemExit(1)
