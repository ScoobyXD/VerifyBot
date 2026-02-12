#!/usr/bin/env python3
"""
test_setup.py — Verify Playwright is installed and can reach ChatGPT.

Run this AFTER:
    pip install playwright
    playwright install chromium
    python chatgpt_skill.py --login  (log in manually first)
"""

from playwright.sync_api import sync_playwright
from pathlib import Path
import selectors as S

PROFILE_DIR = Path(__file__).parent / ".browser_profile"


def test():
    print("[1/4] Checking Playwright installation...")
    with sync_playwright() as pw:
        print("[OK] Playwright loaded.")

        print("[2/4] Launching browser with saved profile...")
        if not PROFILE_DIR.exists():
            print("[FAIL] No saved profile. Run: python chatgpt_skill.py --login")
            return False

        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.new_page()

        print("[3/4] Navigating to ChatGPT...")
        page.goto(S.CHATGPT_URL, wait_until="domcontentloaded", timeout=30_000)

        print("[4/4] Looking for prompt textarea...")
        try:
            page.wait_for_selector(S.PROMPT_TEXTAREA, timeout=15_000)
            print("[OK] Prompt textarea found — you're logged in!")
            print("\n✅ Setup verified. You're ready to automate.")
            success = True
        except Exception:
            print("[FAIL] Prompt textarea not found.")
            print("Possible causes:")
            print("  - Not logged in (run --login first)")
            print("  - ChatGPT changed their DOM (update selectors.py)")
            print("  - Cloudflare challenge blocking")
            success = False

        ctx.close()
        return success


if __name__ == "__main__":
    test()
