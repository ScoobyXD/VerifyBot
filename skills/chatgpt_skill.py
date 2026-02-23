"""
chatgpt_skill.py -- Browser automation for ChatGPT.

Thin wrapper that re-exports ChatGPTSession and provides
raw_md saving / login mode.

For the actual browser logic, see core/session.py.
For DOM selectors, see core/selectors.py.

Usage:
    python -m skills.chatgpt_skill --login    # First-time manual login
"""

import argparse
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

from core import selectors as S
from core.session import ChatGPTSession  # noqa: re-export

PROFILE_DIR = Path(__file__).resolve().parent.parent / ".browser_profile"
RAW_MD_DIR = Path(__file__).resolve().parent.parent / "raw_md"


def save_response(prompt: str, response: str, attempt: int = None) -> Path:
    """Save a prompt/response pair as timestamped markdown."""
    RAW_MD_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = prompt[:40].replace(" ", "_").replace("/", "_")
    filename = f"{ts}_{slug}.md"
    filepath = RAW_MD_DIR / filename

    label = f" (Attempt {attempt})" if attempt else ""
    content = (
        f"# LLM Response{label}\n"
        f"**Timestamp**: {datetime.now().isoformat()}\n"
        f"**Prompt**: {prompt}\n\n---\n\n"
        f"## Response\n\n{response}\n"
    )
    filepath.write_text(content, encoding="utf-8")
    return filepath


def append_to_log(filepath: Path, section_title: str, content: str):
    """Append a section to an existing raw_md log file."""
    if not filepath or not filepath.exists():
        return
    ts = datetime.now().strftime("%H:%M:%S")
    section = f"\n\n---\n\n## {section_title}\n_[{ts}]_\n\n{content}\n"
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(section)


def run_login_mode():
    """Open browser for manual login. Cookies persist."""
    print("=" * 50)
    print("LOGIN MODE -- Log into ChatGPT, then close the browser.")
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


def main():
    parser = argparse.ArgumentParser(description="ChatGPT Browser Skill")
    parser.add_argument("--login", action="store_true", help="Open browser for manual login")
    args = parser.parse_args()
    if args.login:
        run_login_mode()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
