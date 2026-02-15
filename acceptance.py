#!/usr/bin/env python3
"""
acceptance.py -- Task-specific acceptance tests for the verification pipeline.

The fundamental problem: "exit code 0" and regex-based post-condition checks
cannot determine whether code accomplished its ACTUAL goal. A kill script that
exits cleanly but doesn't kill anything passes exit-code checks. A broad
"are there any python processes?" regex catches unrelated system services.

Solution: Before running any code, capture the system's pre-state. After running,
capture post-state. Compare the DIFF to determine if the intended effect happened.

Each AcceptanceTest has:
    name:           Human-readable description
    pre_commands:   SSH commands to snapshot state BEFORE code runs
    post_commands:  SSH commands to snapshot state AFTER code runs
    evaluate():     Compares pre/post snapshots -> PASS/FAIL with evidence

Tests are generated from the user's prompt BEFORE ChatGPT is ever consulted.
They define the success contract that generated code must satisfy.

Usage (from scheduler.py):
    tests = generate_acceptance_tests(prompt, target)
    pre_snapshots = capture_pre_state(tests, target)
    # ... run code ...
    results = run_acceptance_tests(tests, pre_snapshots, target)
    all_passed = all(r["passed"] for r in results)
"""

import os
import re
from dataclasses import dataclass, field
from typing import List, Callable, Dict, Optional

# Import conditionally so acceptance.py can be tested standalone
try:
    from skills.ssh_skill import ssh_run, REMOTE_WORK_DIR
except ImportError:
    REMOTE_WORK_DIR = "/home/scoobyxd/Documents"
    ssh_run = None


# ---------------------------------------------------------------------------
# AcceptanceTest data structure
# ---------------------------------------------------------------------------

@dataclass
class AcceptanceTest:
    """A single acceptance test with pre/post state comparison."""
    name: str
    pre_commands: List[str]           # Commands to run BEFORE code executes
    post_commands: List[str]          # Commands to run AFTER code executes
    evaluate: Callable                # fn(pre_outputs, post_outputs) -> dict
    intent: str = "generic"           # Category: kill_process, create_file, etc.


# ---------------------------------------------------------------------------
# Test evaluators (the actual pass/fail logic)
# ---------------------------------------------------------------------------

def _eval_kill_process(pre_outputs: List[str], post_outputs: List[str],
                       target_patterns: List[str]) -> dict:
    """Evaluate whether target processes from pre-state are dead in post-state.

    Strategy:
    1. Parse pre-state to find PIDs matching target patterns (these are "our targets")
    2. Parse post-state to see which PIDs are still alive
    3. PASS if all target PIDs from pre-state are gone in post-state
    4. System processes that existed in pre-state but DON'T match patterns are ignored

    This avoids the wayvnc false-negative: wayvnc was in pre-state but doesn't
    match "counter" patterns, so it's excluded from the kill check.
    """
    # Pre-state: extract all python processes
    pre_procs = _parse_process_list(pre_outputs[0] if pre_outputs else "")
    post_procs = _parse_process_list(post_outputs[0] if post_outputs else "")

    # Find which pre-state processes match the target patterns
    target_pids = {}
    non_target_pids = {}
    for pid, info in pre_procs.items():
        cmdline_lower = info["cmdline"].lower()
        matched = any(pat.lower() in cmdline_lower for pat in target_patterns)
        if matched:
            target_pids[pid] = info
        else:
            non_target_pids[pid] = info

    # If no target processes found in pre-state, that's a special case
    if not target_pids:
        # Check if any NEW python processes appeared that match patterns
        # (handles case where process was relaunched)
        new_matching = {}
        for pid, info in post_procs.items():
            if pid not in pre_procs:
                cmdline_lower = info["cmdline"].lower()
                if any(pat.lower() in cmdline_lower for pat in target_patterns):
                    new_matching[pid] = info

        if new_matching:
            return {
                "passed": False,
                "reason": (
                    "No matching processes found in pre-state, but NEW matching "
                    "processes appeared in post-state (code may have launched them). "
                    f"New PIDs: {list(new_matching.keys())}"
                ),
                "evidence": {
                    "pre_targets": {},
                    "post_surviving": {},
                    "new_matching": {str(k): v for k, v in new_matching.items()},
                },
            }

        return {
            "passed": True,
            "reason": "No matching processes found in pre-state (already dead or never existed).",
            "evidence": {
                "pre_targets": {},
                "post_surviving": {},
                "pre_non_targets_ignored": len(non_target_pids),
            },
        }

    # Check which target PIDs survived
    surviving = {}
    for pid in target_pids:
        if pid in post_procs:
            surviving[pid] = post_procs[pid]

    if not surviving:
        return {
            "passed": True,
            "reason": (
                f"All {len(target_pids)} target process(es) killed successfully. "
                f"Ignored {len(non_target_pids)} non-matching system process(es)."
            ),
            "evidence": {
                "pre_targets": {str(k): v for k, v in target_pids.items()},
                "pre_non_targets_ignored": len(non_target_pids),
                "post_surviving": {},
            },
        }

    return {
        "passed": False,
        "reason": (
            f"{len(surviving)}/{len(target_pids)} target process(es) still alive after execution. "
            f"Surviving PIDs: {list(surviving.keys())}. "
            f"Commands: {[v['cmdline'] for v in surviving.values()]}"
        ),
        "evidence": {
            "pre_targets": {str(k): v for k, v in target_pids.items()},
            "post_surviving": {str(k): v for k, v in surviving.items()},
            "pre_non_targets_ignored": len(non_target_pids),
        },
    }


