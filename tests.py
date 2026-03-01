#!/usr/bin/env python3
"""
tests.py -- Parallel multi-agent test suite for VerifyBot.

Runs test "streams" in parallel. Each stream gets its own browser session
(its own ChatGPT agent) with a CLONED browser profile so multiple Chromium
instances can coexist without locking the same user_data_dir.

Results are appended to raw_md/test.md as a complete interaction record.
After all streams finish, the final test.md is fed to a ChatGPT agent
that asserts which tests passed and which failed.

KEY DESIGN: Playwright's launch_persistent_context locks the user_data_dir.
Only one Chromium instance can use a profile directory at a time. To run N
parallel agents, we copy .browser_profile/ into N separate directories
(.browser_profile_A/, .browser_profile_B/, etc.) before launching. Each
agent gets its own copy with the same cookies/session. After tests finish,
the cloned directories are cleaned up.

Usage:
    python tests.py                 # Run all tests in parallel
    python tests.py --test 1        # Run only test #1
    python tests.py --target local  # Force all tests to local target
    python tests.py --headless      # Hide browser
    python tests.py --sequential    # Disable parallelism (debug mode)

This runs automatically as the final step of first-time setup.
"""

import argparse
import shutil
import sys
import time
import threading
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RAW_MD_DIR = ROOT / "raw_md"
TEST_MD = RAW_MD_DIR / "test.md"
BROWSER_PROFILE = ROOT / ".browser_profile"

# ---------------------------------------------------------------------------
# Test definitions
# ---------------------------------------------------------------------------
# Each test has:
#   name     -- human-readable label
#   prompt   -- what to send through the pipeline
#   target   -- "local" or "raspi"
#   stream   -- tests in the same stream run sequentially; different streams
#               run in parallel. Use the same stream ID for dependent tests.
#   depends_on -- (optional) test number that must pass before this runs
#
# Stream assignment rules:
#   - Dependent tests (e.g. create then delete) share a stream
#   - Independent tests each get their own stream
#   - This is how we get parallelism: N streams = N concurrent agents
# ---------------------------------------------------------------------------

TESTS = [
    # --- Stream A: file create + delete (sequential dependency) ---
    {
        "name": "Simple file creation",
        "prompt": "make me a test folder called verifybot_test inside my directory, "
                  "and put a file called hello.txt inside it that says 'VerifyBot works!'",
        "target": "local",
        "stream": "A",
    },
    {
        "name": "Cleanup test folder",
        "prompt": "Delete the folder called verifybot_test in my directory and everything "
                  "inside it. Confirm it no longer exists after deletion.",
        "target": "local",
        "stream": "A",
        "depends_on": 1,
    },

    # --- Stream B: math + logic (independent) ---
    {
        "name": "Math + logic script",
        "prompt": "Create a random number generator that picks a number between 1 and 1000, "
                  "then finds the next prime number after it, does this 10 times, keeps a "
                  "sliding window of 5 of those primes, computes the average of that window "
                  "each step, and prints everything clearly",
        "target": "local",
        "stream": "B",
    },

    # --- Stream C: word frequency analysis -> file output (independent) ---
    {
        "name": "Word frequency to file",
        "prompt": "Write a script that takes this paragraph: "
                  "'The quick brown fox jumps over the lazy dog. The dog barked at the fox. "
                  "The fox ran away quickly but the dog chased the fox across the brown field. "
                  "The lazy dog eventually gave up and the quick fox escaped.' "
                  "Count the frequency of each word (case-insensitive), sort them from most "
                  "to least frequent, and write the results to a file called word_freq.txt "
                  "in the current directory. Each line should be: 'word: count'. "
                  "Also print the top 5 words to stdout.",
        "target": "local",
        "stream": "C",
    },

    # --- Stream D: Fibonacci + string manipulation (independent) ---
    {
        "name": "Fibonacci cipher output",
        "prompt": "Write a script that generates the first 15 Fibonacci numbers, then for "
                  "each Fibonacci number, maps it to a letter of the alphabet (1=a, 2=b, ... "
                  "26=z, wrapping around with modulo 26 for numbers > 26, and 0 maps to z). "
                  "Collect the letters into a string and print: "
                  "1) each Fibonacci number with its mapped letter, "
                  "2) the final concatenated string, "
                  "3) the string reversed. "
                  "Save all output to a file called fib_cipher.txt in the current directory "
                  "AND print everything to stdout.",
        "target": "local",
        "stream": "D",
    },
]


