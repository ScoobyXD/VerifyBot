#!/usr/bin/env python3
"""
scheduler.py -- Unified pipeline: prompt -> ChatGPT -> extract -> classify -> execute -> TEST

Single entry point that ties everything together:

  1. Generates acceptance tests from the prompt (BEFORE asking ChatGPT)
  2. Captures pre-state snapshot (process list, file list, etc.)
  3. Sends prompt to ChatGPT (via core/session.py)
  4. Saves response to raw_md/ (via skills/chatgpt_skill.py)
  5. Extracts code blocks (via skills/code_skill.py)
  6. Filters out junk blocks (example output, one-liner run commands)
  7. Checks for intent contradictions (code that does opposite of prompt)
  8. Classifies target: local or Raspberry Pi
  9. Routes execution accordingly
  10. Runs acceptance tests (compares pre/post state)
  11. If tests fail: sends SPECIFIC failure details back to ChatGPT for a fix

Usage:
    python scheduler.py "make a random word generator that outputs to a text file in raspi"
    python scheduler.py "make a fizzbuzz program for local"
    python scheduler.py "write a linked list in C" --target local
    python scheduler.py "blink an LED" --target raspi --headless
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

# Our modules
from skills.code_skill import (
    extract_code_blocks,
    extract_all_filename_hints,
    extract_filename_hint,
    save_code_block,
    run_file,
    build_feedback_prompt,
    PROGRAMS_DIR,
)
from skills.chatgpt_skill import ensure_dirs
from core.session import ChatGPTSession
from skills.ssh_skill import ssh_run, ssh_run_detached, sftp_upload, REMOTE_WORK_DIR
from acceptance import (
    generate_acceptance_tests,
    capture_pre_state,
    run_acceptance_tests,
    format_test_failures_for_feedback,
)


# ---------------------------------------------------------------------------
# Long-running task detection
# ---------------------------------------------------------------------------

LONG_RUNNING_PATTERNS = [
    r"\binfinite\b",
    r"\bforever\b",
    r"\bdaemon\b",
    r"\bserver\b",
    r"\bloop\b.*\bmillion\b",
    r"\bloop\b.*\b1[,_]?000[,_]?000\b",
    r"\bcontinuous\b",
    r"\bnon[-\s]?stop\b",
    r"\bbackground\b",
    r"\bkeep\s+running\b",
    r"\bwhile\s+true\b",
    r"\bnever\s+(stop|end|finish)\b",
    r"\bcount.*up\s+to\s+\d{5,}\b",  # count up to 100000+
]

def is_long_running_task(prompt: str) -> bool:
    """Detect if a prompt describes a task that will run for a long time or forever.

    These tasks should be launched detached (nohup + &) so the pipeline
    doesn't hang waiting for them to finish.

    Excludes prompts that are about KILLING/STOPPING long-running tasks.
    """
    prompt_lower = prompt.lower()

    # If the prompt is about killing/stopping something, it's NOT long-running
    kill_patterns = [r"\bkill\b", r"\bstop\b", r"\bterminate\b", r"\bhalt\b",
                     r"\bdelete\b", r"\bremove\b"]
    if any(re.search(p, prompt_lower) for p in kill_patterns):
        return False

    return any(re.search(p, prompt_lower) for p in LONG_RUNNING_PATTERNS)

# Default sampling delay: after launching a long-running task detached,
# wait this many seconds before sampling output for acceptance tests.
LONG_RUNNING_SAMPLE_DELAY = 8


# ---------------------------------------------------------------------------
# Logger -- captures terminal output AND writes to md file simultaneously
# ---------------------------------------------------------------------------

class PipelineLogger:
    """Dual-output logger: prints to terminal AND records everything for the md file."""

    def __init__(self):
        self.sections = []
        self._current_lines = []
        self._current_title = None
        self.md_path = None

    def section(self, title: str):
        self._flush_section()
        self._current_title = title

    def log(self, msg: str = ""):
        print(msg)
        self._current_lines.append(msg)

    def log_quiet(self, msg: str):
        self._current_lines.append(msg)

    def _flush_section(self):
        if self._current_title and self._current_lines:
            self.sections.append((self._current_title, "\n".join(self._current_lines)))
        self._current_lines = []
        self._current_title = None

    def write_md(self, filepath: Path, prompt: str, target: str):
        self._flush_section()
        self.md_path = filepath

        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            f"# Pipeline Run", "",
            f"**Prompt**: {prompt}",
            f"**Target**: {target}",
            f"**Timestamp**: {ts}",
            f"**Platform**: {'Windows' if os.name == 'nt' else 'Linux'}",
            "", "---", "",
        ]

        for title, content in self.sections:
            lines.extend([f"## {title}", "", content, "", "---", ""])

        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Block filtering -- classify into programs, direct commands, and junk
# ---------------------------------------------------------------------------

def classify_block(block, all_blocks: list, prompt: str = "") -> str:
    """Classify a code block as 'program', 'direct_cmd', or 'junk'.

    - program: A real program to save as a file and execute (Python, C, etc.)
    - direct_cmd: A bash one-liner/command to execute directly via SSH
    - junk: Example output, run instructions, illustration text
    """
    code = block.code.strip()
    lang = block.language.lower()
    lines = [l for l in code.split("\n") if l.strip()]
    num_lines = len(lines)

    # Text/yaml blocks are always junk
    if lang in ("txt", "text", "plaintext", "yaml", "yml"):
        return "junk"

    # Multi-line Python/C/etc with logic = always a program
    if lang in ("python", "py", "c", "cpp", "c++", "rust", "java", "javascript", "js"):
        if num_lines >= 3:
            return "program"
        # Short python snippet with logic keywords = program
        has_logic = any(kw in code for kw in [
            "def ", "class ", "import ", "from ", "for ", "while ",
            "if ", "#include", "int main",
        ])
        if has_logic:
            return "program"
        # Very short python (e.g. `import subprocess; subprocess.run(...)`) = program
        if lang in ("python", "py") and ("import " in code or "os." in code):
            return "program"

    # Bash blocks: the interesting case
    if lang in ("bash", "sh", ""):
        joined = " ".join(lines)

        # Junk: process-launching tips (python3 X.py &, nohup, fg/Ctrl+C)
        if num_lines <= 5:
            if re.search(r"python3?\s+\S+\.py\s*&", joined):
                return "junk"
            if re.search(r"nohup\s+python", joined):
                return "junk"
            if re.search(r"\bfg\b", joined) and re.search(r"ctrl", joined, re.IGNORECASE):
                return "junk"
            if re.search(r"kill\s+\$\(cat\s+", joined):
                return "junk"

        # Junk: example output patterns
        if num_lines <= 2 and re.search(r"\b\d+\s+\d+\.\d+\s+", code):
            return "junk"  # Looks like ps aux output

        # Junk: placeholder PIDs (kill 12345, kill -9 <PID>)
        if num_lines <= 2 and re.search(r"kill\s+(-\d+\s+)?(\d{4,5}|<PID>)", code):
            return "junk"

        # Junk: verification commands (ps aux | grep ...)
        if num_lines == 1 and re.search(r"^ps\s+aux\s*\|", code):
            return "junk"

        # Junk: pip install, chmod, sudo reboot
        if num_lines <= 2:
            if re.search(r"^(pip\s+install|chmod\s+|sudo\s+reboot)", joined, re.IGNORECASE):
                return "junk"

        # Direct command: actionable bash commands (pkill, kill with real pattern, etc.)
        # These are commands that DO something useful and should be run directly via SSH
        direct_patterns = [
            r"^pkill\s+(-\w+\s+)*-f\s+\S+",   # pkill -f script_name
            r"^pkill\s+(-\d+\s+)?\w+",          # pkill python3
            r"^killall\s+",                       # killall
            r"^systemctl\s+(stop|restart|start)",  # systemctl commands
            r"^service\s+\w+\s+(stop|restart)",    # service commands
        ]
        for pat in direct_patterns:
            if re.match(pat, joined, re.IGNORECASE):
                return "direct_cmd"

        # Multi-line bash with logic = a real shell script (program)
        if num_lines >= 5:
            return "program"
        if any(kw in code for kw in ["for ", "while ", "if ", "function ", "#!/"]):
            return "program"

        # Short bash with no clear purpose = junk
        if num_lines <= 2:
            return "junk"

    # Anything else short with no logic = junk
    if num_lines <= 2:
        has_logic = any(kw in code for kw in [
            "def ", "class ", "import ", "from ", "for ", "while ",
            "if ", "#include", "int main", "void ", "fn ",
        ])
        if not has_logic:
            return "junk"

    return "program"


def filter_blocks(blocks: list, prompt: str = "") -> tuple:
    """Classify all blocks into (real_programs, direct_commands, junk)."""
    programs, direct_cmds, junk = [], [], []
    for b in blocks:
        classification = classify_block(b, blocks, prompt)
        if classification == "program":
            programs.append(b)
        elif classification == "direct_cmd":
            direct_cmds.append(b)
        else:
            junk.append(b)

    # Safety net: if no programs AND no direct commands, keep largest block
    if not programs and not direct_cmds and blocks:
        largest = max(blocks, key=lambda b: len(b.code))
        programs = [largest]
        junk = [b for b in blocks if b is not largest]

    return programs, direct_cmds, junk


# ---------------------------------------------------------------------------
# Intent-contradiction detection
# ---------------------------------------------------------------------------

DESTRUCTIVE_INTENT_PATTERNS = [
    r"\bkill\b", r"\bstop\b", r"\bterminate\b", r"\bend\b",
    r"\bdelete\b", r"\bremove\b", r"\bclean\s*up\b", r"\bcancel\b",
    r"\bhalt\b", r"\babort\b", r"\bshut\s*down\b",
]

CONSTRUCTIVE_CODE_PATTERNS = [
    (r"python3?\s+\S+\.py\s*&", "launches a python process in background"),
    (r"nohup\s+", "launches a process with nohup"),
    (r"systemctl\s+start\b", "starts a systemd service"),
    (r"systemctl\s+enable\b", "enables a systemd service"),
    (r"echo\s+\$!\s*>\s*\S+\.pid", "writes a PID file (launch pattern)"),
]


def detect_intent_contradiction(prompt: str, block) -> str:
    prompt_lower = prompt.lower()
    has_destructive_intent = any(
        re.search(p, prompt_lower) for p in DESTRUCTIVE_INTENT_PATTERNS
    )
    if not has_destructive_intent:
        return ""
    code = block.code.strip()
    warnings = []
    for pattern, description in CONSTRUCTIVE_CODE_PATTERNS:
        if re.search(pattern, code):
            warnings.append(description)
    if warnings:
        return (
            f"BLOCKED: Code {', '.join(warnings)} but prompt intent is destructive "
            f"(kill/stop/remove). This would do the OPPOSITE of what was requested."
        )
    return ""


# ---------------------------------------------------------------------------
# Smart filenames
# ---------------------------------------------------------------------------

def make_prompt_slug(prompt: str) -> str:
    filler = {"make", "me", "a", "an", "the", "that", "which", "write",
              "create", "generate", "build", "in", "on", "for", "to",
              "and", "with", "of", "please", "can", "you"}
    words = re.findall(r"[a-zA-Z0-9]+", prompt.lower())
    meaningful = [w for w in words if w not in filler and len(w) > 1]
    return "_".join(meaningful[:4]) or "program"


def assign_filename(block, prompt, hints, single_hint, index, total):
    if hints:
        for h in list(hints):
            if Path(h).suffix == block.extension:
                hints.remove(h)
                return h
    if index == 0 and single_hint and Path(single_hint).suffix == block.extension:
        return single_hint
    slug = make_prompt_slug(prompt)
    return f"{slug}{block.extension}" if total == 1 else f"{slug}_{index}{block.extension}"


# ---------------------------------------------------------------------------
# Target classification
# ---------------------------------------------------------------------------

RASPI_PROMPT_PATTERNS = [
    r"\braspi\b", r"\braspberry\s*pi\b", r"\bpi\s*5\b", r"\bpi5\b",
    r"\brpi\b", r"\bremote\b", r"\bgpio\b", r"\bi2c\b", r"\bspi\b",
    r"\buart\b", r"\bcan\s*bus\b", r"\bsensor\b", r"\bmotor\b",
    r"\bimu\b", r"\bstm32\b", r"\bembedded\b", r"\bhardware\b",
]

LOCAL_PROMPT_PATTERNS = [
    r"\blocal\b", r"\blocally\b", r"\bthis\s+machine\b",
    r"\bmy\s+computer\b", r"\bmy\s+laptop\b", r"\bwindows\b", r"\bhere\b",
]

RASPI_CODE_PATTERNS = [
    r"import\s+RPi", r"import\s+gpiozero", r"import\s+spidev",
    r"import\s+smbus", r"import\s+can\b", r"import\s+serial",
    r"socketcan", r"/dev/tty", r"/dev/spi", r"/dev/i2c", r"GPIO\.",
    r"#include\s+\"stm32", r"HAL_GPIO", r"HAL_CAN", r"HAL_SPI", r"HAL_UART",
]


def classify_target(prompt, code_blocks=None, filename_hints=None):
    prompt_lower = prompt.lower()
    local_score = sum(1 for p in LOCAL_PROMPT_PATTERNS if re.search(p, prompt_lower))
    raspi_score = sum(1 for p in RASPI_PROMPT_PATTERNS if re.search(p, prompt_lower))

    if local_score > 0 and raspi_score == 0: return "local"
    if raspi_score > 0 and local_score == 0: return "raspi"
    if raspi_score > local_score: return "raspi"
    if local_score > raspi_score: return "local"

    if code_blocks:
        for block in code_blocks:
            for pattern in RASPI_CODE_PATTERNS:
                if re.search(pattern, block.code):
                    return "raspi"
    return "auto"


def target_display(target):
    return {"raspi": "Raspberry Pi (remote via SSH)",
            "local": "Local machine",
            "auto": "Auto (defaulting to local)"}.get(target, target)


# ---------------------------------------------------------------------------
# Platform context prefix for ChatGPT
# ---------------------------------------------------------------------------

def build_prompt_prefix(target):
    base_rules = (
        "Do NOT use emojis or Unicode symbols anywhere in the code or output -- "
        "use ASCII only. "
        "Prefer stdlib modules over third-party packages. "
        "If third-party packages are required, list them at the top of your response "
        "in this exact format: DEPENDENCIES: package1, package2, package3 "
        "If no external packages are needed, do NOT include a DEPENDENCIES line."
    )
    if target == "raspi":
        return (
            "[SYSTEM CONTEXT: Code will run on a Raspberry Pi 5 running Linux (aarch64). "
            "Python 3 is available. The code will be deployed and executed remotely via SSH. "
            "Any output files should be saved in the same directory as the script. "
            "Use platform.node() or similar to prove execution happened on the Pi. "
            f"{base_rules}]\n\n"
        )
    elif os.name == "nt":
        return (
            "[SYSTEM CONTEXT: Code will run on Windows with Python. "
            "Do NOT use bash/shell scripts -- use Python or PowerShell instead. "
            f"Avoid Linux-only tools. {base_rules}]\n\n"
        )
    else:
        return f"[SYSTEM CONTEXT: Code will run on Linux with Python. {base_rules}]\n\n"


# ---------------------------------------------------------------------------
# Execution runners
# ---------------------------------------------------------------------------

def run_local(saved_files, timeout, log):
    results = []
    for fp in saved_files:
        result = run_file(fp, timeout=timeout)
        results.append(result)
        if result.get("skipped"):
            log.log(f"  [{fp.name}] Skipped")
        elif result.get("success"):
            log.log(f"  [{fp.name}] SUCCESS (exit code 0)")
        else:
            log.log(f"  [{fp.name}] FAILED (exit code {result.get('returncode', '?')})")
        if result.get("stdout"):
            log.log_quiet(f"  stdout: {result['stdout'][:300].strip()}")
        if result.get("stderr"):
            log.log_quiet(f"  stderr: {result['stderr'][:300].strip()}")
    return results


def run_on_raspi(saved_files, remote_dir, timeout, log, long_running=False):
    import time as _time
    rdir = remote_dir or REMOTE_WORK_DIR
    results = []
    ssh_run(f"mkdir -p {rdir}")

    for fp in saved_files:
        remote_path = f"{rdir}/{fp.name}"
        ext = fp.suffix.lower()

        log.log(f"  [UPLOAD] {fp.name} -> Pi:{remote_path}")
        up = sftp_upload(str(fp), remote_path)
        if not up["success"]:
            log.log(f"  [FAIL] Upload failed: {up['stderr']}")
            results.append({"success": False, "error": f"Upload failed: {up['stderr']}",
                            "returncode": -1, "stdout": "", "stderr": up["stderr"]})
            continue

        if ext not in (".py", ".sh"):
            label = "C/header" if ext in (".c", ".h", ".s", ".ld") else ext
            log.log(f"  [UPLOAD ONLY] {fp.name} ({label}, no auto-execution)")
            results.append({"success": True, "skipped": True, "returncode": 0,
                            "stdout": "", "stderr": ""})
            continue

        executor = "python3" if ext == ".py" else "bash"
        run_cmd = f"cd {rdir} && {executor} {fp.name}"

        # --- Long-running tasks: launch detached, sample after delay ---
        if long_running:
            log.log(f"  [DETACH] Pi: nohup {executor} {fp.name} & (long-running task)")
            detach_result = ssh_run_detached(run_cmd)
            pid = detach_result.get("pid", "?")
            log.log(f"  [DETACH] Launched as PID {pid} on Pi")
            log.log(f"  [DETACH] Waiting {LONG_RUNNING_SAMPLE_DELAY}s for output to appear...")

            _time.sleep(LONG_RUNNING_SAMPLE_DELAY)

            # Sample: is the process alive?
            alive_check = ssh_run(
                f"kill -0 {pid} 2>/dev/null && echo 'ALIVE' || echo 'DEAD'", timeout=5
            )
            status = alive_check.get("stdout", "").strip()
            log.log(f"  [SAMPLE] Process PID {pid} after {LONG_RUNNING_SAMPLE_DELAY}s: {status}")

            # Check for output files
            file_check = ssh_run(f"ls -la {rdir}/ 2>/dev/null | tail -20", timeout=5)
            if file_check.get("stdout"):
                log.log(f"  [SAMPLE] Files on Pi:")
                for line in file_check["stdout"].strip().split("\n"):
                    log.log(f"    {line}")

            # Check if there were early crash errors
            stderr_check = ""
            if status == "DEAD":
                # Process died -- might be a crash. Check stderr by reading nohup.out
                nohup_check = ssh_run(f"tail -20 {rdir}/nohup.out 2>/dev/null", timeout=5)
                stderr_check = nohup_check.get("stdout", "")
                if stderr_check.strip():
                    log.log(f"  [SAMPLE] nohup.out (last 20 lines):")
                    for line in stderr_check.strip().split("\n")[:10]:
                        log.log(f"    {line}")

            run_result = {
                "success": status == "ALIVE" or True,  # ALIVE = definitely working
                "returncode": 0,
                "stdout": f"Detached PID {pid}, status={status} after {LONG_RUNNING_SAMPLE_DELAY}s",
                "stderr": stderr_check,
                "target": "raspi", "remote_path": remote_path,
                "timed_out": False,
                "detached": True,
                "detached_pid": pid,
            }
            results.append(run_result)
            continue

        # --- Normal execution (short-running tasks) ---
        log.log(f"  [RUN] Pi: {executor} {fp.name}")
        result = ssh_run(run_cmd, timeout=timeout)

        if result.get("timed_out"):
            log.log(f"  [TIMEOUT] Process still running after {timeout}s")
            log.log(f"  [TIMEOUT] This may be expected -- continuing pipeline")
            run_result = {
                "success": True,
                "returncode": -1,
                "stdout": result.get("stdout", ""),
                "stderr": result.get("stderr", ""),
                "target": "raspi", "remote_path": remote_path,
                "timed_out": True,
            }
        else:
            run_result = {
                "success": result["success"], "returncode": result["exit_code"],
                "stdout": result["stdout"], "stderr": result["stderr"],
                "target": "raspi", "remote_path": remote_path,
                "timed_out": False,
            }
            if result["success"]:
                log.log(f"  [OK] Execution succeeded on Pi")
            else:
                log.log(f"  [FAIL] Exit code: {result['exit_code']}")

        if result.get("stdout"):
            log.log(f"  [Pi STDOUT]")
            for line in result["stdout"].strip().split("\n")[:20]:
                log.log(f"    {line}")
        if result.get("stderr"):
            log.log(f"  [Pi STDERR]")
            for line in result["stderr"].strip().split("\n")[:10]:
                log.log(f"    {line}")
        results.append(run_result)

    log.log(f"  [CHECK] Files on Pi ({rdir}):")
    verify = ssh_run(f"ls -la {rdir}/ 2>/dev/null | tail -20")
    if verify["stdout"]:
        for line in verify["stdout"].strip().split("\n"):
            log.log(f"    {line}")
    return results


def run_direct_commands(commands, log):
    """Execute bash one-liners directly on Pi via SSH (no file upload needed).

    Used for actionable commands like pkill, systemctl stop, etc.
    Returns a list of result dicts.
    """
    results = []
    for cmd_block in commands:
        cmd = cmd_block.code.strip()
        log.log(f"  [SSH-DIRECT] {cmd}")
        result = ssh_run(cmd, timeout=15)
        run_result = {
            "success": result["success"],
            "returncode": result["exit_code"],
            "stdout": result.get("stdout", ""),
            "stderr": result.get("stderr", ""),
            "target": "raspi",
            "direct_cmd": True,
            "timed_out": result.get("timed_out", False),
        }
        if result["success"]:
            log.log(f"  [SSH-DIRECT OK] exit code 0")
        else:
            log.log(f"  [SSH-DIRECT] exit code {result['exit_code']}")
        if result.get("stdout"):
            for line in result["stdout"].strip().split("\n")[:5]:
                log.log(f"    {line}")
        results.append(run_result)
    return results


# ---------------------------------------------------------------------------
# Feedback builder (target-aware)
# ---------------------------------------------------------------------------

def build_feedback(target, code_files, run_results, test_failure_msg=None):
    """Build a feedback prompt for ChatGPT."""
    if test_failure_msg:
        lines = [test_failure_msg, ""]
        lines.extend([
            "IMPORTANT: This code runs on Raspberry Pi 5 (Linux aarch64, Python 3).",
            "Do NOT use emojis or Unicode symbols. ASCII only.",
            "Do NOT use third-party libraries unless absolutely necessary.",
            "Do NOT delete files or take unrelated actions.",
            "Respond with ONLY the complete Python script. No shell commands, no tips.",
            "Please fix the code. Return the complete corrected version.",
        ])
        return "\n".join(lines)

    if target in ("local", "auto"):
        return build_feedback_prompt(code_files, run_results)

    lines = ["The code you just gave me has errors when run on the Raspberry Pi via SSH.",
             "Here are the execution results:", ""]
    for code_file, result in zip(code_files, run_results):
        if result is None or result.get("skipped"):
            continue
        if result.get("success"):
            lines.append(f"{code_file.name}: OK, ran successfully on Pi")
            continue
        lines.append(f"--- {code_file.name} FAILED (on Raspberry Pi) ---")
        if result.get("returncode") is not None:
            lines.append(f"Exit code: {result['returncode']}")
        if result.get("stderr"):
            lines.append(f"STDERR:\n{result['stderr'].strip()[:2000]}")
        if result.get("stdout"):
            lines.append(f"STDOUT:\n{result['stdout'].strip()[:500]}")
        if result.get("error"):
            lines.append(f"Error: {result['error']}")
        lines.append("")
    lines.extend([
        "IMPORTANT: This code runs on Raspberry Pi 5 (Linux aarch64, Python 3).",
        "Do NOT use emojis or Unicode symbols. ASCII only.",
        "Do NOT use third-party libraries unless absolutely necessary.",
        "Respond with ONLY the complete Python script. No shell commands, no tips.",
        "Please fix the code. Return the complete corrected version.",
    ])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The unified pipeline
# ---------------------------------------------------------------------------

def run_pipeline(prompt, target=None, dest=None, run=True, headed=True,
                 verify=True, max_retries=3, timeout=30, remote_dir=None):

    ensure_dirs()
    dest_dir = Path(dest) if dest else PROGRAMS_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)

    log = PipelineLogger()

    # Classify target
    if target and target in ("local", "raspi"):
        resolved_target = target
    else:
        resolved_target = classify_target(prompt)
        if resolved_target == "auto":
            resolved_target = "local"

    # [v0.6] Generate acceptance tests BEFORE asking ChatGPT
    acceptance_tests = generate_acceptance_tests(prompt, resolved_target, remote_dir)
    has_tests = len(acceptance_tests) > 0

    # [v0.7] Detect long-running tasks (infinite loops, daemons, servers)
    long_running = is_long_running_task(prompt)

    # Banner
    log.section("Pipeline Start")
    log.log(f"[TARGET] {target_display(resolved_target)}")
    mode_parts = ["prompt", "extract", "save"]
    if run:
        mode_parts.append("upload to Pi" if resolved_target == "raspi" else "run locally")
        if resolved_target == "raspi":
            mode_parts.append("detach on Pi" if long_running else "run on Pi")
    if verify:
        mode_parts.append("verify loop")
    if has_tests:
        mode_parts.append("acceptance tests")
    log.log("=" * 60)
    log.log(f"PIPELINE: {' -> '.join(mode_parts)}")
    log.log(f"TARGET:   {target_display(resolved_target)}")
    if long_running:
        log.log(f"MODE:     DETACHED (long-running task detected)")
        log.log(f"          Process will be launched with nohup, sampled after {LONG_RUNNING_SAMPLE_DELAY}s")
    if has_tests:
        log.log(f"TESTS:    {len(acceptance_tests)} acceptance test(s) generated")
        for t in acceptance_tests:
            log.log(f"  - {t.name}")
    log.log("=" * 60)

    # [v0.6] Capture pre-state BEFORE anything runs
    pre_snapshots = {}
    if has_tests and run and resolved_target == "raspi":
        log.section("Pre-State Snapshot")
        log.log("[PRE] Capturing system state before code execution...")
        pre_snapshots = capture_pre_state(acceptance_tests, resolved_target, log)

    prompt_prefix = build_prompt_prefix(resolved_target)
    augmented_prompt = prompt_prefix + prompt

    raw_md_dir = Path(__file__).resolve().parent / "raw_md"
    raw_md_dir.mkdir(exist_ok=True)
    ts_slug = datetime.now().strftime("%Y%m%d_%H%M%S")
    md_path = raw_md_dir / f"{ts_slug}_{make_prompt_slug(prompt)}.md"

    run_log = {
        "prompt": prompt, "target": resolved_target,
        "started_at": datetime.now().isoformat(),
        "verify": verify, "max_retries": max_retries,
        "platform": "windows" if os.name == "nt" else "linux",
        "long_running": long_running,
        "acceptance_tests": [t.name for t in acceptance_tests],
        "attempts": [],
    }

    with ChatGPTSession(headed=headed) as session:
        attempt = 0
        current_prompt = augmented_prompt

        while attempt <= max_retries:
            attempt += 1
            is_retry = attempt > 1
            attempt_log = {"attempt": attempt, "is_retry": is_retry,
                           "target": resolved_target,
                           "timestamp": datetime.now().isoformat()}

            # Step 1: Prompt
            if is_retry:
                log.section(f"Retry {attempt - 1}/{max_retries}")
                log.log(f"{'=' * 60}")
                log.log(f"RETRY {attempt - 1}/{max_retries}: Sending fix request...")
                log.log(f"{'=' * 60}")
            else:
                log.section("Step 1: Prompt ChatGPT")

            log.log(f"[1/4] {'Re-prompting' if is_retry else 'Prompting'} ChatGPT...")
            log.log_quiet(f"Prompt ({len(current_prompt)} chars):")
            log.log_quiet(f"```\n{current_prompt}\n```")

            response = session.followup(current_prompt) if is_retry else session.prompt(current_prompt)
            log.log(f"[OK] Got response ({len(response)} chars)")

            log.section(f"ChatGPT Response (Attempt {attempt})")
            log.log_quiet(response)

            # Step 2: Extract
            log.section(f"Step 2: Extract Code (Attempt {attempt})")
            log.log(f"[2/4] Extracting code blocks...")
            all_blocks = extract_code_blocks(response)

            if not all_blocks:
                log.log("[WARN] No code blocks in response.")
                attempt_log["status"] = "no_code_blocks"
                run_log["attempts"].append(attempt_log)
                break

            log.log(f"  Found {len(all_blocks)} raw block(s):")
            for b in all_blocks:
                log.log(f"    [{b.index}] {b.language} -- {len(b.code)} chars")

            real_blocks, direct_cmds, junk_blocks = filter_blocks(all_blocks, prompt)
            if junk_blocks:
                log.log(f"  Filtered out {len(junk_blocks)} junk block(s):")
                for b in junk_blocks:
                    preview = b.code[:60].replace("\n", " ")
                    log.log(f"    [{b.index}] {b.language} -- \"{preview}...\"")
            if direct_cmds:
                log.log(f"  Found {len(direct_cmds)} direct SSH command(s):")
                for b in direct_cmds:
                    log.log(f"    [{b.index}] {b.language} -- \"{b.code.strip()[:60]}\"")
            log.log(f"  Keeping {len(real_blocks)} program block(s), {len(direct_cmds)} direct command(s)")

            # Intent-contradiction check (on programs only)
            contradiction_found = False
            safe_blocks = []
            for b in real_blocks:
                warning = detect_intent_contradiction(prompt, b)
                if warning:
                    log.log(f"  [CONTRADICTION] Block [{b.index}]: {warning}")
                    junk_blocks.append(b)
                    contradiction_found = True
                else:
                    safe_blocks.append(b)
            real_blocks = safe_blocks

            # Also check direct commands for contradictions
            safe_cmds = []
            for b in direct_cmds:
                warning = detect_intent_contradiction(prompt, b)
                if warning:
                    log.log(f"  [CONTRADICTION] Command [{b.index}]: {warning}")
                    junk_blocks.append(b)
                    contradiction_found = True
                else:
                    safe_cmds.append(b)
            direct_cmds = safe_cmds

            if contradiction_found and not real_blocks and not direct_cmds:
                log.log(f"  [WARN] ALL remaining blocks contradicted user intent!")
                current_prompt = (
                    "Your response did not include a working script to accomplish the task. "
                    "Instead, it included shell tips and examples that would do the OPPOSITE "
                    "(e.g., launching the process instead of killing it).\n\n"
                    "I need a COMPLETE, SELF-CONTAINED Python 3 script that I can deploy "
                    "and run on the Raspberry Pi to accomplish this task:\n\n"
                    f"{prompt}\n\n"
                    "Requirements:\n"
                    "- Pure Python 3, stdlib only\n"
                    "- Must actually perform the action, not just print instructions\n"
                    "- ASCII only, no emojis\n"
                    "- Save any logs in the same directory as the script\n"
                    "- Use platform.node() to prove it ran on the Pi"
                )
                attempt_log["status"] = "contradiction_detected"
                attempt_log["files"] = []
                run_log["attempts"].append(attempt_log)
                continue

            if not real_blocks and not direct_cmds:
                log.log("[WARN] No usable code blocks after filtering.")
                attempt_log["status"] = "no_usable_blocks"
                run_log["attempts"].append(attempt_log)
                break

            if target is None:
                code_target = classify_target(prompt, code_blocks=real_blocks)
                if code_target == "raspi" and resolved_target == "local":
                    log.log(f"  [RECLASSIFY] Code content suggests Raspberry Pi")
                    resolved_target = "raspi"

            # Step 3: Save programs (if any)
            saved_files = []
            if real_blocks:
                log.section(f"Step 3: Save to programs/ (Attempt {attempt})")
                log.log(f"[3/4] Saving to {dest_dir}/")
                if is_retry:
                    for old in dest_dir.glob("*"):
                        if old.is_file() and old.suffix in (".py", ".c", ".cpp", ".h", ".sh", ".js", ".txt"):
                            old.unlink()

                hints = extract_all_filename_hints(response)
                single_hint = extract_filename_hint(response)
                for i, b in enumerate(real_blocks):
                    fname = assign_filename(b, prompt, hints, single_hint, i, len(real_blocks))
                    fp = save_code_block(b, dest_dir, filename=fname)
                    saved_files.append(fp)
                    log.log(f"  [SAVED] {fp.name}  ({b.language}, {len(b.code)} chars)")
            attempt_log["files"] = [fp.name for fp in saved_files]
            attempt_log["direct_cmds"] = [b.code.strip()[:80] for b in direct_cmds]

            # Step 4: Execute
            if not run:
                log.log(f"\n[DONE] Pipeline complete (no execution requested).")
                attempt_log["status"] = "no_run"
                run_log["attempts"].append(attempt_log)
                break

            log.section(f"Step 4: Execute (Attempt {attempt})")
            log.log(f"[4/4] Executing on {target_display(resolved_target)}...")

            run_results = []
            any_ran = False
            all_passed = True

            # [v0.7] Execute direct SSH commands first (e.g. pkill -f counter)
            if direct_cmds and resolved_target == "raspi":
                log.log(f"  --- Direct SSH commands ---")
                cmd_results = run_direct_commands(direct_cmds, log)
                run_results.extend(cmd_results)
                for r in cmd_results:
                    any_ran = True
                    # For kill commands, non-zero exit is OK (means "no matching process")
                    # so we don't count it as failure

            # Execute uploaded programs
            if saved_files:
                log.log(f"  --- Program execution ---")
                if resolved_target == "raspi":
                    file_results = run_on_raspi(saved_files, remote_dir, timeout, log,
                                                long_running=long_running)
                else:
                    file_results = run_local(saved_files, timeout, log)
                run_results.extend(file_results)
                for result in file_results:
                    if result.get("skipped"): continue
                    any_ran = True
                    if not result.get("success") and not result.get("timed_out"):
                        all_passed = False

            if not any_ran:
                log.log(f"\n[WARN] No files were actually executed")
                all_passed = False

            # [v0.6] Run acceptance tests
            test_results = []
            tests_passed = True
            test_failure_msg = ""

            if all_passed and any_ran and has_tests and resolved_target == "raspi":
                log.section(f"Step 5: Acceptance Tests (Attempt {attempt})")
                log.log(f"[TESTING] Running {len(acceptance_tests)} acceptance test(s)...")

                test_results = run_acceptance_tests(
                    acceptance_tests, pre_snapshots, resolved_target, log
                )

                tests_passed = all(r["passed"] for r in test_results)
                attempt_log["acceptance_tests"] = [
                    {"name": r["name"], "passed": r["passed"], "reason": r["reason"]}
                    for r in test_results
                ]

                if not tests_passed:
                    test_failure_msg = format_test_failures_for_feedback(test_results)
                    n_fail = sum(1 for r in test_results if not r["passed"])
                    log.log(f"[FAIL] {n_fail}/{len(test_results)} acceptance test(s) failed")
                else:
                    log.log(f"[PASS] All {len(test_results)} acceptance test(s) passed!")

            if all_passed and any_ran and tests_passed:
                log.section("Result")
                log.log(f"[DONE] All code ran successfully on {resolved_target}!")
                if has_tests:
                    log.log(f"[DONE] All {len(acceptance_tests)} acceptance test(s) passed!")
                attempt_log["status"] = "success"
                run_log["attempts"].append(attempt_log)
                break

            # Failure
            if all_passed and any_ran and not tests_passed:
                attempt_log["status"] = "tests_failed"
            else:
                attempt_log["status"] = "failed"

            if not verify or attempt > max_retries:
                run_log["attempts"].append(attempt_log)
                log.section("Result")
                if not verify:
                    log.log(f"[INFO] Code failed. Use --verify to auto-retry.")
                else:
                    log.log(f"[FAIL] Max retries ({max_retries}) reached.")
                break

            log.section(f"Feedback (Attempt {attempt})")
            log.log(f"[FEEDBACK] Building error report for ChatGPT...")

            if test_failure_msg:
                current_prompt = build_feedback(
                    resolved_target, saved_files, run_results,
                    test_failure_msg=test_failure_msg,
                )
            else:
                current_prompt = build_feedback(resolved_target, saved_files, run_results)

            log.log(f"  Feedback: {len(current_prompt)} chars")
            log.log_quiet(f"```\n{current_prompt}\n```")
            attempt_log["feedback_chars"] = len(current_prompt)
            run_log["attempts"].append(attempt_log)

    # Finalize
    run_log["finished_at"] = datetime.now().isoformat()
    run_log["total_attempts"] = attempt
    run_log["final_status"] = (
        run_log["attempts"][-1].get("status", "unknown")
        if run_log["attempts"] else "no_attempts"
    )
    log.section("Run Record")
    log.log_quiet(f"```json\n{json.dumps(run_log, indent=2, default=str)}\n```")
    log.write_md(md_path, prompt, resolved_target)

    print(f"\n{'=' * 60}")
    print(f"Pipeline finished: {run_log['final_status'].upper()}")
    print(f"  Target:   {resolved_target}")
    print(f"  Attempts: {attempt}")
    print(f"  Log:      {md_path}")
    print(f"{'=' * 60}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="VerifyBot: prompt -> ChatGPT -> extract -> classify -> execute -> TEST",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scheduler.py "make a random word generator for raspi"
  python scheduler.py "write a fizzbuzz for local"
  python scheduler.py "write hello world" --no-verify --headless
        """,
    )
    parser.add_argument("prompt", help="Natural language prompt for ChatGPT")
    parser.add_argument("--target", choices=["local", "raspi"], default=None,
                        help="Force execution target (default: auto-detect from prompt)")
    parser.add_argument("--headless", action="store_true",
                        help="Hide browser window (default: visible)")
    parser.add_argument("--no-verify", action="store_true",
                        help="Disable auto-retry on failure (default: verify ON)")
    parser.add_argument("--max-retries", type=int, default=3,
                        help="Max verification retries (default: 3)")
    parser.add_argument("--no-run", action="store_true",
                        help="Skip execution, just extract code")
    parser.add_argument("--dest", type=str, default=None,
                        help="Local directory for extracted code (default: programs/)")
    parser.add_argument("--remote-dir", type=str, default=None,
                        help="Remote directory on Pi (default: ~/Documents)")
    parser.add_argument("--timeout", type=int, default=30,
                        help="Execution timeout per file in seconds (default: 30)")

    args = parser.parse_args()
    run_pipeline(
        prompt=args.prompt, target=args.target, dest=args.dest,
        run=not args.no_run, headed=not args.headless,
        verify=not args.no_verify, max_retries=args.max_retries,
        timeout=args.timeout, remote_dir=args.remote_dir,
    )


if __name__ == "__main__":
    main()
