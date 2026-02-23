#!/usr/bin/env python3
"""
ssh_skill.py -- SSH into Raspberry Pi and run commands / transfer files.

Loads credentials from .env (PI_USER, PI_HOST, PI_PASSWORD).

Usage (standalone):
    python -m skills.ssh_skill --test
    python -m skills.ssh_skill --run "ls -la ~/Documents"

Usage (imported):
    from skills.ssh_skill import ssh_run, sftp_upload, REMOTE_WORK_DIR
    result = ssh_run("uname -a", timeout=10)
"""

import argparse
import os
import sys
import threading
from pathlib import Path
from datetime import datetime

try:
    import paramiko
except ImportError:
    print("[ERROR] paramiko not installed. Run: pip install paramiko")
    sys.exit(1)

# ---------------------------------------------------------------------------
# .env loading
# ---------------------------------------------------------------------------

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def _load_env():
    if not ENV_FILE.exists():
        print(f"[ERROR] {ENV_FILE} not found. Create it with PI_USER, PI_HOST, PI_PASSWORD.")
        sys.exit(1)
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def _get_creds():
    _load_env()
    user = os.environ.get("PI_USER")
    host = os.environ.get("PI_HOST")
    password = os.environ.get("PI_PASSWORD")
    if not all([user, host, password]):
        print("[ERROR] Missing PI_USER, PI_HOST, or PI_PASSWORD in .env")
        sys.exit(1)
    return user, host, password


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def _connect() -> paramiko.SSHClient:
    user, host, password = _get_creds()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(hostname=host, username=user, password=password, timeout=10)
    return client


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------

REMOTE_WORK_DIR = "/home/scoobyxd/Documents"


def ssh_run(command: str, timeout: int = 30) -> dict:
    """Run a command on Pi via SSH.

    Returns {stdout, stderr, exit_code, success, timed_out}.
    If the command exceeds `timeout`, returns partial output with timed_out=True.
    """
    try:
        client = _connect()
        transport = client.get_transport()
        channel = transport.open_session()
        channel.settimeout(timeout)
        channel.exec_command(command)

        exit_code = [None]

        def wait_for_exit():
            try:
                exit_code[0] = channel.recv_exit_status()
            except Exception:
                pass

        waiter = threading.Thread(target=wait_for_exit, daemon=True)
        waiter.start()
        waiter.join(timeout=timeout)

        if waiter.is_alive():
            # Timed out — grab whatever partial output exists
            out, err = "", ""
            try:
                if channel.recv_ready():
                    out = channel.recv(65536).decode("utf-8", errors="replace")
            except Exception:
                pass
            try:
                if channel.recv_stderr_ready():
                    err = channel.recv_stderr(65536).decode("utf-8", errors="replace")
            except Exception:
                pass
            try:
                channel.close()
            except Exception:
                pass
            client.close()
            return {"stdout": out, "stderr": err, "exit_code": -1,
                    "success": False, "timed_out": True}

        # Completed within timeout
        out, err = "", ""
        try:
            out = channel.recv(10 * 1024 * 1024).decode("utf-8", errors="replace")
        except Exception:
            pass
        try:
            err = channel.recv_stderr(10 * 1024 * 1024).decode("utf-8", errors="replace")
        except Exception:
            pass
        channel.close()
        client.close()

        ec = exit_code[0] if exit_code[0] is not None else -1
        return {"stdout": out, "stderr": err, "exit_code": ec,
                "success": ec == 0, "timed_out": False}

    except paramiko.AuthenticationException:
        return {"stdout": "", "stderr": "[ERROR] Auth failed -- check .env",
                "exit_code": -1, "success": False, "timed_out": False}
    except Exception as e:
        return {"stdout": "", "stderr": f"[ERROR] {e}",
                "exit_code": -1, "success": False, "timed_out": False}


def ssh_run_detached(command: str) -> dict:
    """Fire-and-forget: launch a command via nohup. Returns immediately with PID."""
    wrapped = f"nohup {command} > /dev/null 2>&1 & echo $!"
    result = ssh_run(wrapped, timeout=10)
    return {"success": result["success"], "pid": result.get("stdout", "").strip(),
            "stderr": result.get("stderr", "")}


def sftp_upload(local_path: str, remote_path: str) -> dict:
    """Upload a file to Pi."""
    try:
        client = _connect()
        sftp = client.open_sftp()
        sftp.put(local_path, remote_path)
        sftp.close()
        client.close()
        return {"success": True, "stderr": ""}
    except Exception as e:
        return {"success": False, "stderr": f"[ERROR] Upload failed: {e}"}


def sftp_download(remote_path: str, local_path: str) -> dict:
    """Download a file from Pi."""
    try:
        client = _connect()
        sftp = client.open_sftp()
        sftp.get(remote_path, local_path)
        sftp.close()
        client.close()
        return {"success": True, "stderr": ""}
    except Exception as e:
        return {"success": False, "stderr": f"[ERROR] Download failed: {e}"}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="SSH Skill -- Raspberry Pi")
    parser.add_argument("--test", action="store_true", help="Quick connectivity test")
    parser.add_argument("--run", type=str, help="Run a command on Pi")
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()

    if args.test:
        print("[TEST] Connecting to Pi...")
        r = ssh_run("hostname && uname -srm && python3 --version", timeout=10)
        if r["success"]:
            print(f"[OK] Connected:\n{r['stdout']}")
        else:
            print(f"[FAIL] {r['stderr']}")
    elif args.run:
        r = ssh_run(args.run, timeout=args.timeout)
        print(r["stdout"])
        if r["stderr"]:
            print(r["stderr"], file=sys.stderr)
        sys.exit(r["exit_code"] if r["exit_code"] >= 0 else 1)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
