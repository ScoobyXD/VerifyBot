#!/usr/bin/env python3
"""
main.py -- VerifyBot v2: LLM-driven hardware debug loop.

One command does everything:
    python main.py "make a random word generator that saves to a text file"
    python main.py "why is my I2C sensor not responding"
    python main.py "kill the infinite counter script"
    python main.py "write a fizzbuzz" --target local

The loop:
    1. Probe target machine (Pi or local) for system context
    2. Build context-rich prompt, send to ChatGPT via browser
    3. Extract code blocks / bash commands from response
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
import time
from datetime import datetime
from pathlib import Path

# Clean stale bytecode on startup (prevents import issues after file updates)
for _cache in Path(__file__).resolve().parent.rglob("__pycache__"):
    if _cache.is_dir():
        import shutil
        shutil.rmtree(_cache, ignore_errors=True)

from core.session import ChatGPTSession
from skills.chatgpt_skill import save_response, append_to_log
from skills.ssh_skill import ssh_run, ssh_run_detached, sftp_upload, REMOTE_WORK_DIR
from skills.extract_skill import (
    extract_blocks, extract_filename_hint, extract_timeout_hint,
    classify_blocks, CodeBlock,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
PROGRAMS_DIR = ROOT / "programs"
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


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def build_initial_prompt(user_prompt: str, context: str, target: str,
                         remote_dir: str = None) -> str:
    """Build the first prompt with full system context."""
    rdir = remote_dir or REMOTE_WORK_DIR
    target_desc = "Raspberry Pi 5 via SSH" if target == "raspi" else "this local machine"

    return (
        f"You are helping me debug and write code for hardware.\n"
        f"Code will be deployed and executed on: {target_desc}\n\n"
        f"{context}\n\n"
        f"RULES:\n"
        f"- Put all code inside fenced code blocks (```language ... ```).\n"
        f"- Use whatever language, tools, or packages you think are best.\n"
        f"- If you need a package installed, include INSTALL: package1, package2 "
        f"at the top of your response.\n"
        f"- If this will take longer than 30 seconds to run, include "
        f"TIMEOUT: <seconds> at the top of your response.\n"
        f"- ASCII only in code output. No emojis.\n"
        f"- Print clear status messages so I can see what happened.\n\n"
        f"TASK: {user_prompt}\n"
    )


def build_error_feedback(executed: list[dict]) -> str:
    """Build feedback from failed execution results. Just raw output, no tricks."""
    lines = ["The code failed on the target machine. Here are the results:", ""]

    for item in executed:
        name = item.get("name", item.get("cmd", "unknown"))
        lines.append(f"--- {name} ---")
        lines.append(f"Exit code: {item['exit_code']}")
        if item.get("timed_out"):
            lines.append(f"(Timed out after {item.get('timeout', '?')}s)")
        if item.get("stderr"):
            lines.append(f"STDERR:\n{item['stderr'][:3000]}")
        if item.get("stdout"):
            lines.append(f"STDOUT:\n{item['stdout'][:1500]}")
        lines.append("")

    lines.append("Fix the code. Return the complete corrected version.")
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
# Execution
# ---------------------------------------------------------------------------

def run_commands_on_pi(commands: list[str], timeout: int = 15) -> list[dict]:
    """Run bash commands on Pi via SSH."""
    results = []
    for cmd in commands:
        print(f"  [SSH] {cmd}")
        r = ssh_run(cmd, timeout=timeout)
        result = {"cmd": cmd, "name": cmd[:60], "success": r["success"],
                  "exit_code": r["exit_code"], "stdout": r["stdout"],
                  "stderr": r["stderr"], "timed_out": r.get("timed_out", False),
                  "timeout": timeout}
        _print_output(r)
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
        # Compile on Pi, then run
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
    r = ssh_run(run_cmd, timeout=timeout)
    _print_output(r)

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
    compile_r = ssh_run(f"cd {rdir} && {compiler} -Wall -o {binary} {filepath.name}", timeout=30)
    if not compile_r["success"]:
        _print_output(compile_r)
        return {"name": filepath.name, "success": False, "exit_code": compile_r["exit_code"],
                "stdout": compile_r["stdout"], "stderr": compile_r["stderr"], "timed_out": False}

    print(f"  [RUN] Pi: ./{binary}")
    r = ssh_run(f"cd {rdir} && ./{binary}", timeout=timeout)
    _print_output(r)
    return {"name": filepath.name, "success": r["success"], "exit_code": r["exit_code"],
            "stdout": r["stdout"], "stderr": r["stderr"],
            "timed_out": r.get("timed_out", False), "timeout": timeout}


def run_script_local(filepath: Path, timeout: int = 30) -> dict:
    """Run a script locally."""
    ext = filepath.suffix.lower()

    if ext == ".py":
        cmd = [sys.executable, str(filepath)]
    elif ext == ".sh":
        cmd = ["bash", str(filepath)]
    elif ext in (".c", ".cpp"):
        return _compile_and_run_local(filepath, timeout)
    else:
        return {"name": filepath.name, "success": True, "exit_code": 0,
                "stdout": "", "stderr": "", "timed_out": False, "skipped": True}

    print(f"  [RUN] {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              cwd=filepath.parent)
        result = {"name": filepath.name, "success": proc.returncode == 0,
                  "exit_code": proc.returncode, "stdout": proc.stdout,
                  "stderr": proc.stderr, "timed_out": False}
    except subprocess.TimeoutExpired as e:
        result = {"name": filepath.name, "success": False, "exit_code": -1,
                  "stdout": e.stdout or "", "stderr": e.stderr or "",
                  "timed_out": True, "timeout": timeout}
    _print_output_local(result)
    return result


def _compile_and_run_local(filepath: Path, timeout: int) -> dict:
    """Compile and run C/C++ locally."""
    ext = filepath.suffix.lower()
    compiler = "gcc" if ext == ".c" else "g++"
    binary = filepath.with_suffix("" if os.name != "nt" else ".exe")

    print(f"  [COMPILE] {compiler} -Wall -o {binary.name} {filepath.name}")
    comp = subprocess.run([compiler, "-Wall", "-o", str(binary), str(filepath)],
                          capture_output=True, text=True, timeout=30, cwd=filepath.parent)
    if comp.returncode != 0:
        return {"name": filepath.name, "success": False, "exit_code": comp.returncode,
                "stdout": comp.stdout, "stderr": comp.stderr, "timed_out": False}

    print(f"  [RUN] {binary.name}")
    try:
        proc = subprocess.run([str(binary)], capture_output=True, text=True,
                              timeout=timeout, cwd=filepath.parent)
        return {"name": filepath.name, "success": proc.returncode == 0,
                "exit_code": proc.returncode, "stdout": proc.stdout,
                "stderr": proc.stderr, "timed_out": False}
    except subprocess.TimeoutExpired as e:
        return {"name": filepath.name, "success": False, "exit_code": -1,
                "stdout": e.stdout or "", "stderr": e.stderr or "",
                "timed_out": True, "timeout": timeout}


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
                index: int, total: int) -> Path:
    """Save a code block to programs/."""
    PROGRAMS_DIR.mkdir(exist_ok=True)

    # Try to get a filename from the LLM response
    fname = extract_filename_hint(response_text)
    if not fname or Path(fname).suffix != block.extension:
        slug = make_slug(prompt)
        if total == 1:
            fname = f"{slug}{block.extension}"
        else:
            fname = f"{slug}_{index}{block.extension}"

    filepath = PROGRAMS_DIR / fname
    filepath.write_text(block.code, encoding="utf-8")
    print(f"  [SAVED] {filepath.name} ({len(block.code)} chars)")
    return filepath


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------

def run_pipeline(prompt: str, target: str = None, max_retries: int = 3,
                 timeout: int = 30, remote_dir: str = None,
                 headed: bool = True):
    """Main entry point. Prompt -> LLM -> execute -> verify -> retry."""

    # Resolve target
    resolved = target or classify_target(prompt)
    detach = is_long_running(prompt)

    print("=" * 60)
    print(f"VerifyBot v2")
    print(f"  Target:  {resolved}")
    print(f"  Prompt:  {prompt[:80]}{'...' if len(prompt) > 80 else ''}")
    print(f"  Retries: {max_retries}")
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
    md_path = save_response(prompt, "(pipeline started)", attempt=0)

    with ChatGPTSession(headed=headed) as session:
        current_prompt = initial_prompt
        is_followup = False

        for attempt in range(1, max_retries + 2):  # attempt 1 = first try
            print(f"\n{'='*60}")
            print(f"[{'RETRY ' + str(attempt-1) if attempt > 1 else 'ATTEMPT 1'}]")
            print(f"{'='*60}")

            # Log what we're sending to the LLM
            append_to_log(md_path, f"Prompt Sent (Attempt {attempt})",
                          f"```\n{current_prompt}\n```")

            # Step 3: Send to LLM
            print(f"\n[2] Sending to ChatGPT ({len(current_prompt)} chars)...")
            if is_followup:
                response = session.followup(current_prompt)
            else:
                response = session.prompt(current_prompt)
            is_followup = True

            # Log what we got back
            append_to_log(md_path, f"LLM Response (Attempt {attempt})", response)

            # Handle install requests
            handle_installs(response, resolved)

            # Check for timeout hint from LLM
            timeout_hint = extract_timeout_hint(response)
            run_timeout = timeout_hint or timeout
            if timeout_hint:
                print(f"  [TIMEOUT] LLM says this needs {timeout_hint}s (default was {timeout}s)")

            # Step 4: Extract code
            print(f"\n[3] Extracting code from response...")
            blocks = extract_blocks(response)
            if not blocks:
                print("  [WARN] No code blocks found in response.")
                append_to_log(md_path, f"Attempt {attempt}", "No code blocks found.")
                # Ask LLM to give us actual code
                current_prompt = (
                    "I need actual code to execute. Please respond with a "
                    "complete script in a ```python or ```bash code block."
                )
                continue

            scripts, commands = classify_blocks(blocks)
            print(f"  Found: {len(scripts)} script(s), {len(commands)} command(s)")

            if not scripts and not commands:
                print("  [WARN] No executable code found after classification.")
                append_to_log(md_path, f"Attempt {attempt}",
                              "Extracted blocks but none were executable scripts or commands.")
                current_prompt = (
                    "Your response contained code but I couldn't identify any "
                    "executable scripts or commands. Please put your code inside "
                    "a fenced code block like:\n\n"
                    "```python\n# your code here\n```\n\n"
                    "or\n\n```bash\n# your commands here\n```"
                )
                continue

            # Step 5: Execute everything
            print(f"\n[4] Executing on {resolved}...")
            all_results = []

            # Commands first
            if commands:
                if resolved == "raspi":
                    cmd_results = run_commands_on_pi(commands, timeout=15)
                else:
                    # Run bash commands locally
                    cmd_results = []
                    for cmd in commands:
                        print(f"  [LOCAL] {cmd}")
                        try:
                            proc = subprocess.run(cmd, shell=True, capture_output=True,
                                                  text=True, timeout=15)
                            r = {"cmd": cmd, "name": cmd[:60], "success": proc.returncode == 0,
                                 "exit_code": proc.returncode, "stdout": proc.stdout,
                                 "stderr": proc.stderr, "timed_out": False}
                        except subprocess.TimeoutExpired:
                            r = {"cmd": cmd, "name": cmd[:60], "success": False,
                                 "exit_code": -1, "stdout": "", "stderr": "Timed out",
                                 "timed_out": True}
                        _print_output_local(r)
                        cmd_results.append(r)
                all_results.extend(cmd_results)

            # Scripts
            if scripts:
                # Clean programs dir for retries
                if attempt > 1:
                    for old in PROGRAMS_DIR.glob("*"):
                        if old.is_file():
                            old.unlink()

                saved_files = []
                for i, block in enumerate(scripts):
                    fp = save_script(block, prompt, response, i, len(scripts))
                    saved_files.append(fp)

                for fp in saved_files:
                    if resolved == "raspi":
                        result = run_script_on_pi(fp, remote_dir or REMOTE_WORK_DIR,
                                                  timeout=run_timeout, detach=detach)
                    else:
                        result = run_script_local(fp, timeout=run_timeout)
                    all_results.append(result)

            # Step 6: Check results
            executed = [r for r in all_results if not r.get("skipped")]
            if not executed:
                print("\n  [WARN] Nothing was actually executed.")
                current_prompt = "None of the code blocks were executable. Please provide a runnable script."
                continue

            all_ok = all(r["success"] for r in executed)
            if all_ok:
                print(f"\n[DONE] All code executed successfully!")
                # Log success with output
                success_log = "SUCCESS\n\n"
                for r in executed:
                    success_log += f"**{r.get('name', '?')}**: exit code {r['exit_code']}\n"
                    if r.get("stdout"):
                        success_log += f"```\n{r['stdout'][:2000]}\n```\n"
                append_to_log(md_path, f"Attempt {attempt} Result", success_log)
                break

            # Failed -- build feedback
            failures = [r for r in executed if not r["success"]]
            print(f"\n  [FAIL] {len(failures)}/{len(executed)} failed")

            # Log failures
            fail_log = f"FAILED: {len(failures)}/{len(executed)}\n\n"
            for r in failures:
                fail_log += f"**{r.get('name', '?')}**: exit code {r['exit_code']}\n"
                if r.get("stderr"):
                    fail_log += f"stderr:\n```\n{r['stderr'][:2000]}\n```\n"
                if r.get("stdout"):
                    fail_log += f"stdout:\n```\n{r['stdout'][:1000]}\n```\n"
            append_to_log(md_path, f"Attempt {attempt} Execution", fail_log)

            if attempt > max_retries:
                print(f"\n[FAIL] Max retries ({max_retries}) reached.")
                append_to_log(md_path, "Final Result",
                              f"FAILED after {max_retries} retries.")
                break

            print(f"\n[5] Sending errors back to ChatGPT...")
            current_prompt = build_error_feedback(failures)
            append_to_log(md_path, f"Feedback (Attempt {attempt})",
                          f"```\n{current_prompt}\n```")

    print(f"\nLog: {md_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="VerifyBot v2: LLM-driven hardware debug loop",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            '  python main.py "make a random word generator for raspi"\n'
            '  python main.py "write fizzbuzz" --target local\n'
            '  python main.py "why is I2C not working" --max-retries 5\n'
            '  python main.py "blink GPIO 17" --headless\n'
        ),
    )
    parser.add_argument("prompt", help="What you want done (natural language)")
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

    args = parser.parse_args()

    if args.login:
        from skills.chatgpt_skill import run_login_mode
        run_login_mode()
        return

    run_pipeline(
        prompt=args.prompt,
        target=args.target,
        max_retries=args.max_retries,
        timeout=args.timeout,
        remote_dir=args.remote_dir,
        headed=not args.headless,
    )


if __name__ == "__main__":
    main()