def _eval_file_created(pre_outputs: List[str], post_outputs: List[str],
                       expected_patterns: List[str]) -> dict:
    """Evaluate whether expected files were created."""
    pre_files = set(_parse_file_list(pre_outputs[0] if pre_outputs else ""))
    post_files = set(_parse_file_list(post_outputs[0] if post_outputs else ""))

    new_files = post_files - pre_files

    if not new_files:
        return {
            "passed": False,
            "reason": "No new files were created.",
            "evidence": {"pre_files": sorted(pre_files), "post_files": sorted(post_files)},
        }

    # Check if any new files match expected patterns
    if expected_patterns:
        matching = [f for f in new_files
                    if any(re.search(p, f, re.IGNORECASE) for p in expected_patterns)]
        if not matching:
            return {
                "passed": False,
                "reason": f"New files created ({sorted(new_files)}) but none match expected patterns ({expected_patterns}).",
                "evidence": {"new_files": sorted(new_files), "expected": expected_patterns},
            }

    return {
        "passed": True,
        "reason": f"New file(s) created: {sorted(new_files)}",
        "evidence": {"new_files": sorted(new_files)},
    }


def _eval_file_has_content(pre_outputs: List[str], post_outputs: List[str],
                           content_patterns: List[str]) -> dict:
    """Evaluate whether output file has expected content."""
    post_content = post_outputs[0] if post_outputs else ""

    if not post_content.strip():
        return {
            "passed": False,
            "reason": "Output file is empty or does not exist.",
            "evidence": {"content_preview": ""},
        }

    if content_patterns:
        missing = [p for p in content_patterns
                   if not re.search(p, post_content, re.IGNORECASE)]
        if missing:
            return {
                "passed": False,
                "reason": f"Output file missing expected patterns: {missing}",
                "evidence": {"content_preview": post_content[:500], "missing": missing},
            }

    return {
        "passed": True,
        "reason": "Output file has expected content.",
        "evidence": {"content_preview": post_content[:200]},
    }


