#!/usr/bin/env python3
"""
ssh_skill.py -- SSH into Raspberry Pi and run commands / transfer files.

Loads credentials from .env (PI_USER, PI_HOST, PI_PASSWORD).
Uses paramiko -- pure Python SSH, works on Windows/Mac/Linux.

Usage:
    # Quick test: create a file on Pi proving SSH works
    python ssh_skill.py --test

    # Deploy a script to Pi, run it remotely, results saved ON the Pi
    python ssh_skill.py --deploy programs/word_generator.py

    # Deploy to a custom remote directory
    python ssh_skill.py --deploy programs/word_generator.py --remote-dir /home/scoobyxd/hw/pi

    # Run an arbitrary command on Pi
    python ssh_skill.py --run "ls -la ~/Documents"

    # Upload a file
    python ssh_skill.py --upload local.txt /home/scoobyxd/Documents/local.txt

    # Download a file
    python ssh_skill.py --download /home/scoobyxd/Documents/remote.txt ./remote.txt

Requires: pip install paramiko
"""

import argparse
import os
import sys
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

ENV_FILE = Path(__file__).parent / ".env"


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
    """Run a command on the Pi via SSH. Returns {stdout, stderr, exit_code, success}."""
    try:
        client = _connect()
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        client.close()
        return {
            "stdout": out,
            "stderr": err,
            "exit_code": exit_code,
            "success": exit_code == 0,
        }
    except paramiko.AuthenticationException:
        return {
            "stdout": "",
            "stderr": "[ERROR] Authentication failed -- check PI_USER/PI_PASSWORD in .env",
            "exit_code": -1,
            "success": False,
        }
    except paramiko.SSHException as e:
        return {
            "stdout": "",
            "stderr": f"[ERROR] SSH error: {e}",
            "exit_code": -1,
            "success": False,
        }
    except Exception as e:
        return {
            "stdout": "",
            "stderr": f"[ERROR] Connection failed: {e}",
            "exit_code": -1,
            "success": False,
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
# Test -- prove SSH works by creating a .md file on Pi
# ---------------------------------------------------------------------------

def run_test():
    """SSH into Pi and create a simple .md file at ~/Documents/."""
    print("=" * 50)
    print("SSH SKILL -- TEST")
    print("=" * 50)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    remote_path = "/home/scoobyxd/Documents/ssh_test.md"

    md_content = (
        f"# SSH Test\n\n"
        f"hi i ssh'd here\n\n"
        f"**Timestamp**: {ts}\n"
    )

    escaped = md_content.replace("'", "'\\''")
    cmd = (
        f"mkdir -p /home/scoobyxd/Documents && "
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
# Deploy & Run -- upload a script, execute it ON the Pi, results stay on Pi
# ---------------------------------------------------------------------------

REMOTE_WORK_DIR = "/home/scoobyxd/Documents"


def deploy_and_run(local_script: str, remote_dir: str = None,
                   timeout: int = 30) -> dict:
    """Upload a script to Pi, run it there. The script saves its own results on Pi.

    Full pipeline:
      1. SFTP upload local_script -> Pi remote_dir/
      2. SSH execute: python3 <script>  (on the Pi)
      3. Print stdout/stderr from remote execution
      4. Verify the result file was created on Pi

    Args:
        local_script: Path to local .py file to deploy
        remote_dir:   Remote directory on Pi (default: ~/Documents)
        timeout:      Execution timeout in seconds

    Returns:
        dict with stdout, stderr, exit_code, success, remote_path
    """
    local_path = Path(local_script)
    if not local_path.exists():
        print(f"[ERROR] File not found: {local_path}")
        return {"success": False, "stderr": f"Local file not found: {local_path}"}

    rdir = remote_dir or REMOTE_WORK_DIR
    remote_path = f"{rdir}/{local_path.name}"

    # Step 1: Ensure remote dir exists
    print(f"[1/4] Creating remote directory: {rdir}")
    ssh_run(f"mkdir -p {rdir}")

    # Step 2: Upload
    print(f"[2/4] Uploading {local_path.name} -> Pi:{remote_path}")
    up = sftp_upload(str(local_path), remote_path)
    if not up["success"]:
        print(f"[FAIL] Upload failed: {up['stderr']}")
        return up

    # Step 3: Execute remotely
    print(f"[3/4] Executing on Pi: python3 {local_path.name}")
    result = ssh_run(f"cd {rdir} && python3 {local_path.name}", timeout=timeout)
    result["remote_path"] = remote_path

    if result["success"]:
        print(f"[OK] Execution succeeded (exit code 0)")
    else:
        print(f"[WARN] Execution finished with exit code {result['exit_code']}")

    if result["stdout"]:
        print(f"\n--- Pi stdout ---")
        print(result["stdout"])
    if result["stderr"]:
        print(f"\n--- Pi stderr ---")
        print(result["stderr"])

    # Step 4: Verify result file exists on Pi
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
                        help="Create a test .md file on the Pi to prove SSH works")
    parser.add_argument("--run", type=str,
                        help="Run an arbitrary command on the Pi")
    parser.add_argument("--upload", nargs=2, metavar=("LOCAL", "REMOTE"),
                        help="Upload a file: --upload local.txt /remote/path.txt")
    parser.add_argument("--download", nargs=2, metavar=("REMOTE", "LOCAL"),
                        help="Download a file: --download /remote/path.txt local.txt")
    parser.add_argument("--deploy", type=str, metavar="SCRIPT",
                        help="Upload a .py script to Pi, run it there, results saved on Pi")
    parser.add_argument("--remote-dir", type=str, default=None,
                        help="Remote directory for --deploy (default: ~/Documents)")
    parser.add_argument("--timeout", type=int, default=30,
                        help="Command timeout in seconds (default: 30)")

    args = parser.parse_args()

    if args.test:
        run_test()
    elif args.deploy:
        result = deploy_and_run(args.deploy, remote_dir=args.remote_dir,
                                timeout=args.timeout)
        if not result["success"]:
            sys.exit(1)
    elif args.run:
        result = ssh_run(args.run, timeout=args.timeout)
        print(result["stdout"])
        if result["stderr"]:
            print(result["stderr"], file=sys.stderr)
        sys.exit(result["exit_code"] if result["exit_code"] >= 0 else 1)
    elif args.upload:
        result = sftp_upload(args.upload[0], args.upload[1])
        if result["success"]:
            print(f"[OK] Uploaded {args.upload[0]} -> {args.upload[1]}")
        else:
            print(f"[FAIL] {result['stderr']}")
    elif args.download:
        result = sftp_download(args.download[0], args.download[1])
        if result["success"]:
            print(f"[OK] Downloaded {args.download[0]} -> {args.download[1]}")
        else:
            print(f"[FAIL] {result['stderr']}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
