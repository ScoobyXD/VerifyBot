"""
session.py -- Persistent browser session for ChatGPT.

Manages one Chromium context with saved cookies. Supports:
    - prompt()    Start a NEW conversation (opens fresh chat)
    - followup()  Continue the SAME conversation (multi-turn)
    - new_chat()  Manually start a new conversation

Usage:
    with ChatGPTSession(headed=True) as s:
        response = s.prompt("Write hello world")
        fix = s.followup("That has a bug, fix it")
"""

import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from core import selectors as S

PROFILE_DIR = Path(__file__).resolve().parent.parent / ".browser_profile"


class ChatGPTSession:
    """Persistent ChatGPT browser session."""

    def __init__(self, headed: bool = True):
        self._headed = headed
        self._pw = None
        self._ctx = None
        self._page = None
        self._in_conversation = False

    def __enter__(self):
        self._pw = sync_playwright().start()
        PROFILE_DIR.mkdir(exist_ok=True)
        self._ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=not self._headed,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._page = self._ctx.new_page()
        self._navigate_to_new_chat()
        return self

    def __exit__(self, *args):
        if self._ctx:
            self._ctx.close()
        if self._pw:
            self._pw.stop()

    # --- Public API ---

    def prompt(self, text: str) -> str:
        """Send a prompt. Starts a new chat if already in a conversation."""
        if self._in_conversation:
            self.new_chat()
        return self._send_and_wait(text)

    def followup(self, text: str) -> str:
        """Send a follow-up in the SAME conversation (multi-turn)."""
        return self._send_and_wait(text)

    def new_chat(self):
        """Start a brand new conversation."""
        self._navigate_to_new_chat()
        self._in_conversation = False
        print("[OK] New chat started.")

    # --- Internals ---

    def _navigate_to_new_chat(self):
        self._page.goto(S.NEW_CHAT_URL, wait_until="domcontentloaded",
                        timeout=S.NAVIGATION_TIMEOUT * 1000)
        self._page.wait_for_selector(S.PROMPT_TEXTAREA,
                                     timeout=S.NAVIGATION_TIMEOUT * 1000)
        print("[OK] ChatGPT loaded, ready for prompt.")

    def _send_and_wait(self, text: str) -> str:
        page = self._page

        # Focus and clear textarea
        textarea = page.wait_for_selector(S.PROMPT_TEXTAREA, timeout=10_000)
        textarea.click()
        page.keyboard.press("Control+a")
        page.keyboard.press("Backspace")

        # Type (fast fill for long prompts)
        if len(text) > 500:
            textarea.fill(text)
        else:
            textarea.type(text, delay=S.TYPING_DELAY_MS)

        time.sleep(0.5)

        # Send
        send_btn = self._find_send_button()
        if send_btn:
            send_btn.click()
            print(f"[OK] Sent ({len(text)} chars)")
        else:
            print("[WARN] Send button not found, pressing Enter")
            page.keyboard.press("Enter")

        time.sleep(S.POST_SEND_DELAY)
        self._wait_for_response()

        response = self._extract_last_response()
        self._in_conversation = True
        return response

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

        print("[WARN] Response timeout -- may be incomplete.")
        return False

    def _extract_last_response(self) -> str:
        for sel in S.ASSISTANT_MESSAGE_SELECTORS:
            messages = self._page.query_selector_all(sel)
            if messages:
                last = messages[-1]
                return last.inner_text().strip()
        return "(no response found)"
