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

import re
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
        self._last_response_complete = True

    def __enter__(self):
        self._pw = sync_playwright().start()
        PROFILE_DIR.mkdir(exist_ok=True)
        self._ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=not self._headed,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        # Reuse existing tab to avoid double-tab issue
        if self._ctx.pages:
            self._page = self._ctx.pages[0]
        else:
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
        completed = self._wait_for_response()

        response = self._extract_last_response()
        self._in_conversation = True
        self._last_response_complete = completed
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
        last_text_len = 0
        stable_count = 0

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

                # Neither streaming nor complete indicators visible.
                # Check if content is still growing (ChatGPT may be
                # between states -- the stop button disappeared but
                # the regenerate button hasn't appeared yet).
                current_len = 0
                for sel in S.ASSISTANT_MESSAGE_SELECTORS:
                    msgs = self._page.query_selector_all(sel)
                    if msgs:
                        try:
                            current_len = len(msgs[-1].inner_text())
                        except Exception:
                            pass
                        break

                if current_len > 0 and current_len == last_text_len:
                    stable_count += 1
                else:
                    stable_count = 0
                last_text_len = current_len

                # Only consider it done if content has been stable for 5s
                # AND we have some content
                if stable_count >= 5 and current_len > 50:
                    print("[OK] Response appears complete (content stable).")
                    return True

            time.sleep(1)

        print("[WARN] Response timeout -- may be incomplete.")
        return False

    def _extract_last_response(self) -> str:
        """Extract the last assistant message with code fences intact.

        Strategy: find every <pre><code> block in the message, extract
        the code text with newlines from each one (using JS on the element
        directly), detect the language, and build the response by combining
        prose from inner_text() with properly fenced code blocks.
        """
        last = None
        for sel in S.ASSISTANT_MESSAGE_SELECTORS:
            messages = self._page.query_selector_all(sel)
            if messages:
                last = messages[-1]
                break

        if not last:
            return "(no response found)"

        # Extract structured code blocks from <pre><code> elements
        code_blocks = self._get_code_blocks_from_dom(last)

        if not code_blocks:
            # No <pre><code> found — return raw inner_text
            return last.inner_text().strip()

        # We have code blocks. Now build the full response:
        # Get the full text, then replace mangled code with fenced versions.
        full_text = last.inner_text().strip()
        return self._insert_fences(full_text, code_blocks)

    def _get_code_blocks_from_dom(self, message_el) -> list:
        """Extract code text (with newlines) from each <pre><code> in the message.

        Returns [{language: str, code: str}] where code has proper newlines.
        """
        results = []

        pres = message_el.query_selector_all("pre")
        for pre in pres:
            code_el = pre.query_selector("code")
            if not code_el:
                continue

            # --- Detect language ---
            lang = ""
            try:
                cls = code_el.get_attribute("class") or ""
                m = re.search(r"language-(\w+)", cls)
                if m:
                    lang = m.group(1).lower()
            except Exception:
                pass

            if not lang:
                try:
                    cls = pre.get_attribute("class") or ""
                    m = re.search(r"language-(\w+)", cls)
                    if m:
                        lang = m.group(1).lower()
                except Exception:
                    pass

            if not lang:
                try:
                    # ChatGPT shows language label in a header span above the code
                    header_span = pre.query_selector("div span")
                    if header_span:
                        t = header_span.inner_text().strip().lower()
                        known_langs = {
                            "python", "bash", "sh", "shell", "c", "cpp", "c++",
                            "javascript", "typescript", "rust", "java", "go",
                            "ruby", "powershell", "sql", "html", "css", "json",
                            "yaml", "makefile", "toml", "r", "matlab", "lua",
                        }
                        if t in known_langs:
                            lang = t
                except Exception:
                    pass

            # --- Extract code text with newlines ---
            code = self._get_code_text(code_el)

            if code and len(code.strip()) > 5:
                results.append({
                    "language": lang or "python",
                    "code": code.strip(),
                })

        return results

    def _get_code_text(self, code_el) -> str:
        """Get the text content of a <code> element with newlines preserved.

        Tries multiple strategies because ChatGPT's DOM varies.
        """
        # Strategy 1: JS that walks text nodes and respects line breaks
        try:
            text = code_el.evaluate("""
                el => {
                    // Collect text preserving newline characters
                    let lines = [];
                    let currentLine = '';

                    function collect(node) {
                        if (node.nodeType === Node.TEXT_NODE) {
                            let text = node.textContent;
                            let parts = text.split('\\n');
                            for (let i = 0; i < parts.length; i++) {
                                currentLine += parts[i];
                                if (i < parts.length - 1) {
                                    lines.push(currentLine);
                                    currentLine = '';
                                }
                            }
                            return;
                        }
                        if (node.nodeName === 'BR') {
                            lines.push(currentLine);
                            currentLine = '';
                            return;
                        }
                        // Skip buttons and non-content elements
                        if (node.nodeName === 'BUTTON') return;

                        for (const child of node.childNodes) {
                            collect(child);
                        }
                    }

                    collect(el);
                    if (currentLine) lines.push(currentLine);

                    let result = lines.join('\\n');

                    // If that produced a single long line, try innerText
                    if (result.length > 80 && result.indexOf('\\n') === -1) {
                        let alt = el.innerText;
                        if (alt && alt.indexOf('\\n') !== -1) {
                            return alt;
                        }
                    }

                    return result;
                }
            """)
            if text and len(text.strip()) > 5:
                return text
        except Exception:
            pass

        # Strategy 2: plain innerText
        try:
            text = code_el.inner_text()
            if text:
                return text
        except Exception:
            pass

        # Strategy 3: textContent (last resort, may lose newlines)
        try:
            return code_el.text_content() or ""
        except Exception:
            return ""

    def _insert_fences(self, full_text: str, code_blocks: list) -> str:
        """Replace mangled code in inner_text with properly fenced versions.

        inner_text() produces something like:
            "Here is the code:\npython\nCopy code\nimport math\ndef hello():\n..."
        or sometimes all code on one line:
            "Here is the code:\nPythonimport mathdef hello():    print('hi')"

        We find where each code block appears (possibly mangled) and replace
        it with a proper ```language ... ``` fenced block.
        """
        result = full_text

        for block in code_blocks:
            lang = block["language"]
            code = block["code"]
            fenced = f"\n```{lang}\n{code}\n```\n"

            # The inner_text version may have:
            # 1. "Python\nCopy code\n" prefix before the code
            # 2. "Copy code\n" prefix
            # 3. Language name glued to first line: "Pythonimport math..."
            # 4. Code with newlines intact
            # 5. Code as one long line (no newlines)

            replaced = False

            # Try finding "Language\nCopy code\n<first line of code>"
            first_line = code.split("\n")[0].strip()
            last_line = code.split("\n")[-1].strip()

            # Pattern: "Python\nCopy code\n" or "python\nCopy code\n"
            for prefix_pattern in [
                f"{lang}\nCopy code\n",
                f"{lang.capitalize()}\nCopy code\n",
                f"{lang.upper()}\nCopy code\n",
                "Copy code\n",
            ]:
                idx = result.find(prefix_pattern)
                if idx >= 0:
                    # Find end of code region: look for last line of code
                    search_start = idx + len(prefix_pattern)
                    end_idx = result.find(last_line, search_start)
                    if end_idx >= 0:
                        end_idx += len(last_line)
                        result = result[:idx] + fenced + result[end_idx:]
                        replaced = True
                        break

            if replaced:
                continue

            # Try finding code on one line (no newlines version)
            code_no_nl = code.replace("\n", "")
            # Check for language prefix glued on
            for prefix in [lang.capitalize(), lang, lang.upper(), ""]:
                search = prefix + code_no_nl[:80]
                idx = result.find(search)
                if idx >= 0:
                    # Find end — look for last ~40 chars of the mangled code
                    end_search = code_no_nl[-40:]
                    end_idx = result.find(end_search, idx)
                    if end_idx >= 0:
                        end_idx += len(end_search)
                    else:
                        end_idx = idx + len(search)
                    result = result[:idx] + fenced + result[end_idx:]
                    replaced = True
                    break

            if replaced:
                continue

            # Try finding first line of code directly
            if first_line and first_line in result:
                idx = result.find(first_line)
                end_idx = result.find(last_line, idx)
                if end_idx >= 0:
                    end_idx += len(last_line)
                    result = result[:idx] + fenced + result[end_idx:]
                    replaced = True

            if not replaced:
                # Last resort: append the fenced block
                result = result + "\n" + fenced

        return result