def _eval_file_growing(pre_outputs: List[str], post_outputs: List[str],
                       filepath: str) -> dict:
    """Evaluate whether a file has STOPPED growing (for kill-process verification).

    Samples the file size twice with a delay. If size is still changing,
    the process writing to it is still alive.
    """
    # post_outputs[0] = "SIZE1\nSIZE2" from the two samples
    lines = [l.strip() for l in (post_outputs[0] if post_outputs else "").split("\n") if l.strip()]

    if len(lines) < 2:
        return {
            "passed": True,  # File doesn't exist or can't be read = process is dead
            "reason": f"File {filepath} not found or unreadable (process likely dead).",
            "evidence": {"raw": lines},
        }

    try:
        size1 = int(lines[0])
        size2 = int(lines[1])
    except (ValueError, IndexError):
        return {
            "passed": True,
            "reason": f"Could not parse file sizes (file may not exist).",
            "evidence": {"raw": lines},
        }

    if size1 == size2:
        return {
            "passed": True,
            "reason": f"File {filepath} stopped growing (size stable at {size1} bytes). Process is dead.",
            "evidence": {"size1": size1, "size2": size2},
        }

    return {
        "passed": False,
        "reason": f"File {filepath} is STILL GROWING ({size1} -> {size2} bytes). Process is still alive!",
        "evidence": {"size1": size1, "size2": size2, "delta": size2 - size1},
    }


# ---------------------------------------------------------------------------
# Output parsers
# ---------------------------------------------------------------------------

def _parse_process_list(output: str) -> Dict[int, dict]:
    """Parse ps/pgrep output into {pid: {user, cmdline}}."""
    procs = {}
    for line in output.strip().split("\n"):
        line = line.strip()
        if not line or line == "PROCESS_NOT_FOUND":
            continue
        # Try pgrep -a format: "PID CMDLINE"
        match = re.match(r"^(\d+)\s+(.+)$", line)
        if match:
            pid = int(match.group(1))
            cmdline = match.group(2).strip()
            procs[pid] = {"cmdline": cmdline}
            continue
        # Try ps aux format: "USER PID %CPU %MEM VSZ RSS TTY STAT START TIME COMMAND..."
        parts = line.split(None, 10)
        if len(parts) >= 11:
            try:
                pid = int(parts[1])
                cmdline = parts[10]
                procs[pid] = {"user": parts[0], "cmdline": cmdline}
            except (ValueError, IndexError):
                continue
    return procs


def _parse_file_list(output: str) -> List[str]:
    """Parse ls output into list of filenames."""
    files = []
    for line in output.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        # ls -1 format: just filenames
        if not line.startswith("total ") and not line.startswith("d") and not line.startswith("l"):
            files.append(line)
        # ls -la format: parse last column
        elif line.startswith("-"):
            parts = line.split(None, 8)
            if len(parts) >= 9:
                files.append(parts[8])
    return files


# ---------------------------------------------------------------------------
# Test generators (prompt -> tests)
# ---------------------------------------------------------------------------

def _extract_script_name(prompt: str) -> Optional[str]:
    """Try to extract a .py script name from the prompt."""
    match = re.search(r"(\w+(?:_\w+)*\.py)\b", prompt, re.IGNORECASE)
    return match.group(1) if match else None


def _extract_target_keywords(prompt: str) -> List[str]:
    """Extract keywords that identify the target process/file from the prompt."""
    prompt_lower = prompt.lower()
    keywords = []

    # Direct script name mentions
    script = _extract_script_name(prompt)
    if script:
        keywords.append(script)
        # Also add the basename without extension
        stem = script.rsplit(".", 1)[0]
        keywords.append(stem)

    # Common descriptive words that identify the process
    descriptors = [
        (r"\binfinite\s+counter\b", "infinite_counter"),
        (r"\bcounter\b", "counter"),
        (r"\binfinite\b", "infinite"),
        (r"\bloop\b", "loop"),
    ]
    for pattern, keyword in descriptors:
        if re.search(pattern, prompt_lower):
            keywords.append(keyword)

    return keywords if keywords else ["counter", "infinite"]