# ---------------------------------------------------------------------------
# Browser profile management
# ---------------------------------------------------------------------------

def clone_profile(stream_id: str) -> Path:
    """Clone .browser_profile/ into a stream-specific directory.

    Playwright locks user_data_dir so each parallel Chromium instance
    needs its own copy. We copy the whole directory to preserve cookies
    and login session.

    Returns the path to the cloned profile directory.
    """
    clone_dir = ROOT / f".browser_profile_{stream_id}"

    if clone_dir.exists():
        shutil.rmtree(clone_dir, ignore_errors=True)

    if BROWSER_PROFILE.exists():
        shutil.copytree(BROWSER_PROFILE, clone_dir, dirs_exist_ok=True)
    else:
        clone_dir.mkdir(parents=True, exist_ok=True)

    return clone_dir


def cleanup_cloned_profiles(stream_ids: list):
    """Remove cloned profile directories after tests finish."""
    for sid in stream_ids:
        clone_dir = ROOT / f".browser_profile_{sid}"
        if clone_dir.exists():
            try:
                shutil.rmtree(clone_dir, ignore_errors=True)
                print(f"  [CLEANUP] Removed .browser_profile_{sid}/")
            except Exception as e:
                print(f"  [WARN] Could not remove .browser_profile_{sid}/: {e}")


# ---------------------------------------------------------------------------
# test.md writer (thread-safe)
# ---------------------------------------------------------------------------

_md_lock = threading.Lock()


def init_test_md():
    """Initialize raw_md/test.md with header."""
    RAW_MD_DIR.mkdir(exist_ok=True)
    with _md_lock:
        TEST_MD.write_text(
            f"# VerifyBot Test Results\n"
            f"_Generated: {datetime.now().isoformat()}_\n\n"
            f"---\n\n",
            encoding="utf-8",
        )


def append_test_md(content: str):
    """Thread-safe append to test.md."""
    with _md_lock:
        with open(TEST_MD, "a", encoding="utf-8") as f:
            f.write(content)


def write_test_result(num: int, test: dict, passed: bool, elapsed: float,
                      skipped: bool = False, error: str = None):
    """Write a single test result block to test.md."""
    status = "SKIP" if skipped else ("PASS" if passed else "FAIL")
    ts = datetime.now().strftime("%H:%M:%S")

    block = (
        f"## Test {num}: {test['name']}\n"
        f"- **Status**: {status}\n"
        f"- **Stream**: {test.get('stream', '?')}\n"
        f"- **Target**: {test.get('target', 'local')}\n"
        f"- **Elapsed**: {elapsed:.1f}s\n"
        f"- **Completed**: {ts}\n"
        f"- **Prompt**: {test['prompt'][:200]}{'...' if len(test['prompt']) > 200 else ''}\n"
    )
    if error:
        # Truncate error to first line -- full Playwright crash dumps are noise
        error_short = error.split("\n")[0][:200]
        block += f"- **Error**: {error_short}\n"
    if skipped and test.get("depends_on"):
        block += f"- **Reason**: Depends on test {test['depends_on']} which did not pass\n"
    block += "\n---\n\n"
    append_test_md(block)


def write_summary(results: list, total_time: float, num_streams: int):
    """Write the summary section to test.md.

    Results are sorted by test number so the table matches the console output.
    """
    results_sorted = sorted(results, key=lambda x: x["num"])
    passed = sum(1 for r in results_sorted if r["passed"])
    failed = sum(1 for r in results_sorted if not r["passed"] and not r.get("skipped"))
    skipped = sum(1 for r in results_sorted if r.get("skipped"))

    summary = (
        f"## Summary\n\n"
        f"| # | Test | Stream | Status | Time |\n"
        f"|---|------|--------|--------|------|\n"
    )
    for r in results_sorted:
        st = "SKIP" if r.get("skipped") else ("PASS" if r["passed"] else "FAIL")
        summary += f"| {r['num']} | {r['name']} | {r.get('stream', '?')} | {st} | {r['elapsed']:.1f}s |\n"

    summary += (
        f"\n**Total**: {len(results_sorted)} tests, {passed} passed, {failed} failed"
        f"{f', {skipped} skipped' if skipped else ''}\n"
        f"**Total time**: {total_time:.1f}s\n"
        f"**Parallel streams**: {num_streams}\n\n"
        f"---\n\n"
    )
    append_test_md(summary)


