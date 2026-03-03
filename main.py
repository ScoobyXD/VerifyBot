#!/usr/bin/env python3
"""
main.py -- Agent v2: LLM-driven software/hardware loop.

One command does everything:
    python main.py "make a random word generator that saves to a text file"
    python main.py "why is my I2C sensor not responding"
    python main.py "kill the infinite counter script"
    python main.py "write a fizzbuzz" --target local

The loop:
    1. Probe target machine (Pi or local) for system context
    2. Build context-rich prompt, send to ChatGPT via browser
    3. Extract code blocks from response
    4. Deploy and execute on target
    5. If failed: feed raw output back to ChatGPT, retry
    6. Log everything to raw_md/

The LLM is the brain. This tool is just hands on the keyboard.
"""

import argparse
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# Clean stale bytecode on startup (prevents import issues after file updates)
for _cache in Path(__file__).resolve().parent.rglob("__pycache__"):
    if _cache.is_dir():
        import shutil
        shutil.rmtree(_cache, ignore_errors=True)

# --- First-run detection: run setup wizard before importing anything heavy ---
from core.setup import is_first_run
if is_first_run():
    from core.setup import run_setup
    run_setup()
    # After setup, check if user wants to continue or exit
    if len(sys.argv) <= 1 or "--login" in sys.argv:
        sys.exit(0)
    print("\n  Continuing to your prompt...\n")