def generate_tests_kill_process(prompt: str, remote_dir: str = None) -> List[AcceptanceTest]:
    """Generate acceptance tests for 'kill process' tasks.

    Returns TWO complementary tests:
    1. PID-based: Are the target PIDs from pre-state gone?
    2. File-stability: Has the output file stopped growing?

    Both must pass for the task to be considered successful.
    """
    rdir = remote_dir or REMOTE_WORK_DIR
    keywords = _extract_target_keywords(prompt)

    # Build pgrep pattern that matches any of our keywords
    # Using pgrep -fa for full cmdline match, user-owned only
    # We capture ALL python processes so we can diff properly
    process_snapshot_cmd = (
        "pgrep -a python 2>/dev/null || echo 'PROCESS_NOT_FOUND'"
    )

    def eval_kill(pre, post):
        return _eval_kill_process(pre, post, keywords)

    test_pid = AcceptanceTest(
        name=f"Target processes matching {keywords} are killed",
        pre_commands=[process_snapshot_cmd],
        post_commands=[process_snapshot_cmd],
        evaluate=eval_kill,
        intent="kill_process",
    )

    # File stability test: check if counter_log.txt (or similar) stopped growing
    # This catches the case where the process was killed and restarted
    file_stability_cmd = (
        f"stat -c '%s' {rdir}/counter_log.txt 2>/dev/null || echo '0'; "
        f"sleep 3; "
        f"stat -c '%s' {rdir}/counter_log.txt 2>/dev/null || echo '0'"
    )

    def eval_stable(pre, post):
        return _eval_file_growing(pre, post, f"{rdir}/counter_log.txt")

    test_file = AcceptanceTest(
        name="Output file stopped growing (process no longer writing)",
        pre_commands=[],  # No pre-state needed -- we sample twice in post
        post_commands=[file_stability_cmd],
        evaluate=eval_stable,
        intent="kill_process",
    )

    return [test_pid, test_file]


def generate_tests_create_file(prompt: str, remote_dir: str = None) -> List[AcceptanceTest]:
    """Generate acceptance tests for 'create/generate file' tasks."""
    rdir = remote_dir or REMOTE_WORK_DIR
    file_list_cmd = f"ls -la {rdir}/ 2>/dev/null"

    def eval_created(pre, post):
        return _eval_file_created(pre, post, expected_patterns=[r"\.txt$", r"\.py$", r"\.csv$", r"\.md$"])

    return [AcceptanceTest(
        name="New output file(s) created",
        pre_commands=[file_list_cmd],
        post_commands=[file_list_cmd],
        evaluate=eval_created,
        intent="create_file",
    )]


def generate_tests_generic_execution(prompt: str) -> List[AcceptanceTest]:
    """Minimal test: code ran without error. No state comparison needed."""
    # For generic prompts, we don't generate acceptance tests.
    # The pipeline's exit-code check is sufficient.
    return []


# ---------------------------------------------------------------------------
# Main API: generate tests from prompt
# ---------------------------------------------------------------------------

def generate_acceptance_tests(prompt: str, target: str = "raspi",
                              remote_dir: str = None) -> List[AcceptanceTest]:
    """Analyze user prompt and generate appropriate acceptance tests.

    This is called BEFORE ChatGPT is consulted. The tests define the success
    contract that any generated code must satisfy.

    Returns an empty list for prompts where exit-code checking is sufficient.
    """
    prompt_lower = prompt.lower()

    # --- Kill/stop/terminate process ---
    kill_words = [r"\bkill\b", r"\bstop\b", r"\bterminate\b", r"\bhalt\b"]
    process_words = [r"\bprocess\b", r"\bscript\b", r"\bprogram\b", r"\brunning\b",
                     r"\bcounter\b", r"\binfinite\b", r"\brunaway\b"]

    has_kill = any(re.search(p, prompt_lower) for p in kill_words)
    has_process = any(re.search(p, prompt_lower) for p in process_words)

    if has_kill and has_process:
        return generate_tests_kill_process(prompt, remote_dir)

    # --- Delete/remove file ---
    delete_words = [r"\bdelete\b", r"\bremove\b", r"\bwipe\b"]
    has_delete = any(re.search(p, prompt_lower) for p in delete_words)
    if has_delete:
        # Similar to create_file but checking for absence
        return []  # TODO: implement delete file tests

    # --- Create/generate/write file ---
    create_words = [r"\bcreate\b", r"\bgenerate\b", r"\bwrite\b", r"\bmake\b", r"\bbuild\b"]
    file_words = [r"\bfile\b", r"\btxt\b", r"\boutput\b", r"\bsave\b", r"\btext\b"]
    has_create = any(re.search(p, prompt_lower) for p in create_words)
    has_file = any(re.search(p, prompt_lower) for p in file_words)

    if has_create and has_file and target == "raspi":
        return generate_tests_create_file(prompt, remote_dir)

    return []


