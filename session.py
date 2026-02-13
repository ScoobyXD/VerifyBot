#!/usr/bin/env python3
"""
session.py — Persistent browser session for ChatGPT automation.

Instead of opening/closing the browser for every prompt, this keeps
a single browser context alive. Supports:
  - Reusable session across multiple prompts
  - Same-conversation follow-ups (multi-turn)
  - New chat on demand
  - Context manager for clean lifecycle

Usage:
    from session import ChatGPTSession

    with ChatGPTSession(headed=True) as s:
        r1 = s.prompt("Write a fizzbuzz in Python")
        r2 = s.followup("Now make it count to 100 instead")
        s.new_chat()
        r3 = s.prompt("Write a linked list in C")
"""

import time
from pathlib import Path

from playwright.sync_api import sync_playwright, Playwright, BrowserContext, Page

import selectors as S

PROFILE_DIR = Path(__file__).parent / ".browser_profile"


class ChatGPTSession:
    """Persistent browser session that stays open across multiple prompts."""

    def __init__(self, headed: bool = False):
        self.headed = headed
        self._pw: Playwright = None
        self._ctx: BrowserContext = None
        self._page: Page = None
        self._in_conversation = False  # True after first prompt in a chat

    # --- Lifecycle ---

    def open(self):
        """Launch browser and navigate to ChatGPT."""
        PROFILE_DIR.mkdir(exist_ok=True)
        self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=not self.headed,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._page = self._ctx.new_page()
        self._navigate_to_new_chat()
        return self

    def close(self):
        """Shut down browser."""
        if self._ctx:
            try:
                self._ctx.close()
            except Exception:
                pass
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass
        self._page = None
        self._ctx = None
        self._pw = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    # --- Navigation ---

    def _navigate_to_new_chat(self):
        """Go to a fresh ChatGPT conversation."""
        self._page.goto(
            S.NEW_CHAT_URL,
            wait_until="domcontentloaded",
            timeout=S.NAVIGATION_TIMEOUT * 1000,
        )
        self._page.wait_for_selector(
            S.PROMPT_TEXTAREA, timeout=S.NAVIGATION_TIMEOUT * 1000
        )
        self._in_conversation = False
        print("[OK] ChatGPT loaded, ready for prompt.")

    def new_chat(self):
        """Start a brand new conversation (discards current thread)."""
        self._navigate_to_new_chat()
        print("[OK] New chat started.")

    # --- Sending ---

    def _send_and_wait(self, text: str) -> str:
        """Type text, send it, wait for response, return extracted text.
        
        For long prompts (>500 chars), uses clipboard paste to avoid
        content being mangled by fill() or type() in the contenteditable div.
        """
        page = self._page

        # Focus textarea
        textarea = page.wait_for_selector(S.PROMPT_TEXTAREA, timeout=10_000)
        textarea.click()
        page.keyboard.press("Control+a")
        page.keyboard.press("Backspace")
        time.sleep(0.3)

        # Insert text — use evaluate to set innerHTML/textContent directly
        # for long prompts, since fill() and type() mangle multi-line content
        if len(text) > 200:
            # Use Playwright's fill which works on contenteditable divs
            # But first, set the text via JS to preserve newlines
            escaped = text.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")
            page.evaluate(f"""() => {{
                const el = document.querySelector('{S.PROMPT_TEXTAREA}');
                if (el) {{
                    // Clear existing content
                    el.innerHTML = '';
                    // Create a proper paragraph structure
                    const lines = `{escaped}`.split('\\n');
                    for (const line of lines) {{
                        const p = document.createElement('p');
                        p.textContent = line || '\\u00A0';  // non-breaking space for empty lines
                        el.appendChild(p);
                    }}
                    // Trigger React's change detection
                    el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                }}
            }}""")
            time.sleep(0.5)
        else:
            textarea.type(text, delay=S.TYPING_DELAY_MS)

        time.sleep(0.5)

        # Click send
        send_btn = self._find_send_button()
        if send_btn:
            send_btn.click()
            print(f"[OK] Sent ({len(text)} chars)")
        else:
            # Fallback: try Ctrl+Enter which is a common send shortcut
            print("[WARN] Send button not found, trying Ctrl+Enter then Enter")
            page.keyboard.press("Control+Enter")
            time.sleep(0.5)
            # Check if that worked (stop button appeared)
            still_no_response = True
            for sel in S.STOP_GENERATING_SELECTORS:
                if page.query_selector(sel):
                    still_no_response = False
                    break
            if still_no_response:
                page.keyboard.press("Enter")

        time.sleep(S.POST_SEND_DELAY)

        # Wait for streaming to finish
        self._wait_for_response()

        # Extract
        response = self._extract_last_response()
        self._in_conversation = True
        return response

    def prompt(self, text: str) -> str:
        """Send a prompt. If already in a conversation, starts a new chat first."""
        if self._in_conversation:
            self.new_chat()
        return self._send_and_wait(text)

    def followup(self, text: str) -> str:
        """Send a follow-up in the SAME conversation (multi-turn).
        
        If not in a conversation yet, this behaves like prompt().
        """
        return self._send_and_wait(text)

    # --- Internal helpers ---

    def _find_send_button(self):
        for sel in S.SEND_BUTTON_SELECTORS:
            btn = self._page.query_selector(sel)
            if btn and btn.is_visible():
                return btn
        return None

    def _wait_for_response(self, timeout=S.RESPONSE_TIMEOUT):
        print("[...] Waiting for response...")
        deadline = time.time() + timeout

        while time.time() < deadline:
            still_streaming = False
            for sel in S.STOP_GENERATING_SELECTORS:
                stop_btn = self._page.query_selector(sel)
                if stop_btn and stop_btn.is_visible():
                    still_streaming = True
                    break

            if not still_streaming:
                for sel in S.RESPONSE_COMPLETE_INDICATORS:
                    indicator = self._page.query_selector(sel)
                    if indicator and indicator.is_visible():
                        print("[OK] Response complete.")
                        return True

            time.sleep(1)

        print("[WARN] Response timeout — may be incomplete.")
        return False

    def _extract_last_response(self) -> str:
        """Extract the last assistant message with proper code fences."""
        page = self._page

        for sel in S.ASSISTANT_MESSAGE_SELECTORS:
            messages = page.query_selector_all(sel)
            if not messages:
                continue

            last_msg = messages[-1]

            # Extract code blocks from DOM
            code_blocks = []
            for pre in last_msg.query_selector_all("pre"):
                code_el = pre.query_selector("code")
                if code_el:
                    classes = code_el.get_attribute("class") or ""
                    lang = ""
                    for cls in classes.split():
                        if cls.startswith("language-"):
                            lang = cls.replace("language-", "")
                            break
                        elif cls.startswith("lang-"):
                            lang = cls.replace("lang-", "")
                            break
                    code_blocks.append((lang, code_el.inner_text()))

            # Get full text
            full_text = last_msg.inner_text()

            # Reconstruct code fences
            for lang, code_text in code_blocks:
                clean_code = code_text.strip()
                mangled_variants = []
                if lang:
                    mangled_variants.append(f"{lang}\nCopy code\n{clean_code}")
                    mangled_variants.append(f"{lang}\n Copy code\n{clean_code}")
                    mangled_variants.append(f"{lang}\nCopy\n{clean_code}")
                    mangled_variants.append(f"{lang}\n{clean_code}")

                fenced = f"```{lang}\n{clean_code}\n```"

                replaced = False
                for mangled in mangled_variants:
                    if mangled in full_text:
                        full_text = full_text.replace(mangled, fenced, 1)
                        replaced = True
                        break

                if not replaced:
                    full_text += f"\n\n{fenced}\n"

            return full_text.strip()

        return "[ERROR] Could not extract response"
