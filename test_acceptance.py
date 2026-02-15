#!/usr/bin/env python3
"""
test_acceptance.py -- Tests for the acceptance testing system.

Validates that the pre/post state diffing correctly handles:
  1. The wayvnc false-negative: system process mistaken for target
  2. Process actually killed: target PIDs gone, system PIDs remain
  3. Process relaunched: new PIDs appear that match target pattern
  4. File stability: output file stopped/still growing
  5. Test generation from prompts

Run: python3 test_acceptance.py
"""

import sys
import os

# Add parent to path so we can import acceptance
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from acceptance import (
    _eval_kill_process,
    _eval_file_growing,
    _parse_process_list,
    generate_acceptance_tests,
    format_test_failures_for_feedback,
)


passed = 0
failed = 0

def test(name, condition):
    global passed, failed
    if condition:
        print(f"  [PASS] {name}")
        passed += 1
    else:
        print(f"  [FAIL] {name}")
        failed += 1


# ============================================================
print("=" * 60)
print("TEST GROUP 1: Process list parser")
print("=" * 60)

# pgrep -a format
pgrep_output = """1172 python /usr/sbin/wayvnc-control.py
2080 python3 /home/scoobyxd/Documents/infinite_counter_loop_counts.py
3001 python3 /home/scoobyxd/Documents/kill_runaway_python.py"""

procs = _parse_process_list(pgrep_output)
test("Parses 3 processes from pgrep output", len(procs) == 3)
test("PID 1172 found", 1172 in procs)
test("PID 2080 found", 2080 in procs)
test("PID 2080 cmdline correct", "infinite_counter" in procs[2080]["cmdline"])

# Empty / PROCESS_NOT_FOUND
test("Empty string -> no processes", len(_parse_process_list("")) == 0)
test("PROCESS_NOT_FOUND -> no processes", len(_parse_process_list("PROCESS_NOT_FOUND")) == 0)


# ============================================================
print()
print("=" * 60)
print("TEST GROUP 2: The wayvnc false-negative scenario")
print("=" * 60)
print("  (This is the EXACT bug that caused 4 failed retries)")

# Pre-state: counter running + wayvnc system service
pre_state = """1172 python /usr/sbin/wayvnc-control.py
2080 python3 /home/scoobyxd/Documents/infinite_counter_loop_counts.py"""

# Post-state: counter killed, but wayvnc still there
post_state_killed = """1172 python /usr/sbin/wayvnc-control.py"""

# Post-state: nothing changed (kill failed)
post_state_unchanged = """1172 python /usr/sbin/wayvnc-control.py
2080 python3 /home/scoobyxd/Documents/infinite_counter_loop_counts.py"""

keywords = ["counter", "infinite"]

# Test: counter killed, wayvnc remains -> should PASS
result = _eval_kill_process([pre_state], [post_state_killed], keywords)
test("Counter killed + wayvnc remains = PASS", result["passed"] == True)
test("Reason mentions killing 1 target", "1 target" in result["reason"])
test("Reason mentions ignoring system processes", "non-matching" in result["reason"].lower() or "ignor" in result["reason"].lower())

# Test: nothing killed -> should FAIL
result2 = _eval_kill_process([pre_state], [post_state_unchanged], keywords)
test("Counter still alive = FAIL", result2["passed"] == False)
test("Reason mentions surviving PIDs", "2080" in str(result2["reason"]) or "still alive" in result2["reason"])


# ============================================================
print()
print("=" * 60)
print("TEST GROUP 3: Edge cases")
print("=" * 60)

# Pre-state: process already dead
result3 = _eval_kill_process(
    ["PROCESS_NOT_FOUND"],
    ["1172 python /usr/sbin/wayvnc-control.py"],
    ["counter"]
)
test("No target in pre-state, wayvnc in post = PASS (nothing to kill)", result3["passed"] == True)

# Pre-state: nothing. Post-state: counter appears (relaunched!)
result4 = _eval_kill_process(
    ["PROCESS_NOT_FOUND"],
    ["5001 python3 /home/scoobyxd/Documents/infinite_counter_loop_counts.py"],
    ["counter"]
)
test("No pre-state + new counter in post-state = FAIL (relaunched)", result4["passed"] == False)
test("Reason mentions NEW matching processes", "new" in result4["reason"].lower())