# ---------------------------------------------------------------------------
# Test execution API (called from scheduler.py)
# ---------------------------------------------------------------------------

def capture_pre_state(tests: List[AcceptanceTest], target: str = "raspi",
                      log=None) -> Dict[str, List[str]]:
    """Run all pre-commands and capture their outputs.

    Returns {test_name: [output1, output2, ...]} for each test.
    Must be called BEFORE the generated code runs.
    """
    if not tests or not ssh_run:
        return {}

    snapshots = {}
    for test in tests:
        if not test.pre_commands:
            snapshots[test.name] = []
            continue

        outputs = []
        for cmd in test.pre_commands:
            if log:
                log.log(f"  [PRE-STATE] {test.name}: {cmd}")
            result = ssh_run(cmd, timeout=15)
            output = result.get("stdout", "").strip()
            if log:
                # Show first few lines
                preview_lines = output.split("\n")[:5]
                for line in preview_lines:
                    log.log(f"    {line}")
                if len(output.split("\n")) > 5:
                    log.log(f"    ... ({len(output.split(chr(10)))} lines total)")
            outputs.append(output)
        snapshots[test.name] = outputs

    return snapshots


def run_acceptance_tests(tests: List[AcceptanceTest],
                         pre_snapshots: Dict[str, List[str]],
                         target: str = "raspi",
                         log=None) -> List[dict]:
    """Run all post-commands and evaluate against pre-state.

    Returns a list of {name, passed, reason, evidence} dicts.
    Must be called AFTER the generated code runs.
    """
    if not tests or not ssh_run:
        return []

    results = []
    for test in tests:
        if log:
            log.log(f"  [TEST] {test.name}")

        # Capture post-state
        post_outputs = []
        for cmd in test.post_commands:
            if log:
                log.log(f"  [POST-CMD] {cmd}")
            result = ssh_run(cmd, timeout=15)
            output = result.get("stdout", "").strip()
            if log:
                preview_lines = output.split("\n")[:5]
                for line in preview_lines:
                    log.log(f"    {line}")
            post_outputs.append(output)

        # Evaluate
        pre = pre_snapshots.get(test.name, [])
        eval_result = test.evaluate(pre, post_outputs)
        eval_result["name"] = test.name

        if log:
            status = "PASS" if eval_result["passed"] else "FAIL"
            log.log(f"  [{status}] {test.name}: {eval_result['reason']}")

        results.append(eval_result)

    return results


def format_test_failures_for_feedback(test_results: List[dict]) -> str:
    """Format failed test results into actionable feedback for ChatGPT.

    Instead of generic "post-condition failed", gives specific details about
    what was expected vs what actually happened.
    """
    failures = [r for r in test_results if not r["passed"]]
    if not failures:
        return ""

    lines = [
        "The code ran without errors (exit code 0), but FAILED the acceptance tests.",
        f"{len(failures)} of {len(test_results)} test(s) failed:",
        "",
    ]

    for f in failures:
        lines.append(f"FAILED TEST: {f['name']}")
        lines.append(f"  Reason: {f['reason']}")
        evidence = f.get("evidence", {})
        if evidence:
            for k, v in evidence.items():
                if isinstance(v, (list, dict)):
                    lines.append(f"  {k}: {v}")
                else:
                    lines.append(f"  {k}: {v}")
        lines.append("")

    lines.extend([
        "IMPORTANT: The above tests define success. Your code must make ALL tests pass.",
        "Do NOT delete files or take unrelated actions -- solve the actual problem.",
        "Provide a COMPLETE, SELF-CONTAINED Python 3 script (stdlib only, ASCII only).",
    ])

    return "\n".join(lines)