# ---------------------------------------------------------------------------
# Stream runner -- each stream is one "agent" with its own browser session
# ---------------------------------------------------------------------------

def run_stream(stream_id: str, stream_tests: list,
               target_override: str, headed: bool,
               max_retries: int, timeout: int,
               profile_dir: Path,
               results: list, results_lock: threading.Lock,
               passed_tests: set, passed_lock: threading.Lock):
    """Run all tests in a single stream sequentially.

    Each stream gets its own ChatGPT browser session (its own agent)
    using a cloned browser profile directory so it doesn't conflict
    with other parallel agents.
    """
    from main import run_pipeline

    agent_label = f"Agent-{stream_id}"

    for num, test in stream_tests:
        target = target_override or test.get("target", "local")
        prompt = test["prompt"]

        # Check dependency
        dep = test.get("depends_on")
        if dep is not None:
            with passed_lock:
                dep_passed = dep in passed_tests
            if not dep_passed:
                print(f"\n  [{agent_label}] TEST {num}: {test['name']} -- SKIPPED (depends on #{dep})")
                result = {
                    "num": num, "name": test["name"], "stream": stream_id,
                    "passed": False, "skipped": True, "elapsed": 0,
                }
                with results_lock:
                    results.append(result)
                write_test_result(num, test, False, 0, skipped=True)
                continue

        print(f"\n  [{agent_label}] TEST {num}: {test['name']}")
        print(f"  [{agent_label}] Target: {target}")
        print(f"  [{agent_label}] Prompt: {prompt[:70]}{'...' if len(prompt) > 70 else ''}")

        test_start = time.time()
        error_msg = None

        try:
            passed = run_pipeline(
                prompt=prompt,
                target=target,
                max_retries=max_retries,
                timeout=timeout,
                headed=headed,
                profile_dir=profile_dir,
            )
        except Exception as e:
            print(f"\n  [{agent_label}] TEST {num} CRASHED: {e}")
            passed = False
            error_msg = str(e)

        elapsed = time.time() - test_start

        if passed:
            with passed_lock:
                passed_tests.add(num)

        status = "PASS" if passed else "FAIL"
        color = "\033[92m" if passed else "\033[91m"
        print(f"\n  [{agent_label}] {color}[{status}]\033[0m Test {num}: {test['name']} ({elapsed:.1f}s)")

        result = {
            "num": num, "name": test["name"], "stream": stream_id,
            "passed": passed, "elapsed": elapsed,
        }
        with results_lock:
            results.append(result)

        write_test_result(num, test, passed, elapsed, error=error_msg)


# ---------------------------------------------------------------------------
# Final LLM assertion -- feed test.md to ChatGPT for verdict
# ---------------------------------------------------------------------------

def llm_assert_results(headed: bool = True) -> str:
    """Read test.md and ask ChatGPT to give the final pass/fail verdict.

    Uses the main .browser_profile (not a clone) since all parallel
    agents are done by now.

    Returns the LLM's assertion text.
    """
    from core.session import ChatGPTSession

    if not TEST_MD.exists():
        return "(test.md not found)"

    test_content = TEST_MD.read_text(encoding="utf-8")

    assertion_prompt = (
        "I just ran my automated test suite. Here are the complete results:\n\n"
        f"{test_content}\n\n"
        "Based on these results, give me a clear final verdict:\n"
        "1. List each test by number and name with PASS/FAIL/SKIP\n"
        "2. For any FAILed tests, briefly note what went wrong if visible\n"
        "3. Give the overall result: ALL PASSED or X FAILED\n"
        "4. If there were parallel streams, note whether parallelism worked correctly\n\n"
        "Keep it concise. Start your response with 'VERDICT:'"
    )

    try:
        with ChatGPTSession(headed=headed) as session:
            response = session.prompt(assertion_prompt)

        # Append LLM verdict to test.md
        append_test_md(
            f"## LLM Assertion\n\n"
            f"{response}\n\n"
            f"---\n"
            f"_Assertion completed: {datetime.now().isoformat()}_\n"
        )

        return response
    except Exception as e:
        msg = f"(LLM assertion failed: {e})"
        append_test_md(f"## LLM Assertion\n\n{msg}\n")
        return msg


