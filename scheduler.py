#!/usr/bin/env python3
"""
scheduler.py -- Unified pipeline: prompt -> ChatGPT -> extract -> classify -> execute

Single entry point that ties everything together:

  1. Sends prompt to ChatGPT (via core/session.py)
  2. Saves response to raw_md/ (via skills/chatgpt_skill.py)
  3. Extracts code blocks (via skills/code_skill.py)
  4. Filters out junk blocks (example output, one-liner run commands)
  5. Classifies target: local or Raspberry Pi
  6. Routes execution accordingly
  7. Captures output, logs everything
  8. If --verify: sends errors back to ChatGPT for a fix

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
from skills.ssh_skill import ssh_run, sftp_upload, REMOTE_WORK_DIR


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
# Block filtering -- separate real programs from junk
# ---------------------------------------------------------------------------

def is_junk_block(block, all_blocks: list) -> bool:
    """Determine if a code block is junk (example output, run command, etc.)."""
    code = block.code.strip()
    lang = block.language.lower()
    lines = [l for l in code.split("\n") if l.strip()]
    num_lines = len(lines)

    if lang in ("txt", "text", "plaintext", "yaml", "yml"):
        return True

    if lang in ("bash", "sh") and num_lines <= 3:
        joined = " ".join(lines)
        run_patterns = [
            r"^python3?\s+\S+\.py", r"^node\s+\S+\.js", r"^gcc\s+",
            r"^make\b", r"^cd\s+.*&&\s*(python|node|gcc|make|bash|./)",
            r"^pip\s+install", r"^chmod\s+", r"^sudo\s+", r"^\./\w+",
        ]
        for pat in run_patterns:
            if re.match(pat, joined, re.IGNORECASE):
                return True

    if num_lines <= 2 and lang not in ("c", "cpp", "c++"):
        has_logic = any(kw in code for kw in [
            "def ", "class ", "import ", "from ", "for ", "while ",
            "if ", "#include", "int main", "void ", "fn ",
        ])
        if not has_logic:
            return True

    return False


def filter_blocks(blocks: list) -> tuple:
    real, junk = [], []
    for b in blocks:
        (junk if is_junk_block(b, blocks) else real).append(b)
    if not real and blocks:
        largest = max(blocks, key=lambda b: len(b.code))
        real, junk = [largest], [b for b in blocks if b is not largest]
    return real, junk


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


def run_on_raspi(saved_files, remote_dir, timeout, log):
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

        if ext == ".py":
            log.log(f"  [RUN] Pi: python3 {fp.name}")
            result = ssh_run(f"cd {rdir} && python3 {fp.name}", timeout=timeout)
            run_result = {
                "success": result["success"], "returncode": result["exit_code"],
                "stdout": result["stdout"], "stderr": result["stderr"],
                "target": "raspi", "remote_path": remote_path,
            }
            if result["success"]:
                log.log(f"  [OK] Execution succeeded on Pi")
            else:
                log.log(f"  [FAIL] Exit code: {result['exit_code']}")
            if result["stdout"]:
                log.log(f"  [Pi STDOUT]")
                for line in result["stdout"].strip().split("\n")[:20]:
                    log.log(f"    {line}")
            if result["stderr"]:
                log.log(f"  [Pi STDERR]")
                for line in result["stderr"].strip().split("\n")[:10]:
                    log.log(f"    {line}")
            results.append(run_result)
        elif ext == ".sh":
            log.log(f"  [RUN] Pi: bash {fp.name}")
            result = ssh_run(f"cd {rdir} && bash {fp.name}", timeout=timeout)
            results.append({"success": result["success"], "returncode": result["exit_code"],
                            "stdout": result["stdout"], "stderr": result["stderr"],
                            "target": "raspi", "remote_path": remote_path})
        elif ext in (".c", ".h", ".s", ".ld"):
            log.log(f"  [UPLOAD ONLY] {fp.name} (C/header, compile not yet automated)")
            results.append({"success": True, "skipped": True, "returncode": 0,
                            "stdout": "", "stderr": ""})
        else:
            log.log(f"  [UPLOAD ONLY] {fp.name} (no executor for {ext})")
            results.append({"success": True, "skipped": True, "returncode": 0,
                            "stdout": "", "stderr": ""})

    log.log(f"  [CHECK] Files on Pi ({rdir}):")
    verify = ssh_run(f"ls -la {rdir}/ 2>/dev/null | tail -20")
    if verify["stdout"]:
        for line in verify["stdout"].strip().split("\n"):
            log.log(f"    {line}")
    return results


# ---------------------------------------------------------------------------
# Feedback builder (target-aware)
# ---------------------------------------------------------------------------

def build_feedback(target, code_files, run_results):
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

    # Banner
    log.section("Pipeline Start")
    log.log(f"[TARGET] {target_display(resolved_target)}")
    mode_parts = ["prompt", "extract", "save"]
    if run:
        mode_parts.append("upload to Pi" if resolved_target == "raspi" else "run locally")
        if resolved_target == "raspi":
            mode_parts.append("run on Pi")
    if verify:
        mode_parts.append("verify loop")
    log.log("=" * 60)
    log.log(f"PIPELINE: {' -> '.join(mode_parts)}")
    log.log(f"TARGET:   {target_display(resolved_target)}")
    log.log("=" * 60)

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

            real_blocks, junk_blocks = filter_blocks(all_blocks)
            if junk_blocks:
                log.log(f"  Filtered out {len(junk_blocks)} junk block(s):")
                for b in junk_blocks:
                    preview = b.code[:60].replace("\n", " ")
                    log.log(f"    [{b.index}] {b.language} -- \"{preview}...\"")
            log.log(f"  Keeping {len(real_blocks)} real program block(s)")

            if target is None:
                code_target = classify_target(prompt, code_blocks=real_blocks)
                if code_target == "raspi" and resolved_target == "local":
                    log.log(f"  [RECLASSIFY] Code content suggests Raspberry Pi")
                    resolved_target = "raspi"

            # Step 3: Save
            log.section(f"Step 3: Save to programs/ (Attempt {attempt})")
            log.log(f"[3/4] Saving to {dest_dir}/")
            if is_retry:
                for old in dest_dir.glob("*"):
                    if old.is_file() and old.suffix in (".py", ".c", ".cpp", ".h", ".sh", ".js", ".txt"):
                        old.unlink()

            hints = extract_all_filename_hints(response)
            single_hint = extract_filename_hint(response)
            saved_files = []
            for i, b in enumerate(real_blocks):
                fname = assign_filename(b, prompt, hints, single_hint, i, len(real_blocks))
                fp = save_code_block(b, dest_dir, filename=fname)
                saved_files.append(fp)
                log.log(f"  [SAVED] {fp.name}  ({b.language}, {len(b.code)} chars)")
            attempt_log["files"] = [fp.name for fp in saved_files]

            # Step 4: Execute
            if not run:
                log.log(f"\n[DONE] Pipeline complete (no execution requested).")
                attempt_log["status"] = "no_run"
                run_log["attempts"].append(attempt_log)
                break

            log.section(f"Step 4: Execute (Attempt {attempt})")
            log.log(f"[4/4] Executing on {target_display(resolved_target)}...")

            if resolved_target == "raspi":
                run_results = run_on_raspi(saved_files, remote_dir, timeout, log)
            else:
                run_results = run_local(saved_files, timeout, log)

            all_passed = True
            any_ran = False
            for fp, result in zip(saved_files, run_results):
                if result.get("skipped"): continue
                any_ran = True
                if not result.get("success"): all_passed = False
            if not any_ran:
                log.log(f"\n[WARN] No files were actually executed")
                all_passed = False

            if all_passed and any_ran:
                log.section("Result")
                log.log(f"[DONE] All code ran successfully on {resolved_target}!")
                attempt_log["status"] = "success"
                run_log["attempts"].append(attempt_log)
                break

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
        description="VerifyBot: prompt -> ChatGPT -> extract -> classify -> execute",
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
