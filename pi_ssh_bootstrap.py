#!/usr/bin/env python3
"""Initial Raspberry Pi SSH bootstrap helper.

Creates a simple markdown file on the Pi (default: ~/Documents/verifybot_hello.md)
to validate SSH connectivity and remote file-write behavior.
"""

import argparse
import importlib.util
import os
import platform
import shutil
import sys
import sysconfig
from pathlib import Path


def _ensure_stdlib_selectors():
    """Force stdlib selectors module so local selectors.py does not shadow it."""
    stdlib_path = Path(sysconfig.get_paths()["stdlib"]) / "selectors.py"
    spec = importlib.util.spec_from_file_location("selectors", stdlib_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    sys.modules["selectors"] = module


_ensure_stdlib_selectors()
import subprocess


def load_env_file(path: Path) -> dict:
    values = {}
    if not path.exists():
        return values

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def resolve_value(cli_val: str | None, env_key: str, loaded: dict) -> str | None:
    if cli_val:
        return cli_val
    if os.getenv(env_key):
        return os.getenv(env_key)
    return loaded.get(env_key)




def normalize_target(host: str, user: str) -> tuple[str, str]:
    """Support host values passed as either host or user@host."""
    if "@" not in host:
        return host, user

    parsed_user, parsed_host = host.split("@", 1)
    if not user:
        return parsed_host, parsed_user
    if user != parsed_user:
        print(f"[WARN] Host included user '{parsed_user}', overriding explicit user '{user}'.")
        return parsed_host, parsed_user
    return parsed_host, user

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Create a test markdown file on Raspberry Pi over SSH")
    p.add_argument("--secrets-file", default=".secrets/pi_ssh.env", help="Path to ignored credential file")
    p.add_argument("--host", help="Raspberry Pi SSH host")
    p.add_argument("--user", help="Raspberry Pi SSH username")
    p.add_argument("--password", help="Raspberry Pi SSH password (optional if key auth works)")
    p.add_argument("--remote-path", default="Documents/verifybot_hello.md", help="Remote file path (relative to home or absolute)")
    p.add_argument("--message", default="# hi\n", help="File contents to write")
    return p


def main() -> int:
    args = build_parser().parse_args()
    secrets = load_env_file(Path(args.secrets_file))

    host = resolve_value(args.host, "PI_HOST", secrets)
    user = resolve_value(args.user, "PI_USER", secrets)
    password = resolve_value(args.password, "PI_PASSWORD", secrets)

    if not host or not user:
        print("[ERROR] Missing PI host/user. Set in CLI args, env, or secrets file.")
        return 2

    host, user = normalize_target(host, user)

    remote_path = args.remote_path
    if not remote_path.startswith("/"):
        remote_path = f"~/{remote_path}"

    remote_dir = str(Path(remote_path).parent)
    remote_cmd = f"mkdir -p {remote_dir} && cat > {remote_path}"

    ssh_cmd = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=accept-new",
        f"{user}@{host}",
        remote_cmd,
    ]

    if password:
        sshpass_path = shutil.which("sshpass")
        if sshpass_path:
            ssh_cmd = ["sshpass", "-p", password] + ssh_cmd
        else:
            print("[WARN] sshpass not found; falling back to interactive SSH password prompt.")
            print("       This is expected on Windows unless sshpass is installed.")
            print("       When prompted by ssh, paste your Pi password manually.")

    print(f"[SSH] Writing markdown file to {user}@{host}:{remote_path}")
    capture = platform.system().lower() != "windows"
    result = subprocess.run(
        ssh_cmd,
        input=args.message,
        text=True,
        capture_output=capture,
    )

    if result.returncode != 0:
        print("[FAIL] SSH write failed")
        if capture and result.stderr.strip():
            print(result.stderr.strip())
        return result.returncode

    print("[OK] Remote markdown file created successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())