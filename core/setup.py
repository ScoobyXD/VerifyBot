"""
setup.py -- First-time setup wizard for VerifyBot.

Runs automatically on the very first launch. Walks the user through:
    1. Installing Python dependencies (playwright, paramiko)
    2. Installing Chromium browser for Playwright
    3. Creating .env with Raspberry Pi SSH credentials (optional)
    4. Logging into ChatGPT via browser (cookies saved for future runs)
    5. Creating all required directories
    6. Writing .setup_complete marker

After setup, this never runs again unless .setup_complete is deleted.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETUP_MARKER = ROOT / ".setup_complete"
ENV_FILE = ROOT / ".env"
PROFILE_DIR = ROOT / ".browser_profile"

# Directories that get auto-created
AUTO_DIRS = ["programs", "outputs", "context", "raw_md"]


def is_first_run() -> bool:
    """Check if this is a first-time user."""
    return not SETUP_MARKER.exists()


def run_setup():
    """Interactive first-time setup wizard."""
    _banner()

    # Step 1: Python dependencies
    _step("1/5", "Installing Python packages")
    _install_packages()

    # Step 2: Playwright browser
    _step("2/5", "Installing Chromium browser for Playwright")
    _install_chromium()

    # Step 3: .env for Raspberry Pi
    _step("3/5", "Raspberry Pi SSH credentials")
    _setup_env()

    # Step 4: ChatGPT browser login
    _step("4/5", "ChatGPT browser login")
    _setup_browser_login()

    # Step 5: Create directories
    _step("5/5", "Creating project directories")
    _create_dirs()

    # Done -- write marker
    _write_marker()
    _done()


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def _banner():
    print()
    print("=" * 60)
    print("  VERIFYBOT -- FIRST TIME SETUP")
    print("=" * 60)
    print()
    print("  Looks like this is your first time running VerifyBot.")
    print("  This wizard will get everything set up for you.")
    print("  It only runs once.")
    print()
    input("  Press Enter to begin...")
    print()


def _step(number: str, title: str):
    print()
    print(f"  [{number}] {title}")
    print(f"  {'-' * 50}")


def _install_packages():
    packages = ["playwright", "paramiko"]
    for pkg in packages:
        print(f"  Installing {pkg}...")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", pkg],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            print(f"  [OK] {pkg} installed")
        else:
            # Check if already installed
            check = subprocess.run(
                [sys.executable, "-c", f"import {pkg}"],
                capture_output=True, text=True,
            )
            if check.returncode == 0:
                print(f"  [OK] {pkg} already installed")
            else:
                print(f"  [WARN] Failed to install {pkg}. You may need to run:")
                print(f"         pip install {pkg}")
                print(f"  Error: {result.stderr[:200]}")


def _install_chromium():
    print("  Installing Chromium via Playwright...")
    print("  (This may take a minute on first install)")
    result = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print("  [OK] Chromium installed")
    else:
        print("  [WARN] Chromium install may have failed.")
        print(f"  Try running: playwright install chromium")
        if result.stderr:
            print(f"  Error: {result.stderr[:200]}")


def _setup_env():
    if ENV_FILE.exists():
        print("  .env file already exists, skipping.")
        return

    print()
    print("  VerifyBot can deploy code to a Raspberry Pi over SSH.")
    print("  If you don't have a Pi, just skip this step.")
    print()
    choice = input("  Do you have a Raspberry Pi to connect to? (y/n): ").strip().lower()

    if choice != "y":
        print("  [SKIP] No Pi configured. You can add one later by creating .env")
        print("         with PI_USER, PI_HOST, and PI_PASSWORD.")
        # Write a placeholder .env so ssh_skill doesn't crash on import
        ENV_FILE.write_text(
            "# Raspberry Pi SSH credentials -- fill these in to enable Pi target\n"
            "# PI_USER=your_username\n"
            "# PI_HOST=your_hostname_or_ip\n"
            "# PI_PASSWORD=your_password\n",
            encoding="utf-8",
        )
        return

    print()
    pi_host = input("  Pi hostname or IP address: ").strip()
    pi_user = input("  Pi username: ").strip()
    pi_pass = input("  Pi password: ").strip()

    if not all([pi_host, pi_user, pi_pass]):
        print("  [WARN] Missing credentials. Writing placeholder .env")
        ENV_FILE.write_text(
            "# Raspberry Pi SSH credentials -- fill these in\n"
            "# PI_USER=\n# PI_HOST=\n# PI_PASSWORD=\n",
            encoding="utf-8",
        )
        return

    ENV_FILE.write_text(
        f"# Raspberry Pi SSH credentials -- DO NOT COMMIT\n"
        f"PI_USER={pi_user}\n"
        f"PI_HOST={pi_host}\n"
        f"PI_PASSWORD={pi_pass}\n",
        encoding="utf-8",
    )
    print(f"  [OK] .env created with Pi credentials")

    # Test connection
    print("  Testing SSH connection...")
    try:
        import paramiko
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(hostname=pi_host, username=pi_user, password=pi_pass, timeout=10)
        stdin, stdout, stderr = client.exec_command("hostname")
        hostname = stdout.read().decode().strip()
        client.close()
        print(f"  [OK] Connected to Pi: {hostname}")
    except Exception as e:
        print(f"  [WARN] Could not connect: {e}")
        print("  You can fix .env later and try again.")


def _setup_browser_login():
    print()
    print("  VerifyBot uses ChatGPT through a browser window.")
    print("  You need to log in once so your session is saved.")
    print()
    print("  A Chromium browser will open. Log into ChatGPT,")
    print("  then CLOSE the browser window to continue.")
    print()
    choice = input("  Ready to open browser? (y/n): ").strip().lower()

    if choice != "y":
        print("  [SKIP] You can log in later with: python main.py --login")
        return

    PROFILE_DIR.mkdir(exist_ok=True)
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=False,
                viewport={"width": 1280, "height": 900},
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = ctx.new_page()
            page.goto("https://chat.openai.com")
            print("  [WAITING] Log into ChatGPT, then close the browser...")
            try:
                page.wait_for_event("close", timeout=0)
            except Exception:
                pass
            ctx.close()
        print("  [OK] ChatGPT login saved!")
    except Exception as e:
        print(f"  [WARN] Browser login failed: {e}")
        print("  You can try again later with: python main.py --login")


def _create_dirs():
    for dirname in AUTO_DIRS:
        dirpath = ROOT / dirname
        dirpath.mkdir(exist_ok=True)
        print(f"  [OK] {dirname}/")
    print("  All directories ready.")


def _write_marker():
    from datetime import datetime
    SETUP_MARKER.write_text(
        f"Setup completed: {datetime.now().isoformat()}\n"
        f"Python: {sys.version}\n"
        f"Platform: {sys.platform}\n",
        encoding="utf-8",
    )


def _done():
    print()
    print("=" * 60)
    print("  SETUP COMPLETE!")
    print("=" * 60)
    print()
    print("  You're ready to use VerifyBot. Try:")
    print()
    print('    python main.py "write a hello world script"')
    print('    python main.py "make a random number generator" --target raspi')
    print()
    print("  For help: python main.py --help")
    print()


# ---------------------------------------------------------------------------
# CLI (can also be run standalone)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if is_first_run():
        run_setup()
    else:
        print("Setup already complete. Delete .setup_complete to run again.")