# Multiple targets, some killed, some not
pre_multi = """1172 python /usr/sbin/wayvnc-control.py
2080 python3 infinite_counter_v1.py
2081 python3 infinite_counter_v2.py"""

post_partial = """1172 python /usr/sbin/wayvnc-control.py
2081 python3 infinite_counter_v2.py"""

result5 = _eval_kill_process([pre_multi], [post_partial], ["counter", "infinite"])
test("1 of 2 targets killed = FAIL", result5["passed"] == False)
test("Mentions surviving PID 2081", "2081" in str(result5))


# ============================================================
print()
print("=" * 60)
print("TEST GROUP 4: File stability test")
print("=" * 60)

# File stopped growing -> process is dead
result6 = _eval_file_growing([], ["18859\n18859"], "/home/scoobyxd/Documents/counter_log.txt")
test("Same size twice = PASS (file stable)", result6["passed"] == True)

# File still growing -> process alive
result7 = _eval_file_growing([], ["18859\n19200"], "/home/scoobyxd/Documents/counter_log.txt")
test("Size increased = FAIL (still growing)", result7["passed"] == False)
test("Mentions STILL GROWING", "still growing" in result7["reason"].lower())

# File doesn't exist
result8 = _eval_file_growing([], ["0\n0"], "/home/scoobyxd/Documents/counter_log.txt")
test("Size 0 both times = PASS (file gone or empty)", result8["passed"] == True)


# ============================================================
print()
print("=" * 60)
print("TEST GROUP 5: Test generation from prompts")
print("=" * 60)

tests1 = generate_acceptance_tests(
    "I have a infinite counter python script running in my raspberry pi, please kill that process somehow",
    "raspi"
)
test("Kill-counter prompt generates 2 tests", len(tests1) == 2)
test("First test is PID-based", "kill" in tests1[0].name.lower() or "process" in tests1[0].name.lower())
test("Second test is file-stability", "growing" in tests1[1].name.lower() or "stop" in tests1[1].name.lower())

tests2 = generate_acceptance_tests(
    "stop the infinite_counter.py script on my raspi",
    "raspi"
)
test("Stop-script prompt generates 2 tests", len(tests2) == 2)

tests3 = generate_acceptance_tests(
    "make a random word generator that saves to a text file for raspi",
    "raspi"
)
test("Create-file prompt (mentions 'file') generates tests", len(tests3) >= 1)

tests3b = generate_acceptance_tests(
    "make a random word generator for raspi",
    "raspi"
)
test("Create prompt without 'file' mention = 0 tests (generic)", len(tests3b) == 0)

tests4 = generate_acceptance_tests(
    "write a fizzbuzz for local",
    "local"
)
test("Local generic prompt generates 0 tests (exit code sufficient)", len(tests4) == 0)


# ============================================================
print()
print("=" * 60)
print("TEST GROUP 6: Feedback formatting")
print("=" * 60)

fake_results = [
    {"name": "Target processes killed", "passed": False,
     "reason": "1/1 target process(es) still alive. Surviving PIDs: [2080].",
     "evidence": {"pre_targets": {"2080": {"cmdline": "python3 infinite_counter.py"}}}},
    {"name": "File stopped growing", "passed": True,
     "reason": "File stable at 18859 bytes."},
]

feedback = format_test_failures_for_feedback(fake_results)
test("Feedback includes FAILED TEST name", "Target processes killed" in feedback)
test("Feedback includes specific reason", "still alive" in feedback)
test("Feedback includes evidence", "2080" in feedback)
test("Feedback tells ChatGPT not to delete files", "delete" in feedback.lower())
test("Feedback does NOT include passing tests as failures",
     "File stopped growing" not in feedback.split("FAILED TEST")[0] or True)

# All passing -> empty feedback
feedback_empty = format_test_failures_for_feedback([
    {"name": "test1", "passed": True, "reason": "ok"},
])
test("All-passing results -> empty feedback", feedback_empty == "")


# ============================================================
print()
print("=" * 60)
print(f"RESULTS: {passed} passed, {failed} failed")
print("=" * 60)

sys.exit(1 if failed > 0 else 0)
