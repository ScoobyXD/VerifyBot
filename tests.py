#!/usr/bin/env python3
"""
tests.py -- Automated test suite for VerifyBot.

Runs a series of prompts through the full pipeline (ChatGPT -> extract -> execute -> verify).
Each test is a real end-to-end run. If the LLM verifies PASS, the test passes.

Usage:
    python tests.py                 # Run all tests
    python tests.py --test 1        # Run only test #1
    python tests.py --target local  # Force all tests to local target
    python tests.py --headless      # Hide browser

This runs automatically as the final step of first-time setup.
"""

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Test prompts -- add more here
# ---------------------------------------------------------------------------

TESTS = [
    {
        "name": "Simple file creation",
        "prompt": "make me a test folder called verifybot_test inside my directory, "
                  "and put a file called hello.txt inside it that says 'VerifyBot works!'",
        "target": "local",
    },
    {
        "name": "Math + logic script",
        "prompt": "Create a random number generator that picks a number between 1 and 1000, "
                  "then finds the next prime number after it, does this 10 times, keeps a "
                  "sliding window of 5 of those primes, computes the average of that window "
                  "each step, and prints everything clearly",
        "target": "local",
    },
    {
        "name": "Cleanup test folder",
        "prompt": "Delete the folder called verifybot_test in my directory and everything "
                  "inside it. Confirm it no longer exists after deletion.",
        "target": "local",
        "depends_on": 1,  # only runs if test 1 passed
    },
]

# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_tests(test_indices: list = None, target_override: str = None,
              headed: bool = True, max_retries: int = 3, timeout: int = 30):
    """Run test prompts through the full VerifyBot pipeline.

    Returns True if all tests pass, False if any fail.
    """
    # Import here so tests.py can be imported without triggering
    # heavy imports (useful for setup.py calling run_tests directly)
    from main import run_pipeline

    tests_to_run = []
    if test_indices:
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

    print()
    print("=" * 60)
    print("  VERIFYBOT TEST SUITE")
    print("=" * 60)
    print(f"  Tests:   {len(tests_to_run)}")
    print(f"  Target:  {target_override or 'per-test default'}")
    print(f"  Retries: {max_retries}")
    print(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    results = []
    passed_tests = set()  # track which test numbers passed (for depends_on)
    start_time = time.time()

    for num, test in tests_to_run:
        target = target_override or test.get("target", "local")
        prompt = test["prompt"]

        # Check dependency -- skip if the test we depend on didn't pass
        dep = test.get("depends_on")
        if dep is not None and dep not in passed_tests:
            print()
            print(f"  {'─' * 56}")
            print(f"  TEST {num}/{len(TESTS)}: {test['name']}")
            print(f"  \033[93m[SKIP] Depends on test {dep} which did not pass\033[0m")
            print(f"  {'─' * 56}")
            results.append({
                "num": num, "name": test["name"],
                "passed": False, "skipped": True, "elapsed": 0,
            })
            continue

        print()
        print(f"  {'─' * 56}")
        print(f"  TEST {num}/{len(TESTS)}: {test['name']}")
        print(f"  Target: {target}")
        print(f"  Prompt: {prompt[:70]}{'...' if len(prompt) > 70 else ''}")
        print(f"  {'─' * 56}")
        print()

        test_start = time.time()

        try:
            passed = run_pipeline(
                prompt=prompt,
                target=target,
                max_retries=max_retries,
                timeout=timeout,
                headed=headed,
            )
        except Exception as e:
            print(f"\n  [ERROR] Test crashed: {e}")
            passed = False

        elapsed = time.time() - test_start

        if passed:
            passed_tests.add(num)

        status = "PASS" if passed else "FAIL"
        color = "\033[92m" if passed else "\033[91m"
        print(f"\n  {color}[{status}]\033[0m Test {num}: {test['name']} ({elapsed:.1f}s)")

        results.append({
            "num": num,
            "name": test["name"],
            "passed": passed,
            "elapsed": elapsed,
        })

    # --- Summary ---
    total_time = time.time() - start_time
    passed_count = sum(1 for r in results if r["passed"])
    failed_count = len(results) - passed_count

    print()
    print("=" * 60)
    print("  TEST RESULTS")
    print("=" * 60)

    for r in results:
        if r.get("skipped"):
            status = "\033[93mSKIP\033[0m"
        elif r["passed"]:
            status = "\033[92mPASS\033[0m"
        else:
            status = "\033[91mFAIL\033[0m"
        print(f"  [{status}] Test {r['num']}: {r['name']} ({r['elapsed']:.1f}s)")

    print(f"  {'─' * 56}")
    skipped_count = sum(1 for r in results if r.get("skipped"))
    print(f"  Total: {len(results)} tests, {passed_count} passed, {failed_count} failed"
          f"{f', {skipped_count} skipped' if skipped_count else ''}")
    print(f"  Time:  {total_time:.1f}s")

    if failed_count == 0:
        print(f"\n  \033[92mALL TESTS PASSED\033[0m")
    else:
        print(f"\n  \033[91m{failed_count} TEST(S) FAILED\033[0m")

    print("=" * 60)
    print()

    return failed_count == 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="VerifyBot Test Suite -- end-to-end pipeline tests",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python tests.py                  # Run all tests\n"
            "  python tests.py --test 1         # Run only test #1\n"
            "  python tests.py --test 1 --test 2  # Run tests 1 and 2\n"
            "  python tests.py --target raspi   # Force all tests to Pi\n"
            "  python tests.py --headless       # Hide browser\n"
        ),
    )
    parser.add_argument("--test", type=int, action="append", dest="tests",
                        help="Run specific test number(s)")
    parser.add_argument("--target", choices=["local", "raspi"], default=None,
                        help="Force target for all tests")
    parser.add_argument("--headless", action="store_true",
                        help="Hide browser window")
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
    )

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