from core.session import ChatGPTSession
from core.artifact_sweep import snapshot_dirs, sweep_artifacts
from skills.chatgpt_skill import save_response, append_to_log
from skills.ssh_skill import ssh_run, ssh_run_live, ssh_run_detached, sftp_upload, REMOTE_WORK_DIR
from skills.extract_skill import (
    extract_blocks, extract_filename_hint, extract_timeout_hint,
    extract_observe_hint, classify_blocks, CodeBlock,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
PROGRAMS_DIR = ROOT / "programs"
OUTPUTS_DIR = ROOT / "outputs"
CONTEXT_DIR = ROOT / "context"


# ---------------------------------------------------------------------------
# Context probing -- snapshot the target machine
# ---------------------------------------------------------------------------

def probe_pi(remote_dir: str = None) -> str:
    """SSH into Pi and gather live system context. Saved to context/raspi.md."""
    rdir = remote_dir or REMOTE_WORK_DIR
    probes = [
        ("hostname",        "hostname"),
        ("kernel",          "uname -srm"),
        ("architecture",    "uname -m"),
        ("python version",  "python3 --version 2>&1"),
        ("pip packages",    "pip3 list --format=columns 2>/dev/null | head -50"),
        ("disk usage",      "df -h / | tail -1"),
        ("memory",          "free -h | grep Mem"),
        ("working dir",     f"ls -la {rdir}/ 2>/dev/null | tail -25"),
        ("running python",  "pgrep -a python3 2>/dev/null || echo '(none)'"),
        ("serial ports",    "ls /dev/tty{USB,ACM,S}* 2>/dev/null || echo '(none)'"),
        ("i2c devices",     "i2cdetect -y 1 2>/dev/null | head -10 || echo '(not available)'"),
        ("gpio info",       "cat /sys/kernel/debug/gpio 2>/dev/null | head -10 || echo '(not available)'"),
        ("cpu info",        "cat /proc/cpuinfo | grep -E 'Model|model name|Hardware' | head -3"),
        ("os release",      "cat /etc/os-release | head -4"),
    ]

    lines = [f"# Raspberry Pi -- Live Context", f"_Probed: {datetime.now().isoformat()}_", "",
             f"Working directory: `{rdir}`", ""]

    for label, cmd in probes:
        r = ssh_run(cmd, timeout=10)
        output = r["stdout"].strip() if r["success"] else f"(failed: {r['stderr'][:80]})"
        lines.append(f"## {label}")
        lines.append(f"```\n{output}\n```")
        lines.append("")

    content = "\n".join(lines)

    # Save to context/
    CONTEXT_DIR.mkdir(exist_ok=True)
    ctx_path = CONTEXT_DIR / "raspi.md"
    ctx_path.write_text(content, encoding="utf-8")
    print(f"[OK] Pi context saved to {ctx_path}")
    return content


def probe_local() -> str:
    """Gather local machine context."""
    import platform
    lines = [
        f"# Local Machine -- Live Context",
        f"_Probed: {datetime.now().isoformat()}_", "",
        f"- OS: {platform.system()} {platform.release()} ({platform.machine()})",
        f"- Python: {platform.python_version()}",
        f"- CWD: {os.getcwd()}",
    ]

    # Git branch
    git_branch = _get_git_branch()
    if git_branch:
        lines.append(f"- Git branch: {git_branch}")

    # Check for compilers
    for tool in ["gcc", "g++", "arm-none-eabi-gcc"]:
        found = "yes" if _which(tool) else "no"
        lines.append(f"- {tool}: {found}")

    content = "\n".join(lines)

    CONTEXT_DIR.mkdir(exist_ok=True)
    ctx_path = CONTEXT_DIR / "local.md"
    ctx_path.write_text(content, encoding="utf-8")
    return content


def _which(name):
    """Cross-platform which."""
    import shutil
    return shutil.which(name)


def _get_git_branch() -> str:
    """Get current git branch name, or empty string if not a git repo."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def build_initial_prompt(user_prompt: str, context: str, target: str,
                         remote_dir: str = None) -> str:
    """Build the first prompt with structured sections for clarity."""
    rdir = remote_dir or REMOTE_WORK_DIR
    target_desc = "Raspberry Pi 5 via SSH" if target == "raspi" else "this local machine"

    # Build context as a compact single line
    ctx_compact = context.replace("# ", "").replace("_Probed:", "*Probed:").replace("\n- ", " - ")
    ctx_compact = " ".join(ctx_compact.split())

    # Execution environment differs by target
    if target == "raspi":
        exec_env = (
            "- My runner uploads your code to the Pi and executes it via SSH.\n"
            "- Supported languages: Python (.py), Bash (.sh), C/C++ (.c/.cpp, compiled on Pi)."
        )
    else:
        exec_env = (
            "- My runner saves your code to a .py file and executes it with Python.\n"
            "- ALWAYS write Python, even for simple tasks (folders, file ops, git, shell commands).\n"
            "- Use subprocess.run() for system/shell commands. Use os, shutil, pathlib for file ops.\n"
            "- NEVER write bash/shell scripts -- only Python works here."
        )

    return (
        f"=== ROLE ===\n"
        f"You are helping me debug and write code for hardware.\n"
        f"Code will be deployed and executed on: {target_desc}.\n"
        f"\n"
        f"=== SYSTEM CONTEXT ===\n"
        f"{ctx_compact}\n"
        f"\n"
        f"=== EXECUTION ENVIRONMENT ===\n"
        f"{exec_env}\n"
        f"\n"
        f"=== RESPONSE FORMAT ===\n"
        f"- Put ALL code inside exactly ONE fenced code block (```language\\n...code...\\n```).\n"
        f"- Do NOT put any text after the closing ```. No usage instructions, no \"how to run\", no \"save as\".\n"
        f"- If you need multiple files or steps, combine them into ONE script.\n"
        f"- If you need a package installed, include INSTALL: package1, package2 BEFORE the code block.\n"
        f"- If this will take longer than 30s to run, include TIMEOUT: <seconds> BEFORE the code block.\n"
        f"- If this launches long-running processes (dev servers, build tools, background services),\n"
        f"  include OBSERVE: <seconds> BEFORE the code block. My runner will stream the real terminal\n"
        f"  output for that many seconds and send it back to you for verification.\n"
        f"\n"
        f"=== OBSERVE MODE (for dev servers, builds, long-running processes) ===\n"
        f"When you include OBSERVE: N, my runner watches stdout/stderr for N seconds.\n"
        f"CRITICAL RULES for observed scripts:\n"
        f"- Spawn child processes using subprocess.Popen() with NO special flags.\n"
        f"- Do NOT use CREATE_NEW_CONSOLE, DETACHED_PROCESS, or shell=True.\n"
        f"- Do NOT open new terminal windows. All output must flow through stdout/stderr pipes.\n"
        f"- The script must NOT call sys.exit() or exit early -- it should stay alive so child output keeps flowing.\n"
        f"- Use subprocess.Popen(cmd, stdout=None, stderr=None) to let children inherit the parent's pipes.\n"
        f"- If spawning multiple processes, spawn them all and then wait (e.g. while loop with sleep).\n"
        f"- I will capture ALL output that flows through these pipes for N seconds and send it to you.\n"
        f"\n"
        f"=== RULES ===\n"
        f"- SIMPLICITY FIRST: prefer the simplest, most direct solution. Fewer lines = fewer bugs.\n"
        f"- ASCII only in code output. No emojis.\n"
        f"- Print clear status messages so I can see what happened.\n"
        f"\n"
        f"=== TASK ===\n"
        f"{user_prompt}\n"
    )


def build_verification_prompt(executed: list[dict], original_task: str) -> str:
    """Build a prompt that asks the LLM to verify if the output is correct.

    The LLM decides PASS, FAIL, or REVISE — not us.
    """
    lines = [
        f"I ran your code. Here are the results.",
        f"The original task was: {original_task}",
        "",
    ]

    for item in executed:
        name = item.get("name", item.get("cmd", "unknown"))
        lines.append(f"--- {name} ---")
        lines.append(f"Exit code: {item['exit_code']}")
        if item.get("timed_out"):
            lines.append(f"(Timed out after {item.get('timeout', '?')}s)")
        if item.get("observed"):
            lines.append(f"(OBSERVED MODE: watched real terminal output for {item.get('observe_seconds', '?')}s)")
            if item.get("still_running"):
                lines.append(f"(Process is STILL RUNNING after observation window)")
            lines.append("")
            lines.append("=== REAL TERMINAL OUTPUT (stdout) ===")
            lines.append("This is the actual output from the process and all its children,")
            lines.append("captured in real-time during the observation window:")
        if item.get("stdout"):
            lines.append(f"STDOUT:\n{item['stdout'][:5000]}")
        if item.get("stderr"):
            lines.append(f"STDERR:\n{item['stderr'][:3000]}")
        lines.append("")

    lines.append(
        "Based on the output above, does this correctly complete the task?\n"
        "IMPORTANT: Look at the ACTUAL terminal output, not just status messages from the launcher script.\n"
        "Check for: compilation errors, runtime errors, missing files, failed builds, npm errors, etc.\n"
        "Respond with exactly one of:\n"
        "- PASS: if the task is complete and the output shows success\n"
        "- FAIL: if there are errors in the output, and then provide the complete fixed code\n"
        "- REVISE: if it partially works but needs changes, and then provide the complete revised code"
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Target classification
# ---------------------------------------------------------------------------

RASPI_KEYWORDS = [
    r"\braspi\b", r"\braspberry\s*pi\b", r"\bpi\s*5\b", r"\brpi\b",
    r"\bgpio\b", r"\bi2c\b", r"\bspi\b", r"\buart\b", r"\bcan\s*bus\b",
    r"\bsensor\b", r"\bmotor\b", r"\bimu\b", r"\bstm32\b", r"\bembedded\b",
]
LOCAL_KEYWORDS = [
    r"\blocal\b", r"\blocally\b", r"\bmy\s+(computer|laptop)\b", r"\bwindows\b",
]


def classify_target(prompt: str) -> str:
    """Auto-detect target from prompt. Returns 'raspi' or 'local'."""
    p = prompt.lower()
    raspi = sum(1 for pat in RASPI_KEYWORDS if re.search(pat, p))
    local = sum(1 for pat in LOCAL_KEYWORDS if re.search(pat, p))
    if raspi > local:
        return "raspi"
    if local > raspi:
        return "local"
    return "local"  # default


# ---------------------------------------------------------------------------
# Execution -- Raspberry Pi (SSH, supports bash + python + C/C++)
# ---------------------------------------------------------------------------

def run_commands_on_pi(commands: list[str], timeout: int = 15) -> list[dict]:
    """Run bash commands on Pi via SSH with live terminal output."""
    results = []
    for cmd in commands:
        print(f"  [SSH] {cmd}")
        r = ssh_run_live(cmd, timeout=timeout, label="CMD")
        result = {"cmd": cmd, "name": cmd[:60], "success": r["success"],
                  "exit_code": r["exit_code"], "stdout": r["stdout"],
                  "stderr": r["stderr"], "timed_out": r.get("timed_out", False),
                  "timeout": timeout}
        results.append(result)
    return results


def run_script_on_pi(filepath: Path, remote_dir: str, timeout: int = 30,
                     detach: bool = False) -> dict:
    """Upload and execute a script on Pi."""
    rdir = remote_dir or REMOTE_WORK_DIR
    remote_path = f"{rdir}/{filepath.name}"

    print(f"  [UPLOAD] {filepath.name} -> Pi:{remote_path}")
    ssh_run(f"mkdir -p {rdir}")
    up = sftp_upload(str(filepath), remote_path)
    if not up["success"]:
        return {"name": filepath.name, "success": False, "exit_code": -1,
                "stdout": "", "stderr": f"Upload failed: {up['stderr']}",
                "timed_out": False}

    ext = filepath.suffix.lower()
    if ext == ".py":
        executor = "python3"
    elif ext == ".sh":
        executor = "bash"
    elif ext in (".c", ".cpp"):
        return _compile_and_run_on_pi(filepath, rdir, timeout)
    else:
        print(f"  [SKIP] {filepath.name} (uploaded only, not directly runnable)")
        return {"name": filepath.name, "success": True, "exit_code": 0,
                "stdout": "", "stderr": "", "timed_out": False, "skipped": True}

    run_cmd = f"cd {rdir} && {executor} {filepath.name}"

    if detach:
        print(f"  [DETACH] nohup {executor} {filepath.name} &")
        r = ssh_run_detached(run_cmd)
        pid = r.get("pid", "?")
        print(f"  [DETACH] PID {pid}")
        time.sleep(5)
        alive = ssh_run(f"kill -0 {pid} 2>/dev/null && echo ALIVE || echo DEAD", timeout=5)
        status = alive.get("stdout", "").strip()
        print(f"  [CHECK] PID {pid}: {status}")
        return {"name": filepath.name, "success": True, "exit_code": 0,
                "stdout": f"Detached PID {pid}, status={status}", "stderr": "",
                "timed_out": False, "detached": True}

    print(f"  [RUN] Pi: {executor} {filepath.name}")
    r = ssh_run_live(run_cmd, timeout=timeout, label=f"RUN {filepath.name}")

    success = r["success"]
    if r.get("timed_out") and r["stdout"].strip():
        print(f"  [INFO] Had output before timeout -- may be long-running by design")
        success = True

    return {"name": filepath.name, "success": success, "exit_code": r["exit_code"],
            "stdout": r["stdout"], "stderr": r["stderr"],
            "timed_out": r.get("timed_out", False), "timeout": timeout}


def _compile_and_run_on_pi(filepath: Path, rdir: str, timeout: int) -> dict:
    """Compile a C/C++ file on Pi, then run the binary."""
    remote_path = f"{rdir}/{filepath.name}"
    ext = filepath.suffix.lower()
    compiler = "gcc" if ext == ".c" else "g++"
    binary = filepath.stem

    print(f"  [COMPILE] {compiler} -Wall -o {binary} {filepath.name}")
    compile_r = ssh_run_live(f"cd {rdir} && {compiler} -Wall -o {binary} {filepath.name}",
                             timeout=30, label=f"COMPILE {filepath.name}")
    if not compile_r["success"]:
        return {"name": filepath.name, "success": False, "exit_code": compile_r["exit_code"],
                "stdout": compile_r["stdout"], "stderr": compile_r["stderr"], "timed_out": False}

    print(f"  [RUN] Pi: ./{binary}")
    r = ssh_run_live(f"cd {rdir} && ./{binary}", timeout=timeout, label=f"RUN {binary}")
    return {"name": filepath.name, "success": r["success"], "exit_code": r["exit_code"],
            "stdout": r["stdout"], "stderr": r["stderr"],
            "timed_out": r.get("timed_out", False), "timeout": timeout}


# ---------------------------------------------------------------------------
# Execution -- Local (Python only, plus C/C++ compilation)
# ---------------------------------------------------------------------------

def run_script_local(filepath: Path, timeout: int = 30) -> dict:
    """Run a script locally. Shows the full script code and full output."""
    ext = filepath.suffix.lower()

    if ext == ".py":
        cmd = [sys.executable, "-u", str(filepath)]
    elif ext in (".c", ".cpp"):
        return _compile_and_run_local(filepath, timeout)
    else:
        return {"name": filepath.name, "success": False, "exit_code": -1,
                "stdout": "",
                "stderr": (f"[ERROR] Cannot execute {ext} locally. "
                           f"Local target only supports Python (.py) and C/C++. "
                           f"Please rewrite as a Python script using subprocess.run() "
                           f"for any shell commands."),
                "timed_out": False}

    sep = "─" * 60

    # --- Show the script code ---
    print(f"\n  ┌{sep}")
    print(f"  │ SCRIPT: {filepath.name}")
    print(f"  ├{sep}")
    try:
        code_lines = filepath.read_text(encoding="utf-8").split("\n")
        for i, line in enumerate(code_lines, 1):
            print(f"  │ {i:3d} │ {line}")
    except Exception:
        print(f"  │ (could not read file)")
    print(f"  ├{sep}")
    print(f"  │ EXECUTING: {' '.join(cmd)}")
    print(f"  ├{sep}")

    # --- Run it ---
    # Use the project root as cwd so the script's working directory
    # matches the CWD reported in probe_local() context. Scripts are
    # saved to programs/ but the LLM is told CWD is the project root,
    # so file operations should resolve relative to there.
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              cwd=ROOT)
        result = {"name": filepath.name, "success": proc.returncode == 0,
                  "exit_code": proc.returncode, "stdout": proc.stdout,
                  "stderr": proc.stderr, "timed_out": False}
    except subprocess.TimeoutExpired as e:
        result = {"name": filepath.name, "success": False, "exit_code": -1,
                  "stdout": e.stdout or "", "stderr": e.stderr or "",
                  "timed_out": True, "timeout": timeout}

    # --- Show full output ---
    if result.get("stdout"):
        for line in result["stdout"].split("\n"):
            line = line.rstrip("\r\n")
            if line:
                print(f"  │ {line}")
    if result.get("stderr"):
        for line in result["stderr"].split("\n"):
            line = line.rstrip("\r\n")
            if line:
                print(f"  │ \033[91m{line}\033[0m")
    if result.get("timed_out"):
        print(f"  │ \033[93m[TIMED OUT after {timeout}s]\033[0m")
    if not result.get("stdout") and not result.get("stderr"):
        print(f"  │ (no output)")

    ec = result["exit_code"]
    status = "OK" if ec == 0 else f"EXIT {ec}"
    color = "\033[92m" if ec == 0 else "\033[91m"
    print(f"  └{sep} {color}{status}\033[0m")

    return result


def _compile_and_run_local(filepath: Path, timeout: int) -> dict:
    """Compile and run C/C++ locally. Shows code, compilation, and output."""
    ext = filepath.suffix.lower()
    compiler = "gcc" if ext == ".c" else "g++"
    binary = filepath.with_suffix("" if os.name != "nt" else ".exe")
    sep = "─" * 60

    # --- Show the source code ---
    print(f"\n  ┌{sep}")
    print(f"  │ SOURCE: {filepath.name}")
    print(f"  ├{sep}")
    try:
        code_lines = filepath.read_text(encoding="utf-8").split("\n")
        for i, line in enumerate(code_lines, 1):
            print(f"  │ {i:3d} │ {line}")
    except Exception:
        print(f"  │ (could not read file)")

    # --- Compile ---
    print(f"  ├{sep}")
    print(f"  │ COMPILING: {compiler} -Wall -o {binary.name} {filepath.name}")
    print(f"  ├{sep}")

    comp = subprocess.run([compiler, "-Wall", "-o", str(binary), str(filepath)],
                          capture_output=True, text=True, timeout=30, cwd=ROOT)
    if comp.stdout:
        for line in comp.stdout.strip().split("\n"):
            print(f"  │ {line}")
    if comp.stderr:
        for line in comp.stderr.strip().split("\n"):
            print(f"  │ \033[91m{line}\033[0m")

    if comp.returncode != 0:
        print(f"  └{sep} \033[91mCOMPILE FAILED\033[0m")
        return {"name": filepath.name, "success": False, "exit_code": comp.returncode,
                "stdout": comp.stdout, "stderr": comp.stderr, "timed_out": False}

    # --- Run ---
    print(f"  │ Compilation OK")
    print(f"  ├{sep}")
    print(f"  │ EXECUTING: ./{binary.name}")
    print(f"  ├{sep}")

    try:
        proc = subprocess.run([str(binary)], capture_output=True, text=True,
                              timeout=timeout, cwd=ROOT)
        result = {"name": filepath.name, "success": proc.returncode == 0,
                  "exit_code": proc.returncode, "stdout": proc.stdout,
                  "stderr": proc.stderr, "timed_out": False}
    except subprocess.TimeoutExpired as e:
        result = {"name": filepath.name, "success": False, "exit_code": -1,
                  "stdout": e.stdout or "", "stderr": e.stderr or "",
                  "timed_out": True, "timeout": timeout}

    if result.get("stdout"):
        for line in result["stdout"].split("\n"):
            line = line.rstrip("\r\n")
            if line:
                print(f"  │ {line}")
    if result.get("stderr"):
        for line in result["stderr"].split("\n"):
            line = line.rstrip("\r\n")
            if line:
                print(f"  │ \033[91m{line}\033[0m")
    if result.get("timed_out"):
        print(f"  │ \033[93m[TIMED OUT after {timeout}s]\033[0m")

    ec = result["exit_code"]
    status = "OK" if ec == 0 else f"EXIT {ec}"
    color = "\033[92m" if ec == 0 else "\033[91m"
    print(f"  └{sep} {color}{status}\033[0m")

    return result


# ---------------------------------------------------------------------------
# Observed execution (streaming child process output)
# ---------------------------------------------------------------------------

def run_script_local_observed(filepath: Path, timeout: int = 30,
                              observe_seconds: int = 60) -> dict:
    """Run a script locally AND observe its child processes' output over time.

    Unlike run_script_local which uses subprocess.run() (waits for exit,
    captures final output), this function:
      1. Launches the script with Popen and pipes
      2. Streams stdout/stderr in real-time for `observe_seconds`
      3. Accumulates ALL output (parent + child process output visible on pipes)
      4. After the observation window, returns the accumulated output
      5. Leaves the process running if it's still alive (e.g. dev servers)

    The key insight: if the LLM writes a script that spawns child processes
    using subprocess.Popen (with inherited pipes, not CREATE_NEW_CONSOLE),
    we can capture their output through the parent's pipes.

    Returns the same dict format as run_script_local, plus:
      - 'observed': True
      - 'observe_seconds': how long we watched
      - 'still_running': True if process was still alive after observation
    """
    ext = filepath.suffix.lower()

    if ext == ".py":
        cmd = [sys.executable, "-u", str(filepath)]
    else:
        return {"name": filepath.name, "success": False, "exit_code": -1,
                "stdout": "",
                "stderr": f"[ERROR] Observed execution only supports Python (.py).",
                "timed_out": False, "observed": True}

    sep = "─" * 60

    # --- Show the script code ---
    print(f"\n  ┌{sep}")
    print(f"  │ SCRIPT: {filepath.name}")
    print(f"  │ MODE: OBSERVED (watching output for {observe_seconds}s)")
    print(f"  ├{sep}")
    try:
        code_lines = filepath.read_text(encoding="utf-8").split("\n")
        for i, line in enumerate(code_lines, 1):
            print(f"  │ {i:3d} │ {line}")
    except Exception:
        print(f"  │ (could not read file)")
    print(f"  ├{sep}")
    print(f"  │ EXECUTING: {' '.join(cmd)}")
    print(f"  │ OBSERVING: streaming output for up to {observe_seconds}s...")
    print(f"  ├{sep}")

    # --- Launch with Popen and stream output ---
    out_lines = []
    err_lines = []

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # line-buffered
            cwd=ROOT,
        )
    except Exception as e:
        print(f"  │ \033[91m[ERROR] Failed to start: {e}\033[0m")
        print(f"  └{sep} \033[91mLAUNCH FAILED\033[0m")
        return {"name": filepath.name, "success": False, "exit_code": -1,
                "stdout": "", "stderr": str(e), "timed_out": False,
                "observed": True}

    def _read_stream(stream, buf, label):
        """Read lines from a stream in a background thread."""
        try:
            for line in iter(stream.readline, ""):
                if not line:
                    break
                clean = line.rstrip("\r\n")
                buf.append(line)
                if clean:
                    color = "" if label == "out" else "\033[91m"
                    reset = "\033[0m" if color else ""
                    print(f"  │ {color}{clean}{reset}")
        except (ValueError, OSError):
            pass  # stream closed

    # Start reader threads
    t_out = threading.Thread(target=_read_stream, args=(proc.stdout, out_lines, "out"), daemon=True)
    t_err = threading.Thread(target=_read_stream, args=(proc.stderr, err_lines, "err"), daemon=True)
    t_out.start()
    t_err.start()

    # Wait for either the process to exit or the observation window to expire
    deadline = time.time() + observe_seconds
    script_exited = False
    exit_code = None

    while time.time() < deadline:
        rc = proc.poll()
        if rc is not None:
            # Script exited on its own
            script_exited = True
            exit_code = rc
            # Give readers a moment to flush remaining output
            t_out.join(timeout=2)
            t_err.join(timeout=2)
            break
        time.sleep(0.5)

    still_running = not script_exited

    if still_running:
        # Observation window expired, process still running
        # Don't kill it (might be a dev server the user wants)
        # But do try to grab any remaining buffered output
        exit_code = -1
        print(f"  │")
        print(f"  │ \033[93m[OBSERVE] {observe_seconds}s observation window ended, process still running\033[0m")
        print(f"  │ \033[93m[OBSERVE] Leaving process alive (PID {proc.pid})\033[0m")

    stdout_text = "".join(out_lines)
    stderr_text = "".join(err_lines)

    # --- Summary ---
    if not stdout_text and not stderr_text:
        print(f"  │ (no output during observation window)")

    if script_exited:
        status_str = "OK" if exit_code == 0 else f"EXIT {exit_code}"
        color = "\033[92m" if exit_code == 0 else "\033[91m"
    else:
        status_str = f"STILL RUNNING (observed {observe_seconds}s)"
        color = "\033[93m"
    print(f"  └{sep} {color}{status_str}\033[0m")

    return {
        "name": filepath.name,
        "success": (exit_code == 0) if script_exited else False,
        "exit_code": exit_code if exit_code is not None else -1,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "timed_out": False,
        "observed": True,
        "observe_seconds": observe_seconds,
        "still_running": still_running,
        "pid": proc.pid,
    }

def _print_output(r: dict):
    """Print SSH result output."""
    if r.get("stdout"):
        for line in r["stdout"].strip().split("\n")[:15]:
            print(f"    {line}")
    if r.get("stderr"):
        for line in r["stderr"].strip().split("\n")[:8]:
            print(f"    [err] {line}")


def _print_output_local(r: dict):
    """Print local execution output."""
    if r.get("stdout"):
        for line in r["stdout"].strip().split("\n")[:15]:
            print(f"    {line}")
    if r.get("stderr"):
        for line in r["stderr"].strip().split("\n")[:8]:
            print(f"    [err] {line}")


# ---------------------------------------------------------------------------
# Long-running detection
# ---------------------------------------------------------------------------

LONG_PATTERNS = [
    r"\binfinite\b", r"\bforever\b", r"\bdaemon\b", r"\bserver\b",
    r"\bcontinuous\b", r"\bbackground\b", r"\bwhile\s+true\b",
]
KILL_PATTERNS = [r"\bkill\b", r"\bstop\b", r"\bterminate\b", r"\bhalt\b"]


def is_long_running(prompt: str) -> bool:
    p = prompt.lower()
    if any(re.search(pat, p) for pat in KILL_PATTERNS):
        return False
    return any(re.search(pat, p) for pat in LONG_PATTERNS)


# Patterns in script SOURCE CODE that suggest it spawns long-lived children
# and needs observed execution even if the LLM forgot OBSERVE:
_OBSERVE_CODE_PATTERNS = [
    r"subprocess\.Popen",          # spawns child processes
    r"CREATE_NEW_CONSOLE",         # opens separate windows
    r"DETACHED_PROCESS",           # detached subprocess
    r"npm\s+run\b",               # npm dev servers / builds
    r"tauri\s+dev\b",             # tauri dev server
    r"cargo\s+run\b",            # rust dev builds
    r"convex\s+dev\b",           # convex dev server
    r"vite\b",                    # vite dev server
    r"next\s+dev\b",             # next.js dev
    r"webpack\b.*serve\b",       # webpack dev server
    r"flask\s+run\b",            # flask dev server
    r"uvicorn\b",                # python ASGI server
    r"gunicorn\b",               # python WSGI server
    r"docker\s+compose\s+up\b",  # docker compose
]

# Default observation window when auto-detected (seconds)
_AUTO_OBSERVE_SECONDS = 90


def needs_observation(script_code: str) -> bool:
    """Check if a script's source code suggests it spawns long-lived processes
    that need observed execution."""
    for pat in _OBSERVE_CODE_PATTERNS:
        if re.search(pat, script_code, re.IGNORECASE):
            return True
    return False


# ---------------------------------------------------------------------------
# Dependency handling
# ---------------------------------------------------------------------------

def handle_installs(response: str, target: str) -> bool:
    """If LLM says INSTALL: x, y, z — install on target."""
    match = re.search(r"INSTALL:\s*(.+)", response, re.IGNORECASE)
    if not match:
        return False
    packages = [p.strip() for p in match.group(1).split(",") if p.strip()]
    if not packages:
        return False

    for pkg in packages:
        if target == "raspi":
            print(f"  [INSTALL] pip3 install {pkg} on Pi...")
            r = ssh_run(f"pip3 install {pkg} --break-system-packages", timeout=120)
            ok = r["success"]
        else:
            print(f"  [INSTALL] pip install {pkg} locally...")
            proc = subprocess.run([sys.executable, "-m", "pip", "install", pkg],
                                  capture_output=True, text=True, timeout=120)
            ok = proc.returncode == 0
        print(f"  [{'OK' if ok else 'FAIL'}] {pkg}")
    return True


# ---------------------------------------------------------------------------
# File saving
# ---------------------------------------------------------------------------

def make_slug(prompt: str) -> str:
    filler = {"make", "me", "a", "an", "the", "that", "write", "create",
              "generate", "build", "in", "on", "for", "to", "and", "with",
              "of", "please", "can", "you"}
    words = re.findall(r"[a-zA-Z0-9]+", prompt.lower())
    meaningful = [w for w in words if w not in filler and len(w) > 1]
    return "_".join(meaningful[:4]) or "program"


def save_script(block: CodeBlock, prompt: str, response_text: str,
                index: int, total: int, attempt: int) -> Path:
    """Save a code block to programs/ with versioning.

    Files are named like: prime_generator_1.py, prime_generator_2.py
    where the number is the attempt. Never overwrites.
    """
    PROGRAMS_DIR.mkdir(exist_ok=True)

    # Try to get a filename from the LLM response
    fname_hint = extract_filename_hint(response_text)
    if fname_hint and Path(fname_hint).suffix == block.extension:
        base = Path(fname_hint).stem
    else:
        base = make_slug(prompt)

    # Add block index if multiple scripts in one response
    if total > 1:
        base = f"{base}_{index}"

    # Add attempt number for versioning
    fname = f"{base}_{attempt}{block.extension}"
    filepath = PROGRAMS_DIR / fname

    # Strip Windows \r line endings -- scripts from ChatGPT's DOM
    # may carry \r\n, which breaks bash on Linux targets.
    clean_code = block.code.replace("\r\n", "\n").replace("\r", "\n")
    filepath.write_text(clean_code, encoding="utf-8", newline="\n")
    print(f"  [SAVED] {filepath.name} ({len(clean_code)} chars)")
    return filepath


def save_output(name: str, stdout: str, stderr: str, attempt: int) -> Path:
    """Save execution output to outputs/ folder. Never overwrites."""
    OUTPUTS_DIR.mkdir(exist_ok=True)
    base = Path(name).stem if "." in name else name[:40]
    fname = f"{base}_{attempt}.txt"
    filepath = OUTPUTS_DIR / fname

    content = ""
    if stdout:
        content += f"=== STDOUT ===\n{stdout}\n"
    if stderr:
        content += f"\n=== STDERR ===\n{stderr}\n"
    if not content:
        content = "(no output)\n"

    filepath.write_text(content, encoding="utf-8")
    return filepath


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------

def run_pipeline(prompt: str, target: str = None, max_retries: int = 3,
                 timeout: int = 30, remote_dir: str = None,
                 headed: bool = True, profile_dir: Path = None,
                 attachments: list = None, session: 'ChatGPTSession' = None) -> bool:
    """Main entry point. Prompt -> LLM -> execute -> verify -> retry.

    Args:
        prompt: Natural language task description.
        target: 'local', 'raspi', or None for auto-detect.
        max_retries: Max retry attempts.
        timeout: Default execution timeout in seconds.
        remote_dir: Working directory on Pi.
        headed: Show browser window.
        profile_dir: Override browser profile directory.
        attachments: List of local file paths to upload to ChatGPT.
                     Supports images, CSVs, PDFs, etc.
        session: Optional pre-created ChatGPTSession. If provided, the
                 pipeline uses it directly and does NOT close it on exit.
                 The caller is responsible for the session lifecycle.

    Returns True if the LLM verified PASS, False otherwise.
    """

    # Resolve target
    resolved = target or classify_target(prompt)
    detach = is_long_running(prompt)

    print("=" * 60)
    print(f"Agent v2")
    print(f"  Target:  {resolved}")
    print(f"  Prompt:  {prompt[:80]}{'...' if len(prompt) > 80 else ''}")
    print(f"  Retries: {max_retries}")
    if attachments:
        print(f"  Files:   {len(attachments)} attachment(s)")
    if detach:
        print(f"  Mode:    DETACHED (long-running task)")
    print("=" * 60)

    # Step 1: Probe target
    print("\n[1] Probing target machine...")
    if resolved == "raspi":
        context = probe_pi(remote_dir)
    else:
        context = probe_local()

    # Step 2: Build prompt
    initial_prompt = build_initial_prompt(prompt, context, resolved, remote_dir)

    # Prepare logging
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    md_path = save_response(prompt, "(pipeline started)", prompt_num=0)

    # If an external session was provided, use it directly (no context manager).
    # Otherwise create one ourselves and close it when done.
    if session is not None:
        return _run_pipeline_inner(session, initial_prompt, prompt, resolved,
                                   max_retries, timeout, remote_dir, detach,
                                   attachments, md_path)
    else:
        with ChatGPTSession(headed=headed, profile_dir=profile_dir) as sess:
            return _run_pipeline_inner(sess, initial_prompt, prompt, resolved,
                                       max_retries, timeout, remote_dir, detach,
                                       attachments, md_path)


def _run_pipeline_inner(session, initial_prompt, prompt, resolved,
                        max_retries, timeout, remote_dir, detach,
                        attachments, md_path) -> bool:
    """Inner pipeline logic, factored out so it can work with either
    an externally-owned session or a self-managed context-manager session."""
    current_prompt = initial_prompt
    is_followup = False

    # Track previously saved code to detect duplicates
    _saved_code_hashes = set()

    for attempt in range(1, max_retries + 2):  # attempt 1 = first try
        print(f"\n{'='*60}")
        print(f"[{'RETRY ' + str(attempt-1) if attempt > 1 else 'PROMPT 1'}]")
        print(f"{'='*60}")

        # Log what we're sending to the LLM
        append_to_log(md_path, f"Prompt Sent (Prompt {attempt})",
                      f"```\n{current_prompt}\n```")

        # Step 3: Send to LLM
        print(f"\n[2] Sending to ChatGPT ({len(current_prompt)} chars)...")
        if is_followup:
            response = session.followup(current_prompt)
        else:
            # First prompt: attach files if any
            response = session.prompt(current_prompt, files=attachments)
        is_followup = True

        # Log response
        append_to_log(md_path, f"LLM Response (Prompt {attempt})", response)

        # If response was incomplete (timed out), warn and still try to use it
        if hasattr(session, '_last_response_complete') and not session._last_response_complete:
            print("  [WARN] Response may be incomplete (timed out while streaming).")
            append_to_log(md_path, f"Prompt {attempt} Warning",
                          "Response may be incomplete -- ChatGPT was still streaming when timeout hit.")

        # Check if the LLM just said PASS (from a verification followup)
        response_stripped = response.strip()
        if response_stripped.upper().startswith("PASS"):
            print(f"\n[DONE] LLM verified: PASS")
            append_to_log(md_path, f"Prompt {attempt} Result",
                          "LLM VERIFIED: PASS")
            print(f"\nLog: {md_path}")
            return True

        # Handle install requests
        handle_installs(response, resolved)

        # Check for timeout hint from LLM
        timeout_hint = extract_timeout_hint(response)
        run_timeout = timeout_hint or timeout
        if timeout_hint:
            print(f"  [TIMEOUT] LLM says {timeout_hint}s needed")

        # Check for observe hint from LLM (for long-running processes)
        observe_hint = extract_observe_hint(response)
        if observe_hint:
            print(f"  [OBSERVE] LLM says observe output for {observe_hint}s")

        # Step 4: Extract code
        print(f"\n[3] Extracting code from response...")
        blocks = extract_blocks(response)
        if not blocks:
            print("  [WARN] No code blocks found.")
            append_to_log(md_path, f"Prompt {attempt}", "No code blocks found.")
            current_prompt = (
                "Your response contained no executable code. "
                "Please provide a complete script in a fenced code block."
            )
            continue

        scripts, commands = classify_blocks(blocks)
        print(f"  Found: {len(scripts)} script(s), {len(commands)} command(s)")

        # Deduplicate scripts: skip if code identical to a previous prompt
        if scripts:
            unique_scripts = []
            for block in scripts:
                code_hash = hash(block.code.strip())
                if code_hash in _saved_code_hashes:
                    print(f"  [SKIP] Duplicate script detected (same code as previous prompt), skipping.")
                else:
                    unique_scripts.append(block)
                    _saved_code_hashes.add(code_hash)
            scripts = unique_scripts

        if not scripts and not commands:
            print("  [WARN] No executable code after classification.")
            append_to_log(md_path, f"Prompt {attempt}",
                          "Extracted blocks but none executable.")
            current_prompt = (
                "Your response contained code but none were executable "
                "scripts or commands. Please provide a complete script."
            )
            continue

        # Step 5: Execute
        print(f"\n[4] Executing on {resolved}...")
        all_results = []

        # Snapshot filesystem before execution so we can find new artifacts after
        pre_snap = snapshot_dirs()

        # Commands (raspi only -- local uses Python for everything)
        if commands:
            if resolved == "raspi":
                cmd_results = run_commands_on_pi(commands, timeout=15)
                all_results.extend(cmd_results)
            else:
                print(f"  [SKIP] {len(commands)} shell command(s) -- local runs Python only")

        # Scripts — versioned, never overwrite
        if scripts:
            saved_files = []
            for i, block in enumerate(scripts):
                fp = save_script(block, prompt, response, i, len(scripts), attempt)
                saved_files.append((fp, block))

            for fp, block in saved_files:
                # Log the saved script into raw_md
                append_to_log(md_path, f"Saved Script: {fp.name} (Prompt {attempt})",
                              f"```{block.language}\n{block.code}\n```")

                # Determine execution mode
                effective_observe = observe_hint
                if not effective_observe and needs_observation(block.code):
                    effective_observe = _AUTO_OBSERVE_SECONDS
                    print(f"  [AUTO-OBSERVE] Script spawns child processes, "
                          f"auto-enabling observation for {effective_observe}s")

                if resolved == "raspi":
                    result = run_script_on_pi(fp, remote_dir or REMOTE_WORK_DIR,
                                              timeout=run_timeout, detach=detach)
                elif effective_observe:
                    result = run_script_local_observed(
                        fp, timeout=run_timeout, observe_seconds=effective_observe)
                else:
                    result = run_script_local(fp, timeout=run_timeout)
                all_results.append(result)

        # Save outputs — versioned, never overwrite
        executed = [r for r in all_results if not r.get("skipped")]
        for r in executed:
            out_path = save_output(r.get("name", "output"), r.get("stdout", ""),
                                   r.get("stderr", ""), attempt)
            # Log the output file into raw_md
            out_content = ""
            if r.get("stdout"):
                out_content += f"STDOUT:\n```\n{r['stdout'][:3000]}\n```\n"
            if r.get("stderr"):
                out_content += f"STDERR:\n```\n{r['stderr'][:2000]}\n```\n"
            if not out_content:
                out_content = "(no output)\n"
            append_to_log(md_path, f"Output: {out_path.name} (Prompt {attempt})",
                          out_content)

        if not executed:
            print("\n  [WARN] Nothing executed.")
            current_prompt = "None of the code was executable. Please provide a runnable Python script."
            continue

        # Sweep file artifacts created by the script into outputs/
        if resolved == "local":
            sweep_artifacts(pre_snap)

        # Log execution summary
        exec_log = ""
        for r in executed:
            exec_log += f"**{r.get('name', '?')}**: exit code {r['exit_code']}\n"
            if r.get("timed_out"):
                exec_log += f"(Timed out after {r.get('timeout', '?')}s)\n"
        append_to_log(md_path, f"Prompt {attempt} Execution Summary", exec_log)

        # Step 6: Ask the LLM to verify — IT decides pass/fail
        if attempt > max_retries:
            print(f"\n[FAIL] Max retries ({max_retries}) reached.")
            append_to_log(md_path, "Final Result",
                          f"FAILED after {max_retries} retries.")
            print(f"\nLog: {md_path}")
            return False

        print(f"\n[5] Asking ChatGPT to verify output...")
        current_prompt = build_verification_prompt(executed, prompt)

    # Should not reach here, but safety net
    print(f"\nLog: {md_path}")
    return False


def run_followup_pipeline(session, followup_prompt: str, md_path=None,
                          target: str = "local", max_retries: int = 3,
                          timeout: int = 30, remote_dir: str = None,
                          file_paths: list = None) -> bool:
    """Run the extract → execute → verify loop on a follow-up prompt.

    This is for the workbench UI: after the initial pipeline finishes, the
    user can send follow-up prompts in the same ChatGPT session. Each
    follow-up goes through the full pipeline loop (extract code, execute,
    verify) just like the initial prompt, but uses session.followup()
    instead of session.prompt().

    Args:
        session: An already-open ChatGPTSession (same conversation).
        followup_prompt: The user's new prompt text.
        md_path: Path to the raw_md log to append to (or None).
        target: 'local' or 'raspi'.
        max_retries: Max retry attempts for this follow-up.
        timeout: Default execution timeout.
        remote_dir: Working directory on Pi.
        file_paths: Optional file paths to attach.

    Returns True if the LLM verified PASS, False otherwise.
    """
    resolved = target or "local"
    detach = is_long_running(followup_prompt)

    print("=" * 60)
    print(f"Follow-up Pipeline")
    print(f"  Target:  {resolved}")
    print(f"  Prompt:  {followup_prompt[:80]}{'...' if len(followup_prompt) > 80 else ''}")
    print(f"  Retries: {max_retries}")
    print("=" * 60)

    _saved_code_hashes = set()

    # The first prompt in the follow-up is the user's new prompt.
    # Subsequent prompts are verification/retry follow-ups.
    current_prompt = followup_prompt
    is_first = True

    for attempt in range(1, max_retries + 2):
        print(f"\n{'='*60}")
        label = "FOLLOWUP" if is_first else f"RETRY {attempt - 1}"
        print(f"[{label}]")
        print(f"{'='*60}")

        if md_path:
            append_to_log(md_path, f"Follow-up Prompt (Attempt {attempt})",
                          f"```\n{current_prompt}\n```")

        print(f"\n[2] Sending to ChatGPT ({len(current_prompt)} chars)...")
        if is_first and file_paths:
            response = session.followup(current_prompt, files=file_paths)
        else:
            response = session.followup(current_prompt)
        is_first = False

        if md_path:
            append_to_log(md_path, f"Follow-up Response (Attempt {attempt})", response)

        # Check for PASS
        response_stripped = response.strip()
        if response_stripped.upper().startswith("PASS"):
            print(f"\n[DONE] LLM verified: PASS")
            if md_path:
                append_to_log(md_path, f"Follow-up Attempt {attempt} Result",
                              "LLM VERIFIED: PASS")
            return True

        # Handle installs
        handle_installs(response, resolved)

        # Timeout hint
        timeout_hint = extract_timeout_hint(response)
        run_timeout = timeout_hint or timeout
        if timeout_hint:
            print(f"  [TIMEOUT] LLM says {timeout_hint}s needed")

        # Observe hint (for long-running processes)
        observe_hint = extract_observe_hint(response)
        if observe_hint:
            print(f"  [OBSERVE] LLM says observe output for {observe_hint}s")

        # Extract code
        print(f"\n[3] Extracting code from response...")
        blocks = extract_blocks(response)
        if not blocks:
            print("  [WARN] No code blocks found in follow-up response.")
            if md_path:
                append_to_log(md_path, f"Follow-up Attempt {attempt}",
                              "No code blocks found.")
            # If the LLM just gave a text answer with no code, that's fine —
            # it may be answering a question. Don't retry, just return.
            print("  [INFO] LLM responded without code (may be a text answer).")
            return True

        scripts, commands = classify_blocks(blocks)
        print(f"  Found: {len(scripts)} script(s), {len(commands)} command(s)")

        # Deduplicate
        if scripts:
            unique_scripts = []
            for block in scripts:
                code_hash = hash(block.code.strip())
                if code_hash in _saved_code_hashes:
                    print(f"  [SKIP] Duplicate script detected, skipping.")
                else:
                    unique_scripts.append(block)
                    _saved_code_hashes.add(code_hash)
            scripts = unique_scripts

        if not scripts and not commands:
            print("  [WARN] No executable code after classification.")
            current_prompt = (
                "Your response contained code but none were executable "
                "scripts or commands. Please provide a complete script."
            )
            continue

        # Execute
        print(f"\n[4] Executing on {resolved}...")
        all_results = []
        pre_snap = snapshot_dirs()

        if commands:
            if resolved == "raspi":
                cmd_results = run_commands_on_pi(commands, timeout=15)
                all_results.extend(cmd_results)
            else:
                print(f"  [SKIP] {len(commands)} shell command(s) -- local runs Python only")

        if scripts:
            saved_files = []
            for i, block in enumerate(scripts):
                fp = save_script(block, followup_prompt, response, i, len(scripts), attempt)
                saved_files.append((fp, block))

            for fp, block in saved_files:
                if md_path:
                    append_to_log(md_path, f"Saved Script: {fp.name} (Follow-up {attempt})",
                                  f"```{block.language}\n{block.code}\n```")

                # Determine execution mode
                effective_observe = observe_hint
                if not effective_observe and needs_observation(block.code):
                    effective_observe = _AUTO_OBSERVE_SECONDS
                    print(f"  [AUTO-OBSERVE] Script spawns child processes, "
                          f"auto-enabling observation for {effective_observe}s")

                if resolved == "raspi":
                    result = run_script_on_pi(fp, remote_dir or REMOTE_WORK_DIR,
                                              timeout=run_timeout, detach=detach)
                elif effective_observe:
                    result = run_script_local_observed(
                        fp, timeout=run_timeout, observe_seconds=effective_observe)
                else:
                    result = run_script_local(fp, timeout=run_timeout)
                all_results.append(result)

        executed = [r for r in all_results if not r.get("skipped")]
        for r in executed:
            out_path = save_output(r.get("name", "output"), r.get("stdout", ""),
                                   r.get("stderr", ""), attempt)
            if md_path:
                out_content = ""
                if r.get("stdout"):
                    out_content += f"STDOUT:\n```\n{r['stdout'][:3000]}\n```\n"
                if r.get("stderr"):
                    out_content += f"STDERR:\n```\n{r['stderr'][:2000]}\n```\n"
                if not out_content:
                    out_content = "(no output)\n"
                append_to_log(md_path, f"Output: {out_path.name} (Follow-up {attempt})",
                              out_content)

        if not executed:
            print("\n  [WARN] Nothing executed.")
            current_prompt = "None of the code was executable. Please provide a runnable Python script."
            continue

        if resolved == "local":
            sweep_artifacts(pre_snap)

        # Log execution summary
        exec_log = ""
        for r in executed:
            exec_log += f"**{r.get('name', '?')}**: exit code {r['exit_code']}\n"
            if r.get("timed_out"):
                exec_log += f"(Timed out after {r.get('timeout', '?')}s)\n"
        if md_path:
            append_to_log(md_path, f"Follow-up {attempt} Execution Summary", exec_log)

        # Verify
        if attempt > max_retries:
            print(f"\n[FAIL] Max retries ({max_retries}) reached.")
            if md_path:
                append_to_log(md_path, "Follow-up Final Result",
                              f"FAILED after {max_retries} retries.")
            return False

        print(f"\n[5] Asking ChatGPT to verify output...")
        current_prompt = build_verification_prompt(executed, followup_prompt)

    return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def launch_ui():
    """Launch the web UI from core/ui.py."""
    ui_path = ROOT / "core" / "ui.py"
    if not ui_path.exists():
        print("[ERROR] core/ui.py not found.")
        print("  Place ui.py inside the core/ directory.")
        sys.exit(1)
    subprocess.run([sys.executable, str(ui_path)])


def main():
    parser = argparse.ArgumentParser(
        description="Agent v2: LLM-driven software/hardware loop",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python main.py                          # Launch web UI\n"
            '  python main.py "make a random word generator for raspi"\n'
            '  python main.py "write fizzbuzz" --target local\n'
            '  python main.py "analyze this" --attach data.csv\n'
            '  python main.py "what is this image" --attach photo.png\n'
        ),
    )
    parser.add_argument("prompt", nargs="?", default=None,
                        help="What you want done (natural language). "
                             "If omitted, launches the web UI.")
    parser.add_argument("--target", choices=["local", "raspi"], default=None,
                        help="Force target (default: auto-detect from prompt)")
    parser.add_argument("--headless", action="store_true",
                        help="Hide browser (default: visible)")
    parser.add_argument("--max-retries", type=int, default=3,
                        help="Max retry attempts (default: 3)")
    parser.add_argument("--timeout", type=int, default=30,
                        help="Default execution timeout in seconds (default: 30)")
    parser.add_argument("--remote-dir", type=str, default=None,
                        help="Working directory on Pi (default: ~/Documents)")
    parser.add_argument("--login", action="store_true",
                        help="Open browser for manual ChatGPT login")
    parser.add_argument("--attach", type=str, action="append", dest="attachments",
                        metavar="FILE",
                        help="Attach file(s) to send to ChatGPT (images, CSVs, etc.)")
    parser.add_argument("--cli", action="store_true",
                        help="Force CLI mode (skip UI even with no prompt)")

    args = parser.parse_args()

    if args.login:
        from skills.chatgpt_skill import run_login_mode
        run_login_mode()
        return

    # No prompt and not --cli -> launch web UI
    if args.prompt is None and not args.cli:
        launch_ui()
        return

    if args.prompt is None:
        parser.error("prompt is required in CLI mode (--cli)")

    # Resolve attachment paths to absolute
    file_paths = None
    if args.attachments:
        file_paths = []
        for fp in args.attachments:
            p = Path(fp)
            if not p.is_absolute():
                p = Path.cwd() / p
            if p.exists():
                file_paths.append(str(p))
            else:
                print(f"[WARN] Attachment not found: {fp}")

    run_pipeline(
        prompt=args.prompt,
        target=args.target,
        max_retries=args.max_retries,
        timeout=args.timeout,
        remote_dir=args.remote_dir,
        headed=not args.headless,
        attachments=file_paths,
    )


if __name__ == "__main__":
    main()