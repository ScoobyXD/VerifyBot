#!/usr/bin/env python3
"""
chatgpt_skill.py — Browser automation for ChatGPT (no API needed)

Usage:
    python chatgpt_skill.py --login              # First time: log in manually
    python chatgpt_skill.py --prompt "question"   # Single prompt
    python chatgpt_skill.py --interactive         # Multi-prompt session
    python chatgpt_skill.py --prompt "question" --headed  # Watch the browser

Requires: pip install playwright && playwright install chromium
"""

import argparse
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

import selectors as S
from session import ChatGPTSession

# --- Config ---
PROFILE_DIR = Path(__file__).parent / ".browser_profile"
RAW_MD_DIR = Path(__file__).parent / "raw_md"
PROGRAMS_DIR = Path(__file__).parent / "programs"


def ensure_dirs():
    PROFILE_DIR.mkdir(exist_ok=True)
    RAW_MD_DIR.mkdir(exist_ok=True)
    PROGRAMS_DIR.mkdir(exist_ok=True)


def save_response(prompt: str, response: str, attempt: int = None,
                  is_retry: bool = False) -> Path:
    """Save prompt/response pair as a timestamped markdown file in raw_md/.
    
    This creates the initial file. Use append_to_log() to add terminal output,
    extraction results, run results, and feedback prompts as the pipeline progresses.
    """
    ensure_dirs()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = prompt[:40].replace(" ", "_").replace("/", "_")
    filename = f"{ts}_{slug}.md"
    filepath = RAW_MD_DIR / filename

    attempt_label = f" (Attempt {attempt})" if attempt else ""
    retry_label = " — RETRY (same conversation)" if is_retry else ""

    content = f"""# ChatGPT Response{attempt_label}{retry_label}
**Timestamp**: {datetime.now().isoformat()}  
**Prompt**: {prompt}

---

## Response

{response}
"""
    filepath.write_text(content, encoding="utf-8")
    print(f"[OK] Saved raw_md to {filepath}")
    return filepath


def append_to_log(filepath: Path, section_title: str, content: str):
    """Append a new section to an existing raw_md log file.
    
    Used by the pipeline to incrementally record everything that happens:
    extraction results, compilation output, run output, feedback prompts, etc.
    """
    if not filepath or not filepath.exists():
        return
    
    timestamp = datetime.now().strftime("%H:%M:%S")
    section = f"""

---

## {section_title}
_[{timestamp}]_

{content}
"""
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(section)


def run_login_mode():
    """Open browser for manual login. Cookies persist."""
    print("=" * 50)
    print("LOGIN MODE")
    print("Log into ChatGPT in the browser that opens.")
    print("When done, close the browser window.")
    print("Your session will be saved for future runs.")
    print("=" * 50)

    PROFILE_DIR.mkdir(exist_ok=True)
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.new_page()
        page.goto(S.CHATGPT_URL)

        try:
            page.wait_for_event("close", timeout=0)
        except Exception:
            pass

        ctx.close()

    print("[OK] Login session saved.")


def run_single_prompt(prompt: str, headed: bool = False, session: ChatGPTSession = None) -> str:
    """Send one prompt, get response, save it.
    
    If a session is provided, reuses it (for pipeline efficiency).
    Otherwise creates a temporary session.
    """
    ensure_dirs()

    if session:
        response = session.prompt(prompt)
        save_response(prompt, response)
        _print_preview(response)
        return response

    with ChatGPTSession(headed=headed) as s:
        response = s.prompt(prompt)
        filepath = save_response(prompt, response)
        _print_preview(response, filepath)
        return response


def run_single_followup(prompt: str, session: ChatGPTSession) -> str:
    """Send a follow-up in the same conversation. Requires an open session."""
    ensure_dirs()
    response = session.followup(prompt)
    save_response(prompt, response)
    _print_preview(response)
    return response


def _print_preview(response: str, filepath: Path = None):
    """Print a truncated preview of the response."""
    print("\n--- RESPONSE ---")
    preview = response[:500]
    if len(response) > 500:
        suffix = f"\n\n[... {len(response)} total chars"
        if filepath:
            suffix += f", see {filepath}"
        suffix += "]"
        preview += suffix
    print(preview)


def run_interactive(headed: bool = True):
    """Interactive mode: keep browser open, send multiple prompts."""
    ensure_dirs()

    print("=" * 50)
    print("INTERACTIVE MODE")
    print("Type prompts. 'quit' to exit. 'new' for new chat.")
    print("=" * 50)

    with ChatGPTSession(headed=headed) as s:
        while True:
            prompt = input("\n> ").strip()

            if not prompt:
                continue
            if prompt.lower() == "quit":
                break
            if prompt.lower() == "new":
                s.new_chat()
                continue

            response = s.followup(prompt)
            save_response(prompt, response)

            print("\n--- RESPONSE ---")
            print(response[:1000])
            if len(response) > 1000:
                print(f"\n[... truncated, {len(response)} total chars]")


def main():
    parser = argparse.ArgumentParser(description="ChatGPT Browser Skill")
    parser.add_argument("--login", action="store_true",
                        help="Open browser for manual login")
    parser.add_argument("--prompt", type=str,
                        help="Send a single prompt")
    parser.add_argument("--interactive", action="store_true",
                        help="Interactive multi-prompt mode")
    parser.add_argument("--headed", action="store_true",
                        help="Show browser window (default: headless for --prompt)")

    args = parser.parse_args()

    if args.login:
        run_login_mode()
    elif args.prompt:
        run_single_prompt(args.prompt, headed=args.headed)
    elif args.interactive:
        run_interactive(headed=True)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
