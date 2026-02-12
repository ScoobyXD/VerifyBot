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
import sys
import os
import json
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

import selectors as S

# --- Config ---
PROFILE_DIR = Path(__file__).parent / ".browser_profile"
OUTPUT_DIR = Path(__file__).parent / "outputs"


def ensure_dirs():
    PROFILE_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)


def launch_browser(pw, headed=True):
    """Launch Chromium with persistent profile (keeps login cookies)."""
    context = pw.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=not headed,
        viewport={"width": 1280, "height": 900},
        # Reduce bot detection
        args=[
            "--disable-blink-features=AutomationControlled",
        ],
    )
    return context


def wait_for_page_ready(page):
    """Wait for ChatGPT to fully load."""
    page.goto(S.NEW_CHAT_URL, wait_until="domcontentloaded",
              timeout=S.NAVIGATION_TIMEOUT * 1000)
    # Wait for the prompt textarea to appear
    page.wait_for_selector(S.PROMPT_TEXTAREA, timeout=S.NAVIGATION_TIMEOUT * 1000)
    print("[OK] ChatGPT loaded, prompt textarea found.")


def find_send_button(page):
    """Try multiple selectors for the send button."""
    for sel in S.SEND_BUTTON_SELECTORS:
        btn = page.query_selector(sel)
        if btn and btn.is_visible():
            return btn
    return None


def send_prompt(page, prompt_text: str):
    """Type a prompt and click send."""
    # Focus and type into the textarea
    textarea = page.wait_for_selector(S.PROMPT_TEXTAREA, timeout=10_000)
    textarea.click()
    
    # Clear any existing text
    page.keyboard.press("Control+a")
    page.keyboard.press("Backspace")
    
    # Type with human-like delay
    # For long prompts, use fill() instead of type() for speed
    if len(prompt_text) > 500:
        textarea.fill(prompt_text)
    else:
        textarea.type(prompt_text, delay=S.TYPING_DELAY_MS)
    
    time.sleep(0.5)
    
    # Click send
    send_btn = find_send_button(page)
    if send_btn:
        send_btn.click()
        print(f"[OK] Prompt sent ({len(prompt_text)} chars)")
    else:
        # Fallback: press Enter
        print("[WARN] Send button not found, trying Enter key")
        page.keyboard.press("Enter")
    
    time.sleep(S.POST_SEND_DELAY)


def wait_for_response(page, timeout=S.RESPONSE_TIMEOUT):
    """Wait for ChatGPT to finish streaming its response."""
    print("[...] Waiting for response to complete...")
    deadline = time.time() + timeout
    
    while time.time() < deadline:
        # Check if stop button is still visible (still streaming)
        still_streaming = False
        for sel in S.STOP_GENERATING_SELECTORS:
            stop_btn = page.query_selector(sel)
            if stop_btn and stop_btn.is_visible():
                still_streaming = True
                break
        
        if not still_streaming:
            # Double-check: look for completion indicators
            for sel in S.RESPONSE_COMPLETE_INDICATORS:
                indicator = page.query_selector(sel)
                if indicator and indicator.is_visible():
                    print("[OK] Response complete.")
                    return True
        
        time.sleep(1)
    
    print("[WARN] Response timeout — may be incomplete.")
    return False


def extract_last_response(page) -> str:
    """Get the text of the last assistant message."""
    for sel in S.ASSISTANT_MESSAGE_SELECTORS:
        messages = page.query_selector_all(sel)
        if messages:
            last_msg = messages[-1]
            # Get inner text (strips HTML)
            text = last_msg.inner_text()
            return text.strip()
    
    return "[ERROR] Could not extract response"


def save_response(prompt: str, response: str) -> Path:
    """Save prompt/response pair as a timestamped markdown file."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = prompt[:40].replace(" ", "_").replace("/", "_")
    filename = f"{ts}_{slug}.md"
    filepath = OUTPUT_DIR / filename
    
    content = f"""# ChatGPT Response
**Timestamp**: {datetime.now().isoformat()}  
**Prompt**: {prompt}

---

## Response

{response}
"""
    filepath.write_text(content, encoding="utf-8")
    print(f"[OK] Saved to {filepath}")
    return filepath


def run_login_mode(headed=True):
    """Open browser for manual login. Cookies persist."""
    print("=" * 50)
    print("LOGIN MODE")
    print("Log into ChatGPT in the browser that opens.")
    print("When done, close the browser window.")
    print("Your session will be saved for future runs.")
    print("=" * 50)
    
    with sync_playwright() as pw:
        ctx = launch_browser(pw, headed=True)
        page = ctx.new_page()
        page.goto(S.CHATGPT_URL)
        
        # Block until user closes the browser
        try:
            page.wait_for_event("close", timeout=0)
        except Exception:
            pass
        
        ctx.close()
    
    print("[OK] Login session saved.")


def run_single_prompt(prompt: str, headed=False):
    """Send one prompt, get response, save it."""
    ensure_dirs()
    
    with sync_playwright() as pw:
        ctx = launch_browser(pw, headed=headed)
        page = ctx.new_page()
        
        try:
            wait_for_page_ready(page)
            send_prompt(page, prompt)
            wait_for_response(page)
            
            response = extract_last_response(page)
            filepath = save_response(prompt, response)
            
            print("\n--- RESPONSE ---")
            # Print first 500 chars as preview
            preview = response[:500]
            if len(response) > 500:
                preview += f"\n\n[... {len(response)} total chars, see {filepath}]"
            print(preview)
            
            return response
        
        finally:
            ctx.close()


def run_interactive(headed=True):
    """Interactive mode: keep browser open, send multiple prompts."""
    ensure_dirs()
    
    print("=" * 50)
    print("INTERACTIVE MODE")
    print("Type prompts. 'quit' to exit. 'new' for new chat.")
    print("=" * 50)
    
    with sync_playwright() as pw:
        ctx = launch_browser(pw, headed=headed)
        page = ctx.new_page()
        
        try:
            wait_for_page_ready(page)
            
            while True:
                prompt = input("\n> ").strip()
                
                if not prompt:
                    continue
                if prompt.lower() == "quit":
                    break
                if prompt.lower() == "new":
                    page.goto(S.NEW_CHAT_URL, wait_until="domcontentloaded")
                    page.wait_for_selector(S.PROMPT_TEXTAREA, timeout=15_000)
                    print("[OK] New chat started.")
                    continue
                
                send_prompt(page, prompt)
                wait_for_response(page)
                
                response = extract_last_response(page)
                save_response(prompt, response)
                
                print("\n--- RESPONSE ---")
                print(response[:1000])
                if len(response) > 1000:
                    print(f"\n[... truncated, {len(response)} total chars]")
        
        finally:
            ctx.close()


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
