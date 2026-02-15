#!/usr/bin/env python3
"""
ssh_skill.py -- SSH into Raspberry Pi and run commands / transfer files.

Loads credentials from .env (PI_USER, PI_HOST, PI_PASSWORD).
Uses paramiko -- pure Python SSH, works on Windows/Mac/Linux.

Usage:
    python -m skills.ssh_skill --test
    python -m skills.ssh_skill --run "ls -la ~/Documents"
    python -m skills.ssh_skill --deploy programs/word_generator.py

Requires: pip install paramiko
"""

import argparse
import os
import sys
import time
import threading
from pathlib import Path
from datetime import datetime

try:
    import paramiko
except ImportError:
    print("[ERROR] paramiko not installed. Run: pip install paramiko")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Load .env
# ---------------------------------------------------------------------------

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def load_env():
    """Load key=value pairs from .env into os.environ."""
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


def get_creds():
    """Return (user, host, password) from environment."""
    load_env()
    user = os.environ.get("PI_USER")
    host = os.environ.get("PI_HOST")
    password = os.environ.get("PI_PASSWORD")
    if not all([user, host, password]):
        print("[ERROR] Missing PI_USER, PI_HOST, or PI_PASSWORD in .env")
        sys.exit(1)
    return user, host, password


# ---------------------------------------------------------------------------
# SSH / SFTP wrappers
# ---------------------------------------------------------------------------

def _connect() -> paramiko.SSHClient:
    """Create and return a connected SSH client."""
    user, host, password = get_creds()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(hostname=host, username=user, password=password, timeout=10)
    return client


def ssh_run(command: str, timeout: int = 30) -> dict:
    """Run a command on the Pi via SSH. Returns {stdout, stderr, exit_code, success, timed_out}.

    IMPORTANT: This function properly enforces the timeout. If the command takes
    longer than `timeout` seconds, it returns with timed_out=True and whatever
    partial output was captured. The remote process is NOT killed (it keeps running
    on the Pi) -- use ssh_run_detached() for fire-and-forget commands.
    """
    try:
        client = _connect()
        transport = client.get_transport()
        channel = transport.open_session()
        channel.settimeout(timeout)
        channel.exec_command(command)

        # Wait for exit status with timeout using a background thread
        exit_code = [None]
        timed_out = [False]

        def wait_for_exit():
            try:
                exit_code[0] = channel.recv_exit_status()
            except Exception:
                pass

        waiter = threading.Thread(target=wait_for_exit, daemon=True)
        waiter.start()
        waiter.join(timeout=timeout)

        if waiter.is_alive():
            # Command is still running -- timed out
            timed_out[0] = True
            # Read whatever partial output is available
            out = ""
            err = ""
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
            return {
                "stdout": out,
                "stderr": err,
                "exit_code": -1,
                "success": False,
                "timed_out": True,
            }

        # Command finished within timeout
        out = ""
        err = ""
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
        return {
            "stdout": out,
            "stderr": err,
            "exit_code": ec,
            "success": ec == 0,
            "timed_out": False,
        }
    except paramiko.AuthenticationException:
        return {
            "stdout": "",
            "stderr": "[ERROR] Authentication failed -- check PI_USER/PI_PASSWORD in .env",
            "exit_code": -1,
            "success": False,
            "timed_out": False,
        }
    except paramiko.SSHException as e:
        return {
            "stdout": "",
            "stderr": f"[ERROR] SSH error: {e}",
            "exit_code": -1,
            "success": False,
            "timed_out": False,
        }
    except Exception as e:
        return {
            "stdout": "",
            "stderr": f"[ERROR] Connection failed: {e}",
            "exit_code": -1,
            "success": False,
            "timed_out": False,
        }


def ssh_run_detached(command: str) -> dict:
    """Run a command on the Pi that may run forever. Fire and forget.

    Uses nohup + & to detach the process. Returns immediately.
    The command continues running on the Pi after SSH disconnects.
    """
    # Wrap in nohup, redirect output, background it
    wrapped = f"nohup {command} > /dev/null 2>&1 & echo $!"
    result = ssh_run(wrapped, timeout=10)
    pid = result.get("stdout", "").strip()
    return {
        "success": result["success"],
        "pid": pid,
        "stderr": result.get("stderr", ""),
    }


def sftp_upload(local_path: str, remote_path: str) -> dict:
    """Upload a local file to the Pi via SFTP."""
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
    """Download a file from the Pi via SFTP."""
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
# Constants
# ---------------------------------------------------------------------------

REMOTE_WORK_DIR = "/home/scoobyxd/Documents"


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