# ---------------------------------------------------------------------------
# Orchestrator -- groups tests into streams and launches threads
# ---------------------------------------------------------------------------

def run_tests(test_indices: list = None, target_override: str = None,
              headed: bool = True, max_retries: int = 3, timeout: int = 30,
              sequential: bool = False):
    """Run test suite with parallel streams.

    Tests are grouped by their 'stream' field. Each stream runs in
    its own thread with its own CLONED browser profile. Independent tests
    run concurrently; dependent tests run sequentially within their stream.

    Returns True if all tests pass, False if any fail.
    """
    # Import here to avoid circular imports at module level
    from main import run_pipeline  # noqa: just to verify import works

    # Filter tests
    if test_indices:
        tests_to_run = []
        for i in test_indices:
            if 1 <= i <= len(TESTS):
                tests_to_run.append((i, TESTS[i - 1]))
            else:
                print(f"[WARN] Test #{i} does not exist (have {len(TESTS)} tests)")
    else:
        tests_to_run = [(i + 1, t) for i, t in enumerate(TESTS)]

    if not tests_to_run:
        print("[ERROR] No tests to run.")
        return False

    # Group by stream
    streams = {}
    for num, test in tests_to_run:
        sid = test.get("stream", f"auto_{num}")
        if sid not in streams:
            streams[sid] = []
        streams[sid].append((num, test))

    stream_ids = sorted(streams.keys())

    # Initialize test.md in raw_md/
    init_test_md()
    append_test_md(
        f"## Configuration\n\n"
        f"- **Tests**: {len(tests_to_run)}\n"
        f"- **Streams**: {len(streams)} ({', '.join(stream_ids)})\n"
        f"- **Target override**: {target_override or 'per-test default'}\n"
        f"- **Max retries**: {max_retries}\n"
        f"- **Mode**: {'sequential' if sequential else 'parallel'}\n"
        f"- **Started**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"---\n\n"
    )

    # Console banner
    print()
    print("=" * 60)
    print("  VERIFYBOT PARALLEL TEST SUITE")
    print("=" * 60)
    print(f"  Tests:    {len(tests_to_run)}")
    print(f"  Streams:  {len(streams)} ({', '.join(stream_ids)})")
    print(f"  Mode:     {'sequential' if sequential else 'parallel'}")
    print(f"  Target:   {target_override or 'per-test default'}")
    print(f"  Retries:  {max_retries}")
    print(f"  Started:  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # Print stream layout
    for sid in stream_ids:
        tests_in_stream = streams[sid]
        test_names = " -> ".join(f"#{n}" for n, _ in tests_in_stream)
        print(f"  Stream {sid}: {test_names}")
    print()

    # Clone browser profiles for parallel agents
    if not sequential and len(streams) > 1:
        print("  Cloning browser profiles for parallel agents...")
        profile_dirs = {}
        for sid in stream_ids:
            profile_dirs[sid] = clone_profile(sid)
            print(f"    Stream {sid} -> .browser_profile_{sid}/")
        print()
    else:
        # Sequential mode: all streams share the original profile
        profile_dirs = {sid: BROWSER_PROFILE for sid in stream_ids}

    # Shared state
    results = []
    results_lock = threading.Lock()
    passed_tests = set()
    passed_lock = threading.Lock()

    start_time = time.time()

    if sequential or len(streams) == 1:
        # Sequential mode: run all streams one at a time
        for sid in stream_ids:
            run_stream(
                stream_id=sid,
                stream_tests=streams[sid],
                target_override=target_override,
                headed=headed,
                max_retries=max_retries,
                timeout=timeout,
                profile_dir=profile_dirs[sid],
                results=results,
                results_lock=results_lock,
                passed_tests=passed_tests,
                passed_lock=passed_lock,
            )
    else:
        # Parallel mode: each stream in its own thread
        threads = []
        for sid in stream_ids:
            t = threading.Thread(
                target=run_stream,
                args=(sid, streams[sid], target_override, headed,
                      max_retries, timeout, profile_dirs[sid],
                      results, results_lock,
                      passed_tests, passed_lock),
                name=f"Stream-{sid}",
                daemon=True,
            )
            threads.append(t)

        # Launch all threads with stagger to avoid I/O contention
        print(f"  Launching {len(threads)} parallel agents...")
        for t in threads:
            t.start()
            # 3s stagger so Chromium instances don't fight over disk I/O
            time.sleep(3)

        # Wait for all to finish
        for t in threads:
            t.join()

        # Cleanup cloned profiles
        print()
        cleanup_cloned_profiles(stream_ids)

    total_time = time.time() - start_time

    # Write summary to test.md
    write_summary(results, total_time, len(streams))

    # --- Console summary (sorted by test number to match test.md) ---
    results_sorted = sorted(results, key=lambda x: x["num"])
    passed_count = sum(1 for r in results_sorted if r["passed"])
    failed_count = sum(1 for r in results_sorted if not r["passed"] and not r.get("skipped"))
    skipped_count = sum(1 for r in results_sorted if r.get("skipped"))

    print()
    print("=" * 60)
    print("  TEST RESULTS")
    print("=" * 60)

    for r in results_sorted:
        if r.get("skipped"):
            status = "\033[93mSKIP\033[0m"
        elif r["passed"]:
            status = "\033[92mPASS\033[0m"
        else:
            status = "\033[91mFAIL\033[0m"
        stream_tag = f"[{r.get('stream', '?')}]"
        print(f"  [{status}] {stream_tag} Test {r['num']}: {r['name']} ({r['elapsed']:.1f}s)")

    print(f"  {'─' * 56}")
    print(f"  Total: {len(results_sorted)} tests, {passed_count} passed, {failed_count} failed"
          f"{f', {skipped_count} skipped' if skipped_count else ''}")
    print(f"  Time:  {total_time:.1f}s (parallel across {len(streams)} streams)")

    if failed_count == 0:
        print(f"\n  \033[92mALL TESTS PASSED\033[0m")
    else:
        print(f"\n  \033[91m{failed_count} TEST(S) FAILED\033[0m")

    print("=" * 60)

    # --- LLM Final Assertion ---
    print()
    print("=" * 60)
    print("  LLM FINAL ASSERTION")
    print("=" * 60)
    print("  Feeding test.md to ChatGPT for final verdict...")
    print()

    verdict = llm_assert_results(headed=headed)

    print()
    print("─" * 60)
    print("  LLM VERDICT:")
    print("─" * 60)
    for line in verdict.split("\n"):
        print(f"  {line}")
    print("─" * 60)

    print(f"\n  Full record: {TEST_MD}")
    print()

    return failed_count == 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="VerifyBot Parallel Test Suite -- multi-agent swarm testing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python tests.py                     # Run all tests in parallel\n"
            "  python tests.py --test 1             # Run only test #1\n"
            "  python tests.py --test 1 --test 2    # Run tests 1 and 2\n"
            "  python tests.py --target raspi       # Force all tests to Pi\n"
            "  python tests.py --headless           # Hide browser\n"
            "  python tests.py --sequential         # Disable parallelism\n"
        ),
    )
    parser.add_argument("--test", type=int, action="append", dest="tests",
                        help="Run specific test number(s)")
    parser.add_argument("--target", choices=["local", "raspi"], default=None,
                        help="Force target for all tests")
    parser.add_argument("--headless", action="store_true",
                        help="Hide browser window")
    parser.add_argument("--sequential", action="store_true",
                        help="Run streams sequentially instead of in parallel (debug mode)")
    parser.add_argument("--max-retries", type=int, default=3,
                        help="Max retries per test (default: 3)")
    parser.add_argument("--timeout", type=int, default=30,
                        help="Execution timeout per test (default: 30)")

    args = parser.parse_args()

    all_passed = run_tests(
        test_indices=args.tests,
        target_override=args.target,
        headed=not args.headless,
        max_retries=args.max_retries,
        timeout=args.timeout,
        sequential=args.sequential,
    )

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()