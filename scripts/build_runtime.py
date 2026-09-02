#!/usr/bin/env python3
"""Reproducibly build the pinned local RustPush UniFFI runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / ".build"
DIST = ROOT / "dist" / "bindings"
PINS = ROOT / "pins.env"
CORTEN_PATCH = ROOT / "patches" / "corten-local.patch"
RUSTPUSH_SECURE_STATE_PATCH = ROOT / "patches" / "rustpush-secure-state.patch"
PRIVACY_PATCHER = ROOT / "scripts" / "apply_privacy_patches.py"


def run(args: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    print("+", " ".join(args), file=sys.stderr)
    proc = subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True)
    if proc.returncode:
        if proc.stdout:
            print(proc.stdout, file=sys.stderr)
        if proc.stderr:
            print(proc.stderr, file=sys.stderr)
        raise RuntimeError(f"command failed ({proc.returncode}): {args[0]}")
    if proc.stderr:
        print(proc.stderr, file=sys.stderr)
    return proc.stdout


def read_pins() -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in PINS.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        result[key] = value
    required = {"RUSTPUSH_REPO", "RUSTPUSH_COMMIT", "CORTEN_REPO", "CORTEN_COMMIT", "UNIFFI_VERSION"}
    missing = required - result.keys()
    if missing:
        raise RuntimeError(f"missing pins: {sorted(missing)}")
    return result


def clone_exact(url: str, commit: str, target: Path) -> None:
    if target.exists():
        if not (target / ".git").is_dir():
            raise RuntimeError(f"refusing to replace non-git path: {target}")
        origin = run(["git", "remote", "get-url", "origin"], cwd=target).strip()
        head = run(["git", "rev-parse", "HEAD"], cwd=target).strip()
        if origin == url and head == commit:
            return
        shutil.rmtree(target)
    run(["git", "clone", "--filter=blob:none", "--no-checkout", url, str(target)])
    run(["git", "fetch", "--depth", "1", "origin", commit], cwd=target)
    run(["git", "checkout", "--detach", commit], cwd=target)
    actual = run(["git", "rev-parse", "HEAD"], cwd=target).strip()
    if actual != commit:
        raise RuntimeError(f"pin mismatch: wanted {commit}, got {actual}")


def apply_patch(repo: Path, patch: Path) -> None:
    check = subprocess.run(["git", "apply", "--check", str(patch)], cwd=repo, text=True, capture_output=True)
    if check.returncode == 0:
        run(["git", "apply", str(patch)], cwd=repo)
        return
    reverse = subprocess.run(["git", "apply", "--reverse", "--check", str(patch)], cwd=repo, text=True, capture_output=True)
    if reverse.returncode == 0:
        return
    raise RuntimeError(f"patch no longer applies cleanly: {patch}\n{check.stderr}")


def find_protoc() -> Path:
    direct = shutil.which("protoc")
    if direct:
        return Path(direct)
    brew = shutil.which("brew")
    if brew:
        prefix = run([brew, "--prefix", "protobuf"]).strip()
        candidate = Path(prefix) / "bin" / "protoc"
        if candidate.is_file():
            return candidate
    raise RuntimeError("protoc is required; install Homebrew protobuf")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--clean", action="store_true", help="remove the pinned build checkout first")
    args = parser.parse_args()
    pins = read_pins()
    BUILD.mkdir(parents=True, exist_ok=True)
    source = BUILD / "corten"
    if args.clean and source.exists():
        shutil.rmtree(source)

    clone_exact(pins["CORTEN_REPO"], pins["CORTEN_COMMIT"], source)
    apply_patch(source, CORTEN_PATCH)
    wrapper_pin = (source / "third_party" / "rustpush-upstream.sha").read_text().strip()
    if wrapper_pin != pins["RUSTPUSH_COMMIT"]:
        raise RuntimeError(f"wrapper RustPush pin {wrapper_pin} != audited pin {pins['RUSTPUSH_COMMIT']}")

    cargo = Path.home() / ".cargo" / "bin" / "cargo"
    if not cargo.is_file():
        raise RuntimeError("Rust toolchain missing at ~/.cargo/bin/cargo")
    protoc = find_protoc()
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{cargo.parent}:{protoc.parent}:{env.get('PATH', '')}",
            "PROTOC": str(protoc),
            "RUST_LOG": "off",
            # One pinned upstream submodule uses an SSH-style GitHub URL even
            # though it is public. Keep local builds keyless and noninteractive.
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "url.https://github.com/.insteadOf",
            "GIT_CONFIG_VALUE_0": "git@github.com:",
            "GIT_CONFIG_KEY_1": "url.https://github.com/.insteadOf",
            "GIT_CONFIG_VALUE_1": "ssh://git@github.com/",
        }
    )

    run(["make", "ensure-rustpush-source", f"UPSTREAM_REPO={pins['RUSTPUSH_REPO']}"], cwd=source, env=env)
    rustpush = source / "third_party" / "rustpush-upstream"
    actual_rustpush = run(["git", "rev-parse", "HEAD"], cwd=rustpush).strip()
    if actual_rustpush != pins["RUSTPUSH_COMMIT"]:
        raise RuntimeError(f"RustPush pin mismatch: {actual_rustpush}")
    apply_patch(rustpush, RUSTPUSH_SECURE_STATE_PATCH)
    run([sys.executable, str(PRIVACY_PATCHER), str(rustpush)], env=env)
    rustpushgo_dir = source / "pkg" / "rustpushgo"
    build_env = env | {"MACOSX_DEPLOYMENT_TARGET": "12.0"}
    run(
        [
            str(cargo),
            "build",
            "--release",
            "--locked",
            "--no-default-features",
            "--features",
            "nac-apple-framework",
        ],
        cwd=rustpushgo_dir,
        env=build_env,
    )

    dylib = rustpushgo_dir / "target" / "release" / "librustpushgo.dylib"
    if not dylib.is_file():
        raise RuntimeError(f"missing build output: {dylib}")

    bindgen_dir = ROOT / "tools" / "uniffi-bindgen25"
    bindgen_target = BUILD / "uniffi-bindgen-target"
    bindgen_env = env | {"CARGO_TARGET_DIR": str(bindgen_target)}
    run([str(cargo), "build", "--release", "--locked"], cwd=bindgen_dir, env=bindgen_env)
    bindgen = bindgen_target / "release" / "hermes-rustpush-uniffi-bindgen"

    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir(parents=True, mode=0o755)
    run(
        [
            str(bindgen),
            "generate",
            "--library",
            "--language",
            "python",
            "--out-dir",
            str(DIST),
            str(dylib),
        ],
        cwd=rustpushgo_dir,
        env=env,
    )
    final_dylib = DIST / dylib.name
    shutil.copy2(dylib, final_dylib)
    run(["install_name_tool", "-id", "@rpath/librustpushgo.dylib", str(final_dylib)])
    run(["codesign", "--force", "--sign", "-", "--timestamp=none", str(final_dylib)])
    binary_strings = run(["/usr/bin/strings", "-a", str(final_dylib)])
    forbidden_binary_strings = (
        "LOGIN-DEBUG",
        "RUSTPUSH_LOG_REGISTER_BODY",
        "REGISTER body XML",
        "Result {:?} data {} hash {} iv {} key {} request {}",
    )
    leaked = [item for item in forbidden_binary_strings if item in binary_strings]
    if leaked:
        raise RuntimeError(f"secret-bearing diagnostics remain in release library: {leaked}")
    run(["codesign", "--verify", "--strict", "--verbose=2", str(final_dylib)])
    final_dylib.chmod(0o555)
    for generated in DIST.glob("*.py"):
        generated.chmod(0o444)

    generated = sorted(p.name for p in DIST.iterdir())
    if "rustpushgo.py" not in generated or "librustpushgo.dylib" not in generated:
        raise RuntimeError(f"unexpected generated output: {generated}")
    result = {
        "rustpush_commit": pins["RUSTPUSH_COMMIT"],
        "wrapper_commit": pins["CORTEN_COMMIT"],
        "files": generated,
        "dylib_sha256": sha256(final_dylib),
        "dylib_bytes": final_dylib.stat().st_size,
    }
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