def run_test():
    """SSH into Pi and create a simple .md file at ~/Documents/."""
    print("=" * 50)
    print("SSH SKILL -- TEST")
    print("=" * 50)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    remote_path = f"{REMOTE_WORK_DIR}/ssh_test.md"

    md_content = (
        f"# SSH Test\n\n"
        f"hi i ssh'd here\n\n"
        f"**Timestamp**: {ts}\n"
    )

    escaped = md_content.replace("'", "'\\''")
    cmd = (
        f"mkdir -p {REMOTE_WORK_DIR} && "
        f"printf '%s' '{escaped}' > {remote_path} && "
        f"echo 'File written successfully' && "
        f"cat {remote_path}"
    )

    print(f"[1/1] Connecting to Pi and writing {remote_path} ...")
    result = ssh_run(cmd)

    if result["success"]:
        print(f"[OK] File created on Pi!")
        print(f"\n--- Remote file contents ---")
        print(result["stdout"])
    else:
        print(f"[FAIL] SSH failed:")
        print(f"  stderr: {result['stderr']}")
        print(f"  exit_code: {result['exit_code']}")

    return result


# ---------------------------------------------------------------------------
# Deploy & Run
# ---------------------------------------------------------------------------

def deploy_and_run(local_script: str, remote_dir: str = None,
                   timeout: int = 30) -> dict:
    """Upload a script to Pi, run it there. The script saves its own results on Pi."""
    local_path = Path(local_script)
    if not local_path.exists():
        print(f"[ERROR] File not found: {local_path}")
        return {"success": False, "stderr": f"Local file not found: {local_path}"}

    rdir = remote_dir or REMOTE_WORK_DIR
    remote_path = f"{rdir}/{local_path.name}"

    print(f"[1/4] Creating remote directory: {rdir}")
    ssh_run(f"mkdir -p {rdir}")

    print(f"[2/4] Uploading {local_path.name} -> Pi:{remote_path}")
    up = sftp_upload(str(local_path), remote_path)
    if not up["success"]:
        print(f"[FAIL] Upload failed: {up['stderr']}")
        return up

    print(f"[3/4] Executing on Pi: python3 {local_path.name}")
    result = ssh_run(f"cd {rdir} && python3 {local_path.name}", timeout=timeout)
    result["remote_path"] = remote_path

    if result.get("timed_out"):
        print(f"[WARN] Execution timed out after {timeout}s (process may still be running on Pi)")
    elif result["success"]:
        print(f"[OK] Execution succeeded (exit code 0)")
    else:
        print(f"[WARN] Execution finished with exit code {result['exit_code']}")

    if result["stdout"]:
        print(f"\n--- Pi stdout ---")
        print(result["stdout"])
    if result["stderr"]:
        print(f"\n--- Pi stderr ---")
        print(result["stderr"])

    print(f"[4/4] Checking for result files on Pi...")
    verify = ssh_run(f"ls -la {rdir}/*result* 2>/dev/null || echo '(no result files found)'")
    print(verify["stdout"].strip())

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="SSH Skill -- Run commands on Raspberry Pi")
    parser.add_argument("--test", action="store_true",
                        help="Create a test .md file on the Pi")
    parser.add_argument("--run", type=str,
                        help="Run an arbitrary command on the Pi")
    parser.add_argument("--upload", nargs=2, metavar=("LOCAL", "REMOTE"),
                        help="Upload a file")
    parser.add_argument("--download", nargs=2, metavar=("REMOTE", "LOCAL"),
                        help="Download a file")
    parser.add_argument("--deploy", type=str, metavar="SCRIPT",
                        help="Upload + run a .py script on Pi")
    parser.add_argument("--remote-dir", type=str, default=None,
                        help="Remote directory (default: ~/Documents)")
    parser.add_argument("--timeout", type=int, default=30,
                        help="Timeout in seconds (default: 30)")

    args = parser.parse_args()

    if args.test:
        run_test()
    elif args.deploy:
        deploy_and_run(args.deploy, remote_dir=args.remote_dir, timeout=args.timeout)
    elif args.run:
        result = ssh_run(args.run, timeout=args.timeout)
        print(result["stdout"])
        if result["stderr"]:
            print(result["stderr"], file=sys.stderr)
        if result.get("timed_out"):
            print("[WARN] Command timed out", file=sys.stderr)
        sys.exit(result["exit_code"] if result["exit_code"] >= 0 else 1)
    elif args.upload:
        result = sftp_upload(args.upload[0], args.upload[1])
        print(f"[OK] Uploaded" if result["success"] else f"[FAIL] {result['stderr']}")
    elif args.download:
        result = sftp_download(args.download[0], args.download[1])
        print(f"[OK] Downloaded" if result["success"] else f"[FAIL] {result['stderr']}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
