#!/usr/bin/env python3
"""
ui.py -- Workbench UI for VerifyBot Agent.

Launch with:
    python main.py          (auto-launches this)
    python core/ui.py       (direct)

Opens a browser with a workbench: each agent run gets a draggable,
resizable panel showing terminal output + ChatGPT browser side by side.

Dependencies (auto-installed on first run):
    pip install flask flask-socketio
"""

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Auto-install UI dependencies
# ---------------------------------------------------------------------------

def _ensure_deps():
    missing = []
    try:
        import flask
    except ImportError:
        missing.append("flask")
    try:
        import flask_socketio
    except ImportError:
        missing.append("flask-socketio")
    if missing:
        print(f"[UI] Installing: {', '.join(missing)}...")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", *missing],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        print("[UI] Dependencies installed.")

_ensure_deps()

from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit

# ---------------------------------------------------------------------------
# Project root (ui.py lives in core/, root is one level up)
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
PROGRAMS_DIR = ROOT / "programs"
OUTPUTS_DIR = ROOT / "outputs"
RAW_MD_DIR = ROOT / "raw_md"
CONTEXT_DIR = ROOT / "context"
UPLOADS_DIR = ROOT / "uploads"

sys.path.insert(0, str(ROOT))

# Force UTF-8 for all I/O on Windows (prevents 'charmap' codec errors
# from box-drawing characters and Unicode in pipeline output)
os.environ["PYTHONIOENCODING"] = "utf-8"
os.environ["PYTHONUTF8"] = "1"

# ---------------------------------------------------------------------------
# Flask + SocketIO
# ---------------------------------------------------------------------------

app = Flask(__name__, static_folder=None)
app.config["SECRET_KEY"] = "verifybot-" + str(os.getpid())
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ---------------------------------------------------------------------------
# Multi-agent output capture
# ---------------------------------------------------------------------------

class AgentWriter:
    """Captures stdout/stderr and routes to a specific agent panel via WS.

    Uses thread-local storage so concurrent agent threads don't clobber
    each other's agent_id / sid.

    On Windows, the original console may use cp1252 which can't handle
    Unicode box-drawing chars etc. We catch those encoding errors silently.
    """
    def __init__(self, original, stream_name="stdout"):
        self.original = original
        self.stream_name = stream_name
        self._local = threading.local()

    @property
    def agent_id(self):
        return getattr(self._local, 'agent_id', None)

    @agent_id.setter
    def agent_id(self, value):
        self._local.agent_id = value

    @property
    def sid(self):
        return getattr(self._local, 'sid', None)

    @sid.setter
    def sid(self, value):
        self._local.sid = value

    def write(self, text):
        if self.original:
            try:
                self.original.write(text)
                self.original.flush()
            except (UnicodeEncodeError, UnicodeDecodeError):
                # Windows cp1252 can't handle some chars — write sanitized
                try:
                    safe = text.encode(self.original.encoding or "utf-8", errors="replace").decode(self.original.encoding or "utf-8", errors="replace")
                    self.original.write(safe)
                    self.original.flush()
                except Exception:
                    pass  # give up on console, still send via WS
        if text.strip() and self.sid and self.agent_id:
            # Filter greenlet/playwright cross-thread noise server-side
            if any(kw in text for kw in (
                'greenlet.error', 'Cannot switch to a different thread',
                '_sync_base.py', 'g_self.switch()', 'Exception in callback SyncBase',
                'Handle SyncBase', 'asyncio\\events.py', 'asyncio/events.py',
                'self._context.run(self._callback', 'task.add_done_callback',
                'suspended active started main', '(otid=',
            )):
                return
            socketio.emit("agent_output", {
                "agent_id": self.agent_id,
                "stream": self.stream_name,
                "text": text,
            }, to=self.sid)

    def flush(self):
        if self.original:
            try:
                self.original.flush()
            except Exception:
                pass

_out = AgentWriter(sys.stdout, "stdout")
_err = AgentWriter(sys.stderr, "stderr")

# Track running agents: agent_id -> thread
_agents = {}
_agents_lock = threading.Lock()
_agent_counter = 0

# Track live ChatGPT sessions for follow-ups: agent_id -> session
_agent_sessions = {}
_agent_sessions_lock = threading.Lock()

# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return _get_html()

@app.route("/api/status")
def api_status():
    with _agents_lock:
        running = [aid for aid, t in _agents.items() if t.is_alive()]
    return jsonify({
        "running_agents": running,
        "setup_complete": (ROOT / "core" / ".setup_complete").exists(),
    })

@app.route("/api/history")
def api_history():
    runs = []
    if RAW_MD_DIR.exists():
        for f in sorted(RAW_MD_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if f.suffix == ".md":
                try:
                    content = f.read_text(encoding="utf-8", errors="replace")
                    prompt_match = re.search(r"\*\*Prompt\*\*:\s*(.+)", content)
                    prompt = prompt_match.group(1) if prompt_match else f.stem
                    runs.append({
                        "filename": f.name,
                        "prompt": prompt[:120],
                        "date": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                    })
                except Exception:
                    pass
    return jsonify(runs[:50])

@app.route("/api/programs")
def api_programs():
    files = []
    if PROGRAMS_DIR.exists():
        for f in sorted(PROGRAMS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if f.is_file():
                files.append({
                    "name": f.name,
                    "size": f.stat().st_size,
                    "modified": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                })
    return jsonify(files[:100])

@app.route("/api/outputs")
def api_outputs():
    files = []
    if OUTPUTS_DIR.exists():
        for f in sorted(OUTPUTS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if f.is_file():
                files.append({
                    "name": f.name,
                    "size": f.stat().st_size,
                    "modified": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                })
    return jsonify(files[:100])

@app.route("/api/uploads")
def api_uploads():
    files = []
    if UPLOADS_DIR.exists():
        for f in sorted(UPLOADS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if f.is_file():
                files.append({
                    "name": f.name,
                    "size": f.stat().st_size,
                    "modified": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                })
    return jsonify(files[:100])

@app.route("/api/file/<path:subdir>/<path:filename>")
def api_file(subdir, filename):
    dir_map = {"raw_md": RAW_MD_DIR, "programs": PROGRAMS_DIR,
               "outputs": OUTPUTS_DIR, "context": CONTEXT_DIR,
               "uploads": UPLOADS_DIR}
    base = dir_map.get(subdir)
    if not base:
        return jsonify({"error": "Invalid directory"}), 404
    candidate = base / filename
    if candidate.exists() and candidate.is_file():
        try:
            return jsonify({"content": candidate.read_text(encoding="utf-8", errors="replace")})
        except Exception:
            return jsonify({"content": "(binary file)"}), 200
    return jsonify({"error": "File not found"}), 404

@app.route("/api/clear/<path:folder>", methods=["POST"])
def api_clear(folder):
    """Clear all files in a folder (history, programs, outputs, or uploads)."""
    import shutil as _shutil
    dir_map = {"history": RAW_MD_DIR, "programs": PROGRAMS_DIR,
               "outputs": OUTPUTS_DIR, "uploads": UPLOADS_DIR}
    target = dir_map.get(folder)
    if not target or not target.exists():
        return jsonify({"error": "Invalid folder"}), 400
    count = 0
    for f in target.iterdir():
        if f.is_file():
            try:
                f.unlink()
                count += 1
            except Exception:
                pass
    return jsonify({"cleared": count})

@app.route("/api/agent_files/<agent_id>/<folder>")
def api_agent_files(agent_id, folder):
    """List files belonging to a specific agent by matching owned files or timestamp."""
    dir_map = {"history": RAW_MD_DIR, "programs": PROGRAMS_DIR, "outputs": OUTPUTS_DIR}
    target = dir_map.get(folder)
    if not target or not target.exists():
        return jsonify([])

    # Get agent session info
    with _agent_sessions_lock:
        si = _agent_sessions.get(agent_id, {})
    md_path = si.get("md_path")
    start_time = si.get("start_time")
    owned = si.get("owned_files", set())

    files = []

    if owned:
        # Use precise file ownership tracking (test streams)
        for f in sorted(target.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not f.is_file():
                continue
            if str(f.resolve()) not in owned:
                continue
            if folder == "history":
                try:
                    content = f.read_text(encoding="utf-8", errors="replace")
                    prompt_match = re.search(r"\*\*Prompt\*\*:\s*(.+)", content)
                    prompt_str = prompt_match.group(1) if prompt_match else f.stem
                    files.append({
                        "filename": f.name,
                        "prompt": prompt_str[:120],
                        "date": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                    })
                except Exception:
                    pass
            else:
                files.append({
                    "name": f.name,
                    "size": f.stat().st_size,
                    "modified": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                })
    elif folder == "history":
        if start_time:
            for f in sorted(target.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
                if f.is_file() and f.suffix == '.md' and f.stat().st_mtime >= start_time:
                    try:
                        content = f.read_text(encoding="utf-8", errors="replace")
                        prompt_match = re.search(r"\*\*Prompt\*\*:\s*(.+)", content)
                        prompt_str = prompt_match.group(1) if prompt_match else f.stem
                        files.append({
                            "filename": f.name,
                            "prompt": prompt_str[:120],
                            "date": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                        })
                    except Exception:
                        pass
        elif md_path and md_path.exists():
            try:
                content = md_path.read_text(encoding="utf-8", errors="replace")
                prompt_match = re.search(r"\*\*Prompt\*\*:\s*(.+)", content)
                prompt_str = prompt_match.group(1) if prompt_match else md_path.stem
                files.append({
                    "filename": md_path.name,
                    "prompt": prompt_str[:120],
                    "date": datetime.fromtimestamp(md_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                })
            except Exception:
                pass
    elif start_time and target.exists():
        for f in sorted(target.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if f.is_file() and f.stat().st_mtime >= start_time:
                files.append({
                    "name": f.name,
                    "size": f.stat().st_size,
                    "modified": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                })
    return jsonify(files[:50])

@app.route("/api/git_push", methods=["POST"])
def api_git_push():
    """Run git add, commit, push. Returns stdout/stderr for the agent to process."""
    data = request.get_json(silent=True) or {}
    message = data.get("message", "Auto-commit from VerifyBot")

    results = []
    # Step 1: git add -A
    try:
        r = subprocess.run(["git", "add", "-A"], capture_output=True, text=True,
                           timeout=15, cwd=str(ROOT))
        results.append({"cmd": "git add -A", "exit": r.returncode,
                        "stdout": r.stdout, "stderr": r.stderr})
    except Exception as e:
        results.append({"cmd": "git add -A", "exit": -1, "stdout": "", "stderr": str(e)})

    # Step 2: git commit
    try:
        r = subprocess.run(["git", "commit", "-m", message], capture_output=True,
                           text=True, timeout=15, cwd=str(ROOT))
        results.append({"cmd": f"git commit -m \"{message}\"", "exit": r.returncode,
                        "stdout": r.stdout, "stderr": r.stderr})
    except Exception as e:
        results.append({"cmd": "git commit", "exit": -1, "stdout": "", "stderr": str(e)})

    # Step 3: git push
    try:
        r = subprocess.run(["git", "push"], capture_output=True, text=True,
                           timeout=30, cwd=str(ROOT))
        results.append({"cmd": "git push", "exit": r.returncode,
                        "stdout": r.stdout, "stderr": r.stderr})
    except Exception as e:
        results.append({"cmd": "git push", "exit": -1, "stdout": "", "stderr": str(e)})

    all_ok = all(r["exit"] == 0 for r in results)
    # "nothing to commit" is still a success
    if not all_ok:
        for r in results:
            if r["exit"] != 0 and "nothing to commit" in (r["stdout"] + r["stderr"]):
                r["exit"] = 0
        all_ok = all(r["exit"] == 0 for r in results)

    return jsonify({"success": all_ok, "results": results})

@app.route("/api/upload", methods=["POST"])
def api_upload():
    UPLOADS_DIR.mkdir(exist_ok=True)
    uploaded, file_paths, previews = [], [], {}
    for key in request.files:
        f = request.files[key]
        if f.filename:
            safe = re.sub(r'[\\/:*?"<>|]+', '_', f.filename)
            dest = UPLOADS_DIR / safe
            f.save(str(dest))
            uploaded.append(safe)
            file_paths.append(str(dest.resolve()))
            text_exts = {".txt",".csv",".json",".py",".md",".yaml",".yml",
                         ".toml",".ini",".cfg",".log",".tsv",".xml",".html",
                         ".css",".js",".c",".cpp",".h",".sh",".sql",".r"}
            if dest.suffix.lower() in text_exts:
                try:
                    c = dest.read_text(encoding="utf-8", errors="replace")
                    previews[safe] = c[:5000] + ("\n...(truncated)" if len(c) > 5000 else "")
                except Exception:
                    pass
    return jsonify({"uploaded": uploaded, "file_paths": file_paths, "previews": previews})

# ---------------------------------------------------------------------------
# WebSocket: agent lifecycle
# ---------------------------------------------------------------------------

@socketio.on("connect")
def on_connect():
    pass

@socketio.on("run_agent")
def on_run_agent(data):
    global _agent_counter
    prompt = data.get("prompt", "").strip()
    if not prompt:
        emit("agent_error", {"error": "Empty prompt."})
        return

    with _agents_lock:
        _agent_counter += 1
        agent_id = f"agent-{_agent_counter}"

    target = data.get("target")
    max_retries = int(data.get("max_retries", 3))
    timeout = int(data.get("timeout", 30))
    headless = data.get("headless", False)
    file_paths = data.get("file_paths", [])
    attachments = data.get("attachments", [])

    if attachments:
        prompt += " (Attached files: " + ", ".join(attachments) + ")"

    sid = request.sid

    # Create a work queue for this agent. The thread that owns the Playwright
    # session will consume tasks from it (follow-ups, close).
    agent_queue = queue.Queue()

    def run():
        """Agent thread: runs the pipeline, then waits for follow-up tasks.

        This thread owns the Playwright session for its entire lifetime.
        Follow-ups and close commands are sent via agent_queue so they
        execute on THIS thread, avoiding Playwright's thread-affinity error.
        """
        _out.agent_id = agent_id
        _out.sid = sid
        _err.agent_id = agent_id
        _err.sid = sid

        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = _out
        sys.stderr = _err

        session = None
        md_path = None
        agent_start_time = time.time()
        try:
            from main import run_pipeline
            from core.session import ChatGPTSession
            from skills.chatgpt_skill import append_to_log
            import base64 as _b64

            # --- Create session on THIS thread so Playwright is happy ---
            # Each agent gets its own browser profile to avoid lock conflicts.
            # We copy essential files from the main profile so ChatGPT login
            # persists across all instances.
            import shutil as _shutil
            main_profile = ROOT / ".browser_profile"
            agent_profile = ROOT / ".browser_profiles" / agent_id
            agent_profile.mkdir(parents=True, exist_ok=True)

            # Copy login/cookie files from main profile if they exist
            if main_profile.exists():
                for fname in ["Cookies", "Cookies-journal",
                              "Login Data", "Login Data-journal",
                              "Local State", "Preferences", "Secure Preferences"]:
                    src = main_profile / fname
                    if src.exists():
                        try:
                            _shutil.copy2(str(src), str(agent_profile / fname))
                        except Exception:
                            pass
                # Also copy Default directory (contains cookies in some Chromium versions)
                src_default = main_profile / "Default"
                dst_default = agent_profile / "Default"
                if src_default.exists() and not dst_default.exists():
                    try:
                        _shutil.copytree(str(src_default), str(dst_default),
                                        ignore=_shutil.ignore_patterns(
                                            "Cache", "Code Cache", "GPUCache",
                                            "Service Worker", "*.log", "*.tmp"))
                    except Exception:
                        pass

            session = ChatGPTSession(headed=not headless, profile_dir=agent_profile)
            session.__enter__()

            # --- Auto-scroll + screenshot helpers (closures over agent_id/sid) ---
            def _scroll_to_bottom(sess):
                try:
                    if sess._page and not sess._page.is_closed():
                        sess._page.evaluate("""
                            () => {
                                const containers = document.querySelectorAll('[class*="react-scroll-to-bottom"]');
                                for (const c of containers) c.scrollTop = c.scrollHeight;
                                const main = document.querySelector('main');
                                if (main) main.scrollTop = main.scrollHeight;
                                const conv = document.querySelector('[class*="conversation-turn"]:last-child');
                                if (conv) conv.scrollIntoView({ block: 'end' });
                            }
                        """)
                except Exception:
                    pass

            def _take_screenshot(sess):
                try:
                    if sess._page and not sess._page.is_closed():
                        _scroll_to_bottom(sess)
                        png = sess._page.screenshot(type="png", timeout=3000)
                        if png:
                            socketio.emit("agent_screenshot", {
                                "agent_id": agent_id,
                                "image": _b64.b64encode(png).decode("ascii"),
                            }, to=sid)
                except Exception:
                    pass

            # --- Monkey-patch _wait_for_response for live screenshots ---
            _orig_wait = ChatGPTSession._wait_for_response

            def _patched_wait(self_sess, timeout_val=None):
                import time as _time
                from core import chatgpt_selectors as _S
                _timeout = timeout_val if timeout_val is not None else _S.RESPONSE_TIMEOUT
                print("[...] Waiting for response...")
                deadline = _time.time() + _timeout
                last_text_len = 0
                stable_count = 0

                while _time.time() < deadline:
                    still_streaming = False
                    for sel in _S.STOP_GENERATING_SELECTORS:
                        stop_btn = self_sess._page.query_selector(sel)
                        if stop_btn and stop_btn.is_visible():
                            still_streaming = True
                            break

                    if not still_streaming:
                        for sel in _S.RESPONSE_COMPLETE_INDICATORS:
                            indicator = self_sess._page.query_selector(sel)
                            if indicator and indicator.is_visible():
                                _take_screenshot(self_sess)
                                print("[OK] Response complete.")
                                return True

                        current_len = 0
                        for sel in _S.ASSISTANT_MESSAGE_SELECTORS:
                            msgs = self_sess._page.query_selector_all(sel)
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

                        if stable_count >= 5 and current_len > 50:
                            _take_screenshot(self_sess)
                            print("[OK] Response appears complete (content stable).")
                            return True

                    _take_screenshot(self_sess)
                    _time.sleep(1)

                _take_screenshot(self_sess)
                print("[WARN] Response timeout -- may be incomplete.")
                return False

            _orig_nav = ChatGPTSession._navigate_to_new_chat
            def _patched_nav(self_sess):
                _orig_nav(self_sess)
                _take_screenshot(self_sess)

            ChatGPTSession._wait_for_response = _patched_wait
            ChatGPTSession._navigate_to_new_chat = _patched_nav

            # ---- Phase 1: Run the pipeline ----
            try:
                result = run_pipeline(
                    prompt=prompt,
                    target=target if target != "auto" else None,
                    max_retries=max_retries,
                    timeout=timeout,
                    headed=not headless,
                    attachments=file_paths if file_paths else None,
                    session=session,
                )
            except Exception as e:
                socketio.emit("agent_error", {"agent_id": agent_id, "error": str(e)}, to=sid)
                result = False

            # Recover the raw_md path from the pipeline's log directory
            try:
                _raw_md_dir = Path(__file__).resolve().parent.parent / "raw_md"
                if _raw_md_dir.exists():
                    md_files = sorted(_raw_md_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
                    if md_files:
                        md_path = md_files[0]  # most recent log
            except Exception:
                pass

            # Store session info (with queue, md_path, and target) for follow-ups
            with _agent_sessions_lock:
                _agent_sessions[agent_id] = {
                    "session": session,
                    "sid": sid,
                    "queue": agent_queue,
                    "md_path": md_path,
                    "target": target if target != "auto" else None,
                    "timeout": timeout,
                    "max_retries": max_retries,
                    "start_time": agent_start_time,
                }

            socketio.emit("agent_done", {"agent_id": agent_id, "success": result}, to=sid)

            # ---- Phase 2: Wait for follow-up tasks on the queue ----
            # This keeps the thread (and Playwright session) alive.
            while True:
                try:
                    task = agent_queue.get(timeout=1)
                except queue.Empty:
                    # Check if we've been removed from sessions (panel closed)
                    with _agent_sessions_lock:
                        if agent_id not in _agent_sessions:
                            break
                    continue

                if task is None:
                    # Poison pill: close signal
                    break

                task_type = task.get("type")

                if task_type == "followup":
                    fu_prompt = task["prompt"]
                    fu_files = task.get("file_paths", [])

                    # Re-activate output routing
                    _out.agent_id = agent_id
                    _out.sid = sid
                    _err.agent_id = agent_id
                    _err.sid = sid
                    sys.stdout = _out
                    sys.stderr = _err

                    socketio.emit("agent_running", {"agent_id": agent_id}, to=sid)

                    try:
                        from main import run_followup_pipeline

                        # Resolve target from initial pipeline config
                        fu_target = None
                        fu_timeout = timeout
                        fu_retries = max_retries
                        with _agent_sessions_lock:
                            si = _agent_sessions.get(agent_id)
                            if si:
                                fu_target = si.get("target")
                                fu_timeout = si.get("timeout", timeout)
                                fu_retries = si.get("max_retries", max_retries)

                        fu_result = run_followup_pipeline(
                            session=session,
                            followup_prompt=fu_prompt,
                            md_path=md_path,
                            target=fu_target,
                            max_retries=fu_retries,
                            timeout=fu_timeout,
                            file_paths=fu_files if fu_files else None,
                        )

                        socketio.emit("agent_done", {
                            "agent_id": agent_id,
                            "success": fu_result,
                        }, to=sid)
                    except Exception as e:
                        print(f"[ERROR] Follow-up pipeline failed: {e}")
                        import traceback
                        traceback.print_exc()
                        socketio.emit("agent_error", {"agent_id": agent_id, "error": str(e)}, to=sid)
                    finally:
                        sys.stdout = old_out
                        sys.stderr = old_err
                        _out.agent_id = None
                        _err.agent_id = None

            # ---- Phase 3: Cleanup (thread exiting) ----
            ChatGPTSession._wait_for_response = _orig_wait
            ChatGPTSession._navigate_to_new_chat = _orig_nav

        except Exception as e:
            socketio.emit("agent_error", {"agent_id": agent_id, "error": str(e)}, to=sid)
        finally:
            # Close the browser session -- force-close all pages first
            if session:
                try:
                    if session._ctx:
                        for pg in session._ctx.pages:
                            try:
                                pg.close()
                            except Exception:
                                pass
                    session.__exit__(None, None, None)
                except Exception:
                    pass
            # Clean up the cloned browser profile
            agent_profile = ROOT / ".browser_profiles" / agent_id
            if agent_profile.exists():
                try:
                    import shutil as _shutil2
                    _shutil2.rmtree(str(agent_profile), ignore_errors=True)
                    print(f"  [CLEANUP] Removed .browser_profiles/{agent_id}/")
                except Exception:
                    pass
            with _agent_sessions_lock:
                _agent_sessions.pop(agent_id, None)
            sys.stdout = old_out
            sys.stderr = old_err
            _out.agent_id = None
            _err.agent_id = None
            with _agents_lock:
                _agents.pop(agent_id, None)

    emit("agent_created", {"agent_id": agent_id, "prompt": prompt})
    t = threading.Thread(target=run, daemon=True, name=agent_id)
    with _agents_lock:
        _agents[agent_id] = t
    t.start()

@socketio.on("followup_agent")
def on_followup_agent(data):
    """Push a follow-up task onto the agent's queue (runs on its own thread)."""
    agent_id = data.get("agent_id", "")
    prompt = data.get("prompt", "").strip()
    file_paths = data.get("file_paths", [])
    if not prompt:
        return

    sid = request.sid

    with _agent_sessions_lock:
        session_info = _agent_sessions.get(agent_id)

    if not session_info:
        socketio.emit("agent_output", {
            "agent_id": agent_id,
            "stream": "stderr",
            "text": "[WARN] Session no longer available for follow-up.\n",
        }, to=sid)
        return

    # Push onto the agent's work queue -- the Playwright-owning thread picks it up
    session_info["queue"].put({
        "type": "followup",
        "prompt": prompt,
        "file_paths": file_paths,
    })

@socketio.on("close_agent")
def on_close_agent(data):
    """Send poison pill to the agent thread so it closes the session cleanly."""
    agent_id = data.get("agent_id", "")
    with _agent_sessions_lock:
        session_info = _agent_sessions.get(agent_id)
    if session_info:
        # Poison pill tells the thread to exit its wait loop
        session_info["queue"].put(None)

@socketio.on("run_tests")
def on_run_tests(data):
    """Run tests using the agent panel system.

    Each test stream gets its own agent panel with live terminal output
    and browser screenshots, just like a regular agent run. This avoids
    the orphaned browser windows and missing terminal output issues.
    """
    global _agent_counter

    sid = request.sid

    def run_test_streams():
        global _agent_counter
        # Create a coordinator panel to show any top-level test errors
        socketio.emit("agent_created", {
            "agent_id": "test-init",
            "prompt": "Test Runner",
            "is_test": True,
        }, to=sid)

        try:
            # Import test definitions
            sys.path.insert(0, str(ROOT))

            # Check that required files exist before importing
            tests_file = ROOT / "tests.py"
            main_file = ROOT / "main.py"
            if not tests_file.exists():
                socketio.emit("agent_error", {
                    "agent_id": "test-init",
                    "error": "tests.py not found in project root. Cannot run tests.",
                }, to=sid)
                return
            if not main_file.exists():
                socketio.emit("agent_error", {
                    "agent_id": "test-init",
                    "error": "main.py not found in project root. Cannot run tests.",
                }, to=sid)
                return

            try:
                from tests import TESTS, clone_profile, cleanup_cloned_profiles
                from tests import init_test_md, write_test_result, write_summary, append_test_md
                from main import run_pipeline
                from core.session import ChatGPTSession
            except ImportError as ie:
                socketio.emit("agent_error", {
                    "agent_id": "test-init",
                    "error": f"Import error: {ie}. Make sure tests.py and main.py are in the project root.",
                }, to=sid)
                return

            init_test_md()

            # Group tests by stream
            streams = {}
            for i, test in enumerate(TESTS, 1):
                s = test.get("stream", str(i))
                streams.setdefault(s, []).append((i, test))

            results = []
            results_lock = threading.Lock()
            passed_tests = set()
            passed_lock = threading.Lock()
            stream_threads = []
            stream_ids = list(streams.keys())
            test_suite_start = time.time()

            for stream_id, stream_tests in streams.items():
                # Create an agent panel for this stream
                with _agents_lock:
                    _agent_counter += 1
                    agent_id = f"test-{stream_id}-{_agent_counter}"

                test_names = ", ".join(t["name"] for _, t in stream_tests)
                socketio.emit("agent_created", {
                    "agent_id": agent_id,
                    "prompt": f"Tests Stream {stream_id}: {test_names}",
                    "is_test": True,
                }, to=sid)

                def run_stream_agent(agent_id=agent_id, stream_id=stream_id,
                                     stream_tests=stream_tests):
                    """Run a test stream inside an agent panel."""
                    _out.agent_id = agent_id
                    _out.sid = sid
                    _err.agent_id = agent_id
                    _err.sid = sid
                    old_out, old_err = sys.stdout, sys.stderr
                    sys.stdout = _out
                    sys.stderr = _err

                    session = None
                    agent_start_time = time.time()

                    try:
                        # Clone browser profile for this stream
                        profile_dir = clone_profile(stream_id)
                        headed = True

                        # Create session
                        session = ChatGPTSession(headed=headed, profile_dir=profile_dir)
                        session.__enter__()

                        agent_queue = queue.Queue()
                        with _agent_sessions_lock:
                            _agent_sessions[agent_id] = {
                                "session": session,
                                "sid": sid,
                                "queue": agent_queue,
                                "md_path": None,
                                "target": "local",
                                "timeout": 30,
                                "max_retries": 3,
                                "start_time": agent_start_time,
                            }

                        # Monkeypatch screenshots
                        _orig_wait = ChatGPTSession._wait_for_response
                        def _patched_wait(self, *args, **kwargs):
                            resp = _orig_wait(self, *args, **kwargs)
                            try:
                                img = session.screenshot()
                                if img:
                                    import base64
                                    socketio.emit("agent_screenshot", {
                                        "agent_id": agent_id,
                                        "image": base64.b64encode(img).decode(),
                                    }, to=sid)
                            except Exception:
                                pass
                            return resp
                        ChatGPTSession._wait_for_response = _patched_wait

                        # Run each test in this stream
                        all_passed = True
                        for num, test in stream_tests:
                            target = test.get("target", "local")
                            prompt = test["prompt"]

                            # Check dependency
                            dep = test.get("depends_on")
                            if dep is not None:
                                with passed_lock:
                                    dep_passed = dep in passed_tests
                                if not dep_passed:
                                    print(f"\n[SKIP] Test {num}: {test['name']} (depends on #{dep})")
                                    with results_lock:
                                        results.append({
                                            "num": num, "name": test["name"],
                                            "stream": stream_id,
                                            "passed": False, "skipped": True, "elapsed": 0,
                                        })
                                    write_test_result(num, test, False, 0, skipped=True)
                                    continue

                            print(f"\n{'='*50}")
                            print(f"[TEST {num}] {test['name']}")
                            print(f"{'='*50}")

                            test_start = time.time()
                            try:
                                passed = run_pipeline(
                                    prompt=prompt,
                                    target=target,
                                    max_retries=3,
                                    timeout=30,
                                    headed=headed,
                                    session=session,
                                )
                            except Exception as e:
                                print(f"\n[CRASH] Test {num}: {e}")
                                passed = False

                            elapsed = time.time() - test_start
                            if passed:
                                with passed_lock:
                                    passed_tests.add(num)
                            else:
                                all_passed = False

                            # Track files created during this test for file buttons
                            try:
                                for d_ in [RAW_MD_DIR, PROGRAMS_DIR, OUTPUTS_DIR]:
                                    if d_.exists():
                                        for f_ in d_.iterdir():
                                            if f_.is_file() and f_.stat().st_mtime >= test_start:
                                                with _agent_sessions_lock:
                                                    si_ = _agent_sessions.get(agent_id)
                                                    if si_:
                                                        si_.setdefault("owned_files", set()).add(str(f_.resolve()))
                            except Exception:
                                pass

                            status = "PASS" if passed else "FAIL"
                            print(f"\n[{status}] Test {num}: {test['name']} ({elapsed:.1f}s)")

                            with results_lock:
                                results.append({
                                    "num": num, "name": test["name"],
                                    "stream": stream_id,
                                    "passed": passed, "elapsed": elapsed,
                                })
                            write_test_result(num, test, passed, elapsed)

                        socketio.emit("agent_done", {
                            "agent_id": agent_id,
                            "success": all_passed,
                        }, to=sid)

                        ChatGPTSession._wait_for_response = _orig_wait

                    except Exception as e:
                        socketio.emit("agent_error", {
                            "agent_id": agent_id, "error": str(e),
                        }, to=sid)
                    finally:
                        if session:
                            try:
                                # Force-close all pages to prevent orphaned chrome tabs
                                if session._ctx:
                                    for pg in session._ctx.pages:
                                        try:
                                            pg.close()
                                        except Exception:
                                            pass
                                session.__exit__(None, None, None)
                            except Exception:
                                pass
                        # Clean up the cloned profile for this stream
                        if profile_dir and profile_dir.exists():
                            try:
                                import shutil as _shutil_stream
                                _shutil_stream.rmtree(str(profile_dir), ignore_errors=True)
                            except Exception:
                                pass
                        with _agent_sessions_lock:
                            _agent_sessions.pop(agent_id, None)
                        sys.stdout = old_out
                        sys.stderr = old_err
                        _out.agent_id = None
                        _err.agent_id = None
                        with _agents_lock:
                            _agents.pop(agent_id, None)

                t = threading.Thread(target=run_stream_agent, daemon=True, name=agent_id)
                with _agents_lock:
                    _agents[agent_id] = t
                stream_threads.append(t)
                t.start()

            # Wait for all streams to finish
            for t in stream_threads:
                t.join()

            # Cleanup cloned profiles
            cleanup_cloned_profiles(stream_ids)

            # Also clean any .browser_profiles/ dirs (retry with delay if locked)
            profiles_dir = ROOT / ".browser_profiles"
            if profiles_dir.exists():
                import shutil as _shutil3
                time.sleep(1)  # brief delay for Chromium file locks to release
                _shutil3.rmtree(str(profiles_dir), ignore_errors=True)

            # Clean any leftover .browser_profile_* dirs from tests.py clone_profile
            for p in ROOT.iterdir():
                if p.is_dir() and p.name.startswith(".browser_profile_"):
                    try:
                        import shutil as _shutil4
                        _shutil4.rmtree(str(p), ignore_errors=True)
                    except Exception:
                        pass

            # Write summary
            total = len(results)
            passed_ct = sum(1 for r in results if r.get("passed"))
            failed_ct = total - passed_ct

            summary_text = f"\n{'='*50}\nTEST SUMMARY: {passed_ct}/{total} passed, {failed_ct} failed\n{'='*50}\n"
            socketio.emit("agent_output", {
                "agent_id": "test-init",
                "stream": "stdout",
                "text": summary_text,
            }, to=sid)
            print(summary_text, file=sys.__stdout__)

            try:
                total_time = time.time() - test_suite_start
                write_summary(results, total_time, len(streams))
            except TypeError:
                # Fallback if write_summary signature changed
                try:
                    write_summary(results)
                except Exception:
                    pass

            # Mark the coordinator panel as done
            socketio.emit("agent_done", {
                "agent_id": "test-init",
                "success": failed_ct == 0,
            }, to=sid)

        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            socketio.emit("agent_error", {
                "agent_id": "test-init",
                "error": f"Test runner error: {e}\n{tb}",
            }, to=sid)
            print(f"[TEST RUNNER ERROR] {e}\n{tb}", file=sys.__stderr__)

    threading.Thread(target=run_test_streams, daemon=True, name="test-coordinator").start()

# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

def _get_html():
    return r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>VerifyBot Workbench</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.5/socket.io.min.js"></script>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&family=DM+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#0c0c12;--bg2:#13131d;--bg3:#1b1b2a;--bg4:#222236;
  --brd:#2a2a40;--brd2:#3a3a55;
  --tx:#e0e0ee;--tx2:#9090a8;--tx3:#606078;
  --acc:#6c6cff;--acc2:#5252d4;--accg:rgba(108,108,255,.12);
  --ok:#3ddc84;--err:#ff5555;--warn:#ffb347;
  --toolbar-h:44px;
}
html,body{height:100%;overflow:hidden;font-family:'DM Sans',sans-serif;background:var(--bg);color:var(--tx)}
.app{display:flex;flex-direction:column;height:100vh}

/* === TOOLBAR === */
.toolbar{
  height:var(--toolbar-h);background:var(--bg2);border-bottom:1px solid var(--brd);
  display:flex;align-items:center;padding:0 12px;gap:8px;flex-shrink:0;z-index:100;
}
.toolbar .logo{
  width:26px;height:26px;background:linear-gradient(135deg,var(--acc),#9b6cff);
  border-radius:7px;display:flex;align-items:center;justify-content:center;
  font-weight:700;font-size:12px;color:#fff;flex-shrink:0;
}
.toolbar .title{font-size:14px;font-weight:600;margin-right:12px;letter-spacing:-.3px}
.toolbar .sep{width:1px;height:20px;background:var(--brd);margin:0 4px}

.tb-btn{
  padding:5px 12px;background:var(--bg3);border:1px solid var(--brd);border-radius:7px;
  color:var(--tx2);font-family:inherit;font-size:12px;font-weight:500;cursor:pointer;
  display:flex;align-items:center;gap:5px;transition:all .12s;position:relative;
}
.tb-btn:hover{background:var(--bg4);color:var(--tx);border-color:var(--brd2)}
.tb-btn.accent{background:var(--acc2);border-color:var(--acc);color:#fff}
.tb-btn.accent:hover{background:var(--acc)}

.tb-select{
  padding:4px 8px;background:var(--bg3);border:1px solid var(--brd);border-radius:6px;
  color:var(--tx);font-family:inherit;font-size:12px;outline:none;
}
.tb-select option{background:var(--bg2)}
.tb-input{
  width:48px;padding:4px 6px;background:var(--bg3);border:1px solid var(--brd);
  border-radius:6px;color:var(--tx);font-family:inherit;font-size:12px;
  outline:none;text-align:center;
}
.tb-label{font-size:11px;color:var(--tx3);font-weight:500}

.status-dot{width:7px;height:7px;border-radius:50%;background:var(--ok);flex-shrink:0}
.status-dot.busy{background:var(--acc);animation:pulse 1.5s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}
.status-text{font-size:12px;color:var(--tx2)}

.toolbar .spacer{flex:1}

/* === DROPDOWN PANELS === */
.dropdown-wrap{position:relative}
.dropdown-panel{
  display:none;position:absolute;top:calc(100% + 6px);left:0;
  width:400px;max-height:500px;background:var(--bg2);border:1px solid var(--brd);
  border-radius:10px;box-shadow:0 12px 40px rgba(0,0,0,.5);overflow:hidden;z-index:200;
}
.dropdown-panel.open{display:flex;flex-direction:column}
.dropdown-header{
  padding:10px 14px;border-bottom:1px solid var(--brd);font-size:13px;font-weight:600;
  display:flex;align-items:center;justify-content:space-between;gap:8px;
}
.clear-btn{
  background:none;border:1px solid var(--err);color:var(--err);font-size:9px;
  padding:2px 8px;border-radius:4px;cursor:pointer;font-weight:400;
}
.clear-btn:hover{background:var(--err);color:#fff}
.dropdown-list{flex:1;overflow-y:auto;padding:4px}
.dropdown-item{
  padding:8px 12px;border-radius:7px;cursor:pointer;font-size:12px;
  color:var(--tx2);transition:all .1s;margin-bottom:1px;
}
.dropdown-item:hover{background:var(--bg3);color:var(--tx)}
.dropdown-item .meta{font-size:10px;color:var(--tx3);margin-top:2px}

/* Panel mini file buttons (removed — now in header as icons) */

/* === FILE VIEWER MODAL === */
.modal-overlay{
  display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:300;
  align-items:center;justify-content:center;
}
.modal-overlay.open{display:flex}
.modal{
  width:70vw;max-width:900px;max-height:80vh;background:var(--bg2);
  border:1px solid var(--brd);border-radius:12px;overflow:hidden;display:flex;flex-direction:column;
}
.modal-header{
  padding:12px 16px;border-bottom:1px solid var(--brd);display:flex;align-items:center;
  font-size:14px;font-weight:600;gap:8px;
}
.modal-header .close{
  margin-left:auto;cursor:pointer;color:var(--tx3);font-size:18px;padding:2px 6px;border-radius:4px;
}
.modal-header .close:hover{background:var(--bg3);color:var(--tx)}
.modal-body{
  flex:1;overflow:auto;padding:16px;
  font-family:'JetBrains Mono',monospace;font-size:12px;line-height:1.6;
  white-space:pre-wrap;word-break:break-all;color:var(--tx2);
}

/* === WORKBENCH === */
.workbench{
  flex:1;overflow:hidden;position:relative;cursor:grab;
}
.workbench.panning{cursor:grabbing}
.canvas{
  position:absolute;width:8000px;height:8000px;
  transform-origin:0 0;
}

.welcome{
  width:100%;display:flex;flex-direction:column;align-items:center;
  justify-content:center;padding:80px 20px;opacity:.5;
}
.welcome .icon{
  width:56px;height:56px;background:linear-gradient(135deg,var(--acc),#9b6cff);
  border-radius:16px;display:flex;align-items:center;justify-content:center;
  font-size:24px;color:#fff;font-weight:700;margin-bottom:14px;
}
.welcome h2{font-size:20px;font-weight:600;margin-bottom:6px}
.welcome p{font-size:13px;color:var(--tx3);max-width:400px;text-align:center;line-height:1.5}
.examples{display:flex;gap:6px;flex-wrap:wrap;justify-content:center;margin-top:12px}
.example-chip{
  padding:6px 12px;background:var(--bg3);border:1px solid var(--brd);border-radius:16px;
  font-size:11px;color:var(--tx2);cursor:pointer;transition:all .12s;
}
.example-chip:hover{border-color:var(--acc2);color:var(--tx);background:var(--accg)}

/* === AGENT PANEL === */
.agent-panel{
  background:var(--bg2);border:1px solid var(--brd);border-radius:10px;
  overflow:hidden;display:flex;flex-direction:column;
  min-width:420px;min-height:48px;
  width:680px;height:420px;
  position:absolute;
}
.agent-panel.maximized{
  position:fixed!important;inset:var(--toolbar-h) 0 0 0!important;
  width:auto!important;height:auto!important;z-index:50;border-radius:0;
}
.agent-panel.minimized .agent-body,
.agent-panel.minimized .agent-status,
.agent-panel.minimized .panel-input-bar{display:none}
.agent-panel.minimized{height:34px!important;min-height:34px!important;overflow:visible;min-width:160px}
.agent-panel.minimized .resize-handle.rh-n,
.agent-panel.minimized .resize-handle.rh-s{display:none}
.agent-panel.minimized .resize-handle.rh-ne,
.agent-panel.minimized .resize-handle.rh-nw,
.agent-panel.minimized .resize-handle.rh-se,
.agent-panel.minimized .resize-handle.rh-sw{cursor:ew-resize}

/* Resize handles on all edges and corners */
.resize-handle{position:absolute;z-index:5}
.resize-handle.rh-n{top:-3px;left:8px;right:8px;height:6px;cursor:ns-resize}
.resize-handle.rh-s{bottom:-3px;left:8px;right:8px;height:6px;cursor:ns-resize}
.resize-handle.rh-e{right:-3px;top:8px;bottom:8px;width:6px;cursor:ew-resize}
.resize-handle.rh-w{left:-3px;top:8px;bottom:8px;width:6px;cursor:ew-resize}
.resize-handle.rh-ne{top:-3px;right:-3px;width:10px;height:10px;cursor:nesw-resize}
.resize-handle.rh-nw{top:-3px;left:-3px;width:10px;height:10px;cursor:nwse-resize}
.resize-handle.rh-se{bottom:-3px;right:-3px;width:10px;height:10px;cursor:nwse-resize}
.resize-handle.rh-sw{bottom:-3px;left:-3px;width:10px;height:10px;cursor:nesw-resize}

.agent-head{
  height:34px;background:var(--bg3);border-bottom:1px solid var(--brd);
  display:flex;align-items:center;padding:0 10px;gap:6px;flex-shrink:0;
  cursor:grab;user-select:none;
}
.agent-head:active{cursor:grabbing}
.agent-head .dot{width:6px;height:6px;border-radius:50%}
.agent-head .dot.run{background:var(--acc);animation:pulse 1.5s ease-in-out infinite}
.agent-head .dot.pass{background:var(--ok)}
.agent-head .dot.fail{background:var(--err)}
.agent-head .label{font-size:11px;font-weight:600;color:var(--tx2);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.agent-head .head-btn{
  background:none;border:none;color:var(--tx3);cursor:pointer;font-size:14px;
  padding:2px 5px;border-radius:4px;display:flex;align-items:center;
}
.agent-head .head-btn:hover{background:var(--bg);color:var(--tx)}
.agent-head .file-btn{opacity:.6;padding:2px 3px}
.agent-head .file-btn:hover{opacity:1;background:var(--bg3)}
.agent-head .file-btn.fb-history{color:#f59e0b}
.agent-head .file-btn.fb-programs{color:#8b5cf6}
.agent-head .file-btn.fb-outputs{color:#22d3ee}
.agent-head .head-sep{width:1px;height:14px;background:var(--brd);margin:0 2px;flex-shrink:0}

.agent-body{flex:1;display:flex;overflow:hidden}

.agent-terminal{
  flex:1;overflow-y:auto;padding:10px 12px;
  font-family:'JetBrains Mono',monospace;font-size:11.5px;line-height:1.6;
  white-space:pre-wrap;word-break:break-word;color:#c0c0d4;
  border-right:1px solid var(--brd);min-width:0;
}
.agent-terminal .line{display:block;padding:1px 0}
.agent-terminal .stderr{color:var(--err)}
.agent-terminal .step{color:var(--acc);font-weight:600;display:block;margin-top:8px}
.agent-terminal .ok{color:var(--ok)}
.agent-terminal .warn{color:var(--warn)}
.agent-terminal .err-text{color:var(--err)}
.agent-terminal .sep{color:var(--tx3);opacity:.5;display:block;margin:2px 0}
.agent-terminal .dim{color:var(--tx3)}
.agent-terminal .bold{font-weight:600;color:var(--tx)}
.agent-terminal .code-line{color:#a0c4ff}
.agent-terminal .line-num{color:var(--tx3);user-select:none;display:inline-block;min-width:32px;text-align:right;margin-right:8px}

.agent-browser{
  flex:1;display:flex;flex-direction:column;
  background:#0a0a10;color:var(--tx3);font-size:12px;text-align:center;
  overflow:hidden;min-width:0;position:relative;
}
.agent-browser-top{
  flex:1;display:flex;align-items:center;justify-content:center;
  overflow:hidden;position:relative;min-height:0;
}
.agent-browser-top img{
  width:100%;height:100%;object-fit:contain;display:block;
}
.agent-browser-top .placeholder{
  display:flex;flex-direction:column;align-items:center;gap:8px;padding:20px;
}
.agent-browser-top .placeholder svg{opacity:.3}
.agent-browser-bottom{
  flex:0 0 auto;min-height:40px;max-height:40%;border-top:1px solid var(--brd);
  display:flex;align-items:center;justify-content:center;
  color:var(--tx3);font-size:11px;padding:8px;
}

/* Panel input bar (inside each agent box) */
.panel-input-bar{
  border-top:1px solid var(--brd);background:var(--bg3);
  padding:6px 8px;display:flex;align-items:flex-end;gap:6px;flex-shrink:0;
}
.panel-input-wrap{
  flex:1;background:var(--bg);border:1px solid var(--brd);border-radius:8px;
  display:flex;align-items:flex-end;padding:2px;transition:border-color .15s;
}
.panel-input-wrap:focus-within{border-color:var(--acc2)}
.panel-input-wrap .p-attach-btn{
  width:26px;height:26px;background:none;border:none;cursor:pointer;
  display:flex;align-items:center;justify-content:center;color:var(--tx3);
  border-radius:5px;flex-shrink:0;
}
.panel-input-wrap .p-attach-btn:hover{background:var(--bg4);color:var(--tx2)}
.panel-input-wrap textarea{
  flex:1;background:none;border:none;outline:none;color:var(--tx);
  font-family:inherit;font-size:12px;line-height:1.3;padding:4px 4px;
  resize:none;min-height:18px;max-height:80px;
}
.panel-input-wrap textarea::placeholder{color:var(--tx3)}
.panel-send-btn{
  width:26px;height:26px;background:var(--acc);border:none;border-radius:6px;
  cursor:pointer;display:flex;align-items:center;justify-content:center;color:#fff;
  flex-shrink:0;transition:all .12s;
}
.panel-send-btn:hover{background:var(--acc2)}
.panel-attach-tags{display:flex;gap:3px;flex-wrap:wrap;padding-bottom:2px}
.panel-attach-tags .attach-tag{font-size:9px;padding:2px 6px}

.agent-status{
  height:26px;border-top:1px solid var(--brd);display:flex;align-items:center;
  padding:0 10px;font-size:11px;font-weight:600;flex-shrink:0;
}
.agent-status.running{color:var(--acc)}
.agent-status.pass{color:var(--ok)}
.agent-status.fail{color:var(--err)}
.copy-output-btn{background:none;border:none;color:inherit;cursor:pointer;padding:2px 4px;margin-left:6px;opacity:0.6;vertical-align:middle}
.copy-output-btn:hover{opacity:1}

/* === INPUT BAR === */
.input-bar{
  border-top:1px solid var(--brd);background:var(--bg2);
  padding:10px 16px;display:flex;align-items:flex-end;gap:8px;flex-shrink:0;
}
.input-wrap{
  flex:1;background:var(--bg3);border:1px solid var(--brd);border-radius:12px;
  display:flex;align-items:flex-end;padding:2px;transition:border-color .15s;
}
.input-wrap:focus-within{border-color:var(--acc2)}

.input-wrap .attach-btn{
  width:32px;height:32px;background:none;border:none;cursor:pointer;
  display:flex;align-items:center;justify-content:center;color:var(--tx3);
  border-radius:6px;flex-shrink:0;
}
.input-wrap .attach-btn:hover{background:var(--bg4);color:var(--tx2)}

#promptInput{
  flex:1;background:none;border:none;outline:none;color:var(--tx);
  font-family:inherit;font-size:13px;line-height:1.4;padding:7px 4px;
  resize:none;min-height:20px;max-height:150px;
}
#promptInput::placeholder{color:var(--tx3)}

.send-btn{
  width:32px;height:32px;background:var(--acc);border:none;border-radius:8px;
  cursor:pointer;display:flex;align-items:center;justify-content:center;color:#fff;
  flex-shrink:0;transition:all .12s;
}
.send-btn:hover{background:var(--acc2);transform:scale(1.05)}
.send-btn:disabled{opacity:.3;cursor:not-allowed;transform:none}

#fileInput{display:none}

.attach-tags{display:flex;gap:4px;flex-wrap:wrap;padding-bottom:2px}
.attach-tag{
  padding:3px 8px;background:var(--bg3);border:1px solid var(--brd);border-radius:10px;
  font-size:10px;color:var(--tx2);display:flex;align-items:center;gap:3px;
}
.attach-tag .rm{cursor:pointer;opacity:.5;font-size:13px}
.attach-tag .rm:hover{opacity:1;color:var(--err)}

/* File preview tiles (main bar) */
.attach-preview{
  display:flex;gap:6px;flex-wrap:wrap;padding:4px 0;
}
.attach-preview .file-tile{
  position:relative;width:64px;height:64px;border-radius:8px;overflow:hidden;
  background:var(--bg3);border:1px solid var(--brd);cursor:default;
  display:flex;align-items:center;justify-content:center;flex-direction:column;
}
.attach-preview .file-tile img{
  width:100%;height:100%;object-fit:cover;
}
.attach-preview .file-tile .file-icon{
  font-size:20px;opacity:.6;
}
.attach-preview .file-tile .file-ext{
  font-size:8px;color:var(--tx3);margin-top:2px;text-transform:uppercase;
  max-width:56px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
}
.attach-preview .file-tile .file-name{
  font-size:7px;color:var(--tx3);max-width:56px;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap;
}
.attach-preview .file-tile .tile-rm{
  position:absolute;top:1px;right:1px;width:16px;height:16px;
  background:rgba(0,0,0,.7);color:#fff;border:none;border-radius:50%;
  font-size:10px;cursor:pointer;display:flex;align-items:center;
  justify-content:center;opacity:0;transition:opacity .15s;line-height:1;
}
.attach-preview .file-tile:hover .tile-rm{opacity:1}

/* Panel file previews (smaller) */
.panel-attach-preview{
  display:flex;gap:4px;flex-wrap:wrap;padding:3px 0;
}
.panel-attach-preview .file-tile{
  position:relative;width:40px;height:40px;border-radius:6px;overflow:hidden;
  background:var(--bg3);border:1px solid var(--brd);cursor:default;
  display:flex;align-items:center;justify-content:center;flex-direction:column;
}
.panel-attach-preview .file-tile img{
  width:100%;height:100%;object-fit:cover;
}
.panel-attach-preview .file-tile .file-icon{font-size:14px;opacity:.6}
.panel-attach-preview .file-tile .file-ext{
  font-size:6px;color:var(--tx3);text-transform:uppercase;
}
.panel-attach-preview .file-tile .tile-rm{
  position:absolute;top:0;right:0;width:13px;height:13px;
  background:rgba(0,0,0,.7);color:#fff;border:none;border-radius:50%;
  font-size:8px;cursor:pointer;display:flex;align-items:center;
  justify-content:center;opacity:0;transition:opacity .15s;line-height:1;
}
.panel-attach-preview .file-tile:hover .tile-rm{opacity:1}

/* Drop overlay */
.drop-overlay{
  display:none;position:absolute;inset:0;z-index:100;
  background:rgba(99,102,241,.12);border:2px dashed var(--acc);border-radius:8px;
  pointer-events:none;align-items:center;justify-content:center;
}
.drop-overlay .drop-label{
  background:var(--bg2);padding:8px 18px;border-radius:8px;
  color:var(--acc);font-size:13px;font-weight:600;
  border:1px solid var(--acc);pointer-events:none;
}
.agent-panel.drag-over .drop-overlay{display:flex}
.prompt-bar.drag-over{outline:2px dashed var(--acc);outline-offset:-2px;border-radius:8px}

/* === IN-APP CONFIRM DIALOG === */
.confirm-overlay{
  display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:400;
  align-items:center;justify-content:center;
}
.confirm-overlay.open{display:flex}
.confirm-box{
  background:var(--bg2);border:1px solid var(--brd);border-radius:12px;
  padding:24px;min-width:320px;max-width:420px;text-align:center;
  box-shadow:0 8px 32px rgba(0,0,0,.4);
}
.confirm-box .confirm-msg{font-size:14px;color:var(--tx);margin-bottom:18px}
.confirm-box .confirm-btns{display:flex;gap:10px;justify-content:center}
.confirm-box .confirm-btns button{
  padding:8px 20px;border-radius:8px;border:1px solid var(--brd);
  font-family:inherit;font-size:12px;cursor:pointer;font-weight:500;
}
.confirm-box .btn-cancel{background:var(--bg3);color:var(--tx2)}
.confirm-box .btn-cancel:hover{background:var(--bg4)}
.confirm-box .btn-confirm{background:var(--err);border-color:var(--err);color:#fff}
.confirm-box .btn-confirm:hover{opacity:.85}

::-webkit-scrollbar{width:5px;height:5px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--brd);border-radius:3px}
::-webkit-scrollbar-thumb:hover{background:var(--tx3)}

/* === ESCALATION SVG OVERLAY === */
.escalation-svg{
  position:absolute;top:0;left:0;width:100%;height:100%;
  pointer-events:none;z-index:4;overflow:visible;
}
.escalation-line{
  fill:none;stroke:url(#escGrad);stroke-width:2;
  stroke-linecap:round;opacity:.7;
  filter:drop-shadow(0 0 6px rgba(168,130,255,.4));
}
.escalation-line.animating{
  stroke-dasharray:8 4;
  animation:escDash 1.2s linear infinite;
}
@keyframes escDash{to{stroke-dashoffset:-24}}

.escalation-dot{
  fill:#a882ff;filter:drop-shadow(0 0 4px rgba(168,130,255,.6));
}
.escalation-label{
  fill:var(--tx3);font-family:'DM Sans',sans-serif;font-size:9px;
  font-weight:500;letter-spacing:.3px;
}

/* Escalation panel has a subtle purple tint */
.agent-panel.escalation-panel{
  border-color:rgba(168,130,255,.35);
  box-shadow:0 0 20px rgba(168,130,255,.08);
}
.agent-panel.escalation-panel .agent-head{
  background:linear-gradient(135deg,rgba(168,130,255,.12),var(--bg3));
  border-bottom-color:rgba(168,130,255,.25);
}
.agent-panel.escalation-panel .dot.run{
  background:#a882ff;
}
.escalation-badge{
  font-size:9px;font-weight:600;color:#a882ff;
  background:rgba(168,130,255,.12);border:1px solid rgba(168,130,255,.25);
  padding:1px 6px;border-radius:4px;margin-right:4px;
  letter-spacing:.3px;text-transform:uppercase;flex-shrink:0;
}
</style>
</head>
<body>
<div class="app">

<!-- TOOLBAR -->
<div class="toolbar">
  <div class="logo">V</div>
  <span class="title">VerifyBot</span>
  <div class="sep"></div>

  <div class="dropdown-wrap">
    <button class="tb-btn" onclick="toggleDropdown('historyDrop')"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#f59e0b" stroke-width="2" style="vertical-align:-2px;margin-right:3px"><path d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>History</button>
    <div class="dropdown-panel" id="historyDrop">
      <div class="dropdown-header">Pipeline History <button class="clear-btn" onclick="clearFolder('history','historyList')">Clear All</button></div>
      <div class="dropdown-list" id="historyList"></div>
    </div>
  </div>

  <div class="dropdown-wrap">
    <button class="tb-btn" onclick="toggleDropdown('programsDrop')"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#8b5cf6" stroke-width="2" style="vertical-align:-2px;margin-right:3px"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>Programs</button>
    <div class="dropdown-panel" id="programsDrop">
      <div class="dropdown-header">Saved Programs <button class="clear-btn" onclick="clearFolder('programs','programsList')">Clear All</button></div>
      <div class="dropdown-list" id="programsList"></div>
    </div>
  </div>

  <div class="dropdown-wrap">
    <button class="tb-btn" onclick="toggleDropdown('outputsDrop')"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#22d3ee" stroke-width="2" style="vertical-align:-2px;margin-right:3px"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>Outputs</button>
    <div class="dropdown-panel" id="outputsDrop">
      <div class="dropdown-header">Execution Outputs <button class="clear-btn" onclick="clearFolder('outputs','outputsList')">Clear All</button></div>
      <div class="dropdown-list" id="outputsList"></div>
    </div>
  </div>

  <div class="dropdown-wrap">
    <button class="tb-btn" onclick="toggleDropdown('uploadsDrop')"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#34d399" stroke-width="2" style="vertical-align:-2px;margin-right:3px"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>Uploads</button>
    <div class="dropdown-panel" id="uploadsDrop">
      <div class="dropdown-header">Uploaded Files <button class="clear-btn" onclick="clearFolder('uploads','uploadsList')">Clear All</button></div>
      <div class="dropdown-list" id="uploadsList"></div>
    </div>
  </div>

  <div class="sep"></div>
  <button class="tb-btn accent" onclick="runTests()">Run Tests</button>
  <button class="tb-btn" onclick="pushToGithub()" style="border-color:#34d399;color:#34d399"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-2px;margin-right:3px"><path d="M9 19c-5 1.5-5-2.5-7-3m14 6v-3.87a3.37 3.37 0 00-.94-2.61c3.14-.35 6.44-1.54 6.44-7A5.44 5.44 0 0020 4.77 5.07 5.07 0 0019.91 1S18.73.65 16 2.48a13.38 13.38 0 00-7 0C6.27.65 5.09 1 5.09 1A5.07 5.07 0 005 4.77a5.44 5.44 0 00-1.5 3.78c0 5.42 3.3 6.61 6.44 7A3.37 3.37 0 009 18.13V22"/></svg>Push</button>
  <div class="sep"></div>

  <span class="tb-label">Target</span>
  <select class="tb-select" id="targetSel">
    <option value="auto">Auto</option><option value="local">Local</option><option value="raspi">Pi</option>
  </select>
  <span class="tb-label">Retries</span>
  <input class="tb-input" type="number" id="retriesIn" value="3" min="1" max="10">
  <span class="tb-label">Timeout</span>
  <input class="tb-input" type="number" id="timeoutIn" value="30" min="5" max="600">
  <span class="tb-label">Browser</span>
  <select class="tb-select" id="headlessSel">
    <option value="visible">Visible</option><option value="headless">Hidden</option>
  </select>

  <div class="spacer"></div>
  <div class="status-dot" id="statusDot"></div>
  <span class="status-text" id="statusText">Ready</span>
</div>

<!-- FILE VIEWER MODAL -->
<div class="modal-overlay" id="modalOverlay" onclick="if(event.target===this)closeModal()">
  <div class="modal">
    <div class="modal-header">
      <span id="modalTitle">File</span>
      <button class="tb-btn" onclick="copyModalContent()" id="modalCopyBtn" style="margin-left:auto;margin-right:8px;font-size:11px;padding:3px 10px">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-1px;margin-right:3px"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>Copy All
      </button>
      <span class="close" onclick="closeModal()">&times;</span>
    </div>
    <div class="modal-body" id="modalBody"></div>
  </div>
</div>

<!-- CONFIRM DIALOG (in-app themed) -->
<div class="confirm-overlay" id="confirmOverlay" onclick="if(event.target===this)confirmCancel()">
  <div class="confirm-box">
    <div class="confirm-msg" id="confirmMsg">Are you sure?</div>
    <div class="confirm-btns">
      <button class="btn-confirm" id="confirmOk" onclick="confirmOk()">Delete</button>
      <button class="btn-cancel" onclick="confirmCancel()">Cancel</button>
    </div>
  </div>
</div>

<!-- WORKBENCH -->
<div class="workbench" id="workbench">
  <div class="canvas" id="canvas">
    <!-- SVG overlay for escalation connection lines -->
    <svg class="escalation-svg" id="escalationSvg">
      <defs>
        <linearGradient id="escGrad" x1="0%" y1="0%" x2="100%" y2="0%">
          <stop offset="0%" stop-color="rgba(168,130,255,.15)"/>
          <stop offset="30%" stop-color="rgba(168,130,255,.55)"/>
          <stop offset="70%" stop-color="rgba(168,130,255,.55)"/>
          <stop offset="100%" stop-color="rgba(168,130,255,.15)"/>
        </linearGradient>
      </defs>
    </svg>
    <div class="welcome" id="welcome">
      <div class="icon">V</div>
      <h2>VerifyBot Workbench</h2>
      <p>Type a prompt below. Each task spawns an agent panel here. Drag headers to reorder, resize from any edge or corner.</p>
      <div class="examples">
        <div class="example-chip" onclick="useExample(this)">write a fizzbuzz script</div>
        <div class="example-chip" onclick="useExample(this)">make a random number generator</div>
        <div class="example-chip" onclick="useExample(this)">read I2C sensor on raspi</div>
        <div class="example-chip" onclick="useExample(this)">analyze my CSV data</div>
      </div>
    </div>
  </div>
</div>

<!-- INPUT BAR -->
<div class="input-bar prompt-bar">
  <div id="attachPreview" class="attach-preview"></div>
  <div class="input-wrap">
    <button class="attach-btn" onclick="document.getElementById('fileInput').click()" title="Attach files">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M21.44 11.05l-9.19 9.19a6 6 0 01-8.49-8.49l9.19-9.19a4 4 0 015.66 5.66l-9.2 9.19a2 2 0 01-2.83-2.83l8.49-8.48"/></svg>
    </button>
    <textarea id="promptInput" rows="1" placeholder="Describe what you want built or debugged..." onkeydown="handleKey(event)" oninput="autoResize(this)"></textarea>
    <button class="send-btn" id="sendBtn" onclick="sendPrompt()">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
    </button>
  </div>
  <input type="file" id="fileInput" multiple onchange="handleFiles(this.files)">
</div>

</div><!-- .app -->

<script>
const socket = io();
let attachedFiles = [];
let agents = {};
let openDropdown = null;

// === SOCKET ===
socket.on('agent_created', d => {
  const w = document.getElementById('welcome');
  if (w) w.remove();
  createPanel(d.agent_id, d.prompt, d.is_test);
});

socket.on('agent_output', d => {
  // Check if this agent is in escalation mode and redirect output
  let targetAgentId = d.agent_id;
  const parentAgent = agents[d.agent_id];
  if (parentAgent && parentAgent._escalatingTo && agents[parentAgent._escalatingTo]) {
    targetAgentId = parentAgent._escalatingTo;
  }

  const a = agents[targetAgentId];
  if (!a) return;
  const term = a.terminal;

  // Parse and render structured output
  const lines = d.text.split('\n');
  for (const raw of lines) {
    if (!raw && raw !== '') continue;
    const line = stripAnsi(raw);

    // Filter out greenlet/playwright cross-thread noise (comprehensive)
    const lt = line.trim();
    if (line.includes('greenlet.error') || line.includes('Cannot switch to a different thread') ||
        line.includes('_sync_base.py') || line.includes('g_self.switch()') ||
        line.includes('Exception in callback SyncBase') ||
        line.includes('Handle SyncBase') ||
        line.includes('asyncio\\events.py') || line.includes('asyncio/events.py') ||
        line.includes('self._context.run(') || line.includes('self._callback') ||
        line.includes('_sync_base.py:') ||
        line.includes('task.add_done_callback') ||
        line.includes('(otid=') ||
        line.includes('suspended active started main') ||
        (lt.startsWith('Current:') && line.includes('greenlet')) ||
        (lt.startsWith('Expected:') && line.includes('greenlet')) ||
        (lt.startsWith('~') && lt.endsWith('^^')) ||
        lt === '*self._args)' || lt.startsWith('*self._args') ||
        (lt.startsWith('handle:') && line.includes('<Handle')) ||
        (lt.startsWith('File "') && (line.includes('events.py') || line.includes('_sync_base'))) ||
        (lt === 'Traceback (most recent call last):' && d.stream === 'stderr') ||
        (lt.startsWith('line ') && lt.includes(', in _run')) ||
        (lt.startsWith('line ') && lt.includes(', in <lambda>'))
    ) continue;

    const span = document.createElement('span');
    span.className = 'line';

    // Classify line for styling
    if (d.stream === 'stderr' || line.includes('[ERROR]') || line.includes('[FAIL]') || line.includes('Traceback')) {
      span.className = 'line err-text';
    } else if (/^\s*\[\d+\]/.test(line) || /^=+$/.test(line.trim())) {
      span.className = 'line step';
    } else if (line.includes('[OK]') || line.includes('[DONE]') || line.includes('PASS')) {
      span.className = 'line ok';
    } else if (line.includes('[WARN]') || line.includes('[SKIP]') || line.includes('[TIMEOUT]')) {
      span.className = 'line warn';
    } else if (/^[\s]*[\u2500\u2502\u250c\u2514\u251c\u2524\u252c\u2534\u253c\u2504-\u254b]+/.test(line) || /^[\s]*[─│┌└├┤┬┴┼]+/.test(line)) {
      span.className = 'line sep';
    } else if (/^\s+\d+\s*[│|]/.test(line)) {
      // Code display with line numbers
      const m = line.match(/^(\s*\d+\s*[│|])(.*)/);
      if (m) {
        const num = document.createElement('span');
        num.className = 'line-num';
        num.textContent = m[1];
        const code = document.createElement('span');
        code.className = 'code-line';
        code.textContent = m[2];
        span.appendChild(num);
        span.appendChild(code);
        span.className = 'line';
        term.appendChild(span);
        continue;
      }
    } else if (line.startsWith('  [') && line.includes(']')) {
      // Status messages like [SAVED], [UPLOAD], [RUN], [INSTALL]
      span.className = 'line dim';
    }

    span.textContent = span.textContent || line;
    term.appendChild(span);
  }

  term.scrollTop = term.scrollHeight;
});

function copyTerminal(id) {
  const a = agents[id];
  if (!a) return;
  const text = a.terminal.innerText;
  navigator.clipboard.writeText(text).then(() => {
    // Flash the button to confirm
    const btn = a.status.querySelector('.copy-output-btn');
    if (btn) { btn.style.color = '#34d399'; setTimeout(() => btn.style.color = '', 1000); }
  });
}

function stripAnsi(s) {
  // Remove ANSI escape sequences
  return s.replace(/\x1b\[[0-9;]*m/g, '').replace(/\033\[[0-9;]*m/g, '');
}

socket.on('agent_done', d => {
  const a = agents[d.agent_id];
  if (!a) return;

  // Check if this is the end of an escalation
  if (a._escalatingTo) {
    const childId = a._escalatingTo;
    // Finalize the child panel
    const child = agents[childId];
    if (child) {
      child.dot.className = 'dot ' + (d.success ? 'pass' : 'fail');
      child.status.className = 'agent-status ' + (d.success ? 'pass' : 'fail');
      child.status.innerHTML = (d.success ? 'PASS (Thinking)' : 'FAIL (Thinking)') +
        ' <button class="copy-output-btn" onclick="copyTerminal(\'' + childId + '\')" title="Copy output">' +
        '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' +
        '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/>' +
        '</svg></button>';
    }
    finalizeEscalationLine(childId);
    delete a._escalatingTo;
  }

  const ok = d.success;
  a.dot.className = 'dot ' + (ok ? 'pass' : 'fail');
  a.status.className = 'agent-status ' + (ok ? 'pass' : 'fail');
  a.status.innerHTML = (ok ? 'PASS \u2014 completed' : 'FAIL \u2014 see output') +
    ' <button class="copy-output-btn" onclick="copyTerminal(\'' + d.agent_id + '\')" title="Copy output">' +
    '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' +
    '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/>' +
    '</svg></button>';
  updateGlobalStatus();
});

socket.on('agent_error', d => {
  const a = agents[d.agent_id];
  if (!a) return;
  const span = document.createElement('span');
  span.className = 'line err-text';
  span.textContent = '[ERROR] ' + (d.error || 'Unknown');
  a.terminal.appendChild(span);
  a.dot.className = 'dot fail';
  a.status.className = 'agent-status fail';
  a.status.textContent = 'ERROR \u2014 ' + (d.error || '').slice(0, 60);
  updateGlobalStatus();
});

// === CREATE PANEL ===
function createPanel(id, prompt, isTest) {
  // Hide welcome message
  const welcome = document.getElementById('welcome');
  if (welcome) welcome.remove();

  const panel = document.createElement('div');
  panel.className = 'agent-panel';
  panel.id = id;

  const safePrompt = esc(prompt);
  const shortPrompt = esc((isTest ? 'Test Suite' : prompt).slice(0, 80));

  panel.innerHTML =
    // Resize handles on all edges and corners
    '<div class="resize-handle rh-n" data-dir="n"></div>' +
    '<div class="resize-handle rh-s" data-dir="s"></div>' +
    '<div class="resize-handle rh-e" data-dir="e"></div>' +
    '<div class="resize-handle rh-w" data-dir="w"></div>' +
    '<div class="resize-handle rh-ne" data-dir="ne"></div>' +
    '<div class="resize-handle rh-nw" data-dir="nw"></div>' +
    '<div class="resize-handle rh-se" data-dir="se"></div>' +
    '<div class="resize-handle rh-sw" data-dir="sw"></div>' +
    // Header with file buttons integrated
    '<div class="agent-head">' +
      '<div class="dot run"></div>' +
      '<span class="label" title="' + safePrompt + '">' + shortPrompt + '</span>' +
      '<button class="head-btn file-btn fb-history" onclick="event.stopPropagation();openAgentFile(\'' + id + '\',\'history\')" title="View log">' +
        '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>' +
      '</button>' +
      '<button class="head-btn file-btn fb-programs" onclick="event.stopPropagation();openAgentFile(\'' + id + '\',\'programs\')" title="View program">' +
        '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>' +
      '</button>' +
      '<button class="head-btn file-btn fb-outputs" onclick="event.stopPropagation();openAgentFile(\'' + id + '\',\'outputs\')" title="View output">' +
        '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>' +
      '</button>' +
      '<div class="head-sep"></div>' +
      '<button class="head-btn" onclick="closePanel(\'' + id + '\')" title="Close">&times;</button>' +
    '</div>' +
    // Drop overlay for file drag
    '<div class="drop-overlay"><span class="drop-label">Drop files here</span></div>' +
    // Body: terminal left, browser right (split top/bottom)
    '<div class="agent-body">' +
      '<div class="agent-terminal"></div>' +
      '<div class="agent-browser">' +
        '<div class="agent-browser-top">' +
          '<div class="placeholder">' +
            '<svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>' +
            '<span>Waiting for browser...</span>' +
          '</div>' +
          '<img style="display:none" />' +
        '</div>' +
        '<div class="agent-browser-bottom">Extra context (coming soon)</div>' +
      '</div>' +
    '</div>' +
    // Status bar
    '<div class="agent-status running">Running...</div>' +
    // In-panel input bar for follow-ups
    '<div class="panel-input-bar">' +
      '<div class="panel-attach-preview" data-agent="' + id + '"></div>' +
      '<div class="panel-input-wrap">' +
        '<button class="p-attach-btn" onclick="panelAttach(\'' + id + '\')" title="Attach file">' +
          '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M21.44 11.05l-9.19 9.19a6 6 0 01-8.49-8.49l9.19-9.19a4 4 0 015.66 5.66l-9.2 9.19a2 2 0 01-2.83-2.83l8.49-8.48"/></svg>' +
        '</button>' +
        '<textarea rows="1" placeholder="Follow up..." onkeydown="panelKey(event,\'' + id + '\')" oninput="autoResize(this)"></textarea>' +
        '<button class="panel-send-btn" onclick="panelSend(\'' + id + '\')">' +
          '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>' +
        '</button>' +
      '</div>' +
      '<input type="file" class="panel-file-input" multiple style="display:none" onchange="panelFiles(\'' + id + '\',this.files)">' +
    '</div>';

  // Append to canvas (infinite pan area), not workbench directly
  const canvas = document.getElementById('canvas');

  // Position new panel in a tiling grid
  const existing = Object.keys(agents).length;
  const cols = 2;
  const gapX = 700, gapY = 440;
  const col = existing % cols;
  const row = Math.floor(existing / cols);
  panel.style.left = (20 + col * gapX) + 'px';
  panel.style.top = (20 + row * gapY) + 'px';

  canvas.appendChild(panel);

  const head = panel.querySelector('.agent-head');
  makeDraggable(panel, head);
  makeResizable(panel);
  observeTerminalSize(panel);
  setupPanelDrop(panel, id);

  agents[id] = {
    el: panel,
    terminal: panel.querySelector('.agent-terminal'),
    dot: panel.querySelector('.dot'),
    status: panel.querySelector('.agent-status'),
    browserImg: panel.querySelector('.agent-browser-top img'),
    browserPlaceholder: panel.querySelector('.agent-browser-top .placeholder'),
    panelFiles: [],
    panelPreviews: [],
  };

  updateGlobalStatus();
}

// Live screenshots pushed from the pipeline thread
socket.on('agent_screenshot', d => {
  // Route to escalation panel if active
  let targetId = d.agent_id;
  const parent = agents[d.agent_id];
  if (parent && parent._escalatingTo && agents[parent._escalatingTo]) {
    targetId = parent._escalatingTo;
  }
  const a = agents[targetId];
  if (!a || !d.image) return;
  a.browserImg.src = 'data:image/png;base64,' + d.image;
  a.browserImg.style.display = 'block';
  a.browserPlaceholder.style.display = 'none';
});

// Re-activate panel when follow-up triggers
socket.on('agent_running', d => {
  const a = agents[d.agent_id];
  if (!a) return;
  a.dot.className = 'dot run';
  a.status.className = 'agent-status running';
  a.status.textContent = 'Running...';
  updateGlobalStatus();
});

function toggleMin(id) {
  const el = agents[id]?.el;
  if (el) {
    el.classList.toggle('minimized');
  }
}

function closePanel(id) {
  const a = agents[id];
  if (a) {
    a.el.remove();
    socket.emit('close_agent', { agent_id: id });
    // Clean up escalation links
    if (escalationLinks[id]) {
      const linkId = 'esc-line-' + id.replace(/[^a-z0-9]/gi, '_');
      ['', '-dot1', '-dot2', '-label'].forEach(s => {
        const el = document.getElementById(linkId + s);
        if (el) el.remove();
      });
      // Clear parent's redirect
      const parent = agents[escalationLinks[id].parentId];
      if (parent) delete parent._escalatingTo;
      delete escalationLinks[id];
    }
    // Also check if this is a parent with an escalation child
    for (const [childId, link] of Object.entries(escalationLinks)) {
      if (link.parentId === id) {
        const linkId = 'esc-line-' + childId.replace(/[^a-z0-9]/gi, '_');
        ['', '-dot1', '-dot2', '-label'].forEach(s => {
          const el = document.getElementById(linkId + s);
          if (el) el.remove();
        });
        delete escalationLinks[childId];
      }
    }
  }
  delete agents[id];
  if (Object.keys(agents).length === 0) {
    document.getElementById('canvas').innerHTML =
      '<div class="welcome" id="welcome">' +
        '<div class="icon">V</div><h2>VerifyBot Workbench</h2>' +
        '<p>Type a prompt below to spawn an agent.</p>' +
        '<div class="examples">' +
          '<div class="example-chip" onclick="useExample(this)">write a fizzbuzz script</div>' +
          '<div class="example-chip" onclick="useExample(this)">make a random number generator</div>' +
        '</div></div>';
  }
}

// === DRAGGABLE (with minimize on click without drag) ===
function makeDraggable(panel, handle) {
  let sx, sy, ox, oy, dragging = false, moved = false;
  handle.addEventListener('mousedown', e => {
    if (e.target.closest('.head-btn')) return;
    if (panel.classList.contains('maximized')) return;
    dragging = true;
    moved = false;
    ox = parseInt(panel.style.left) || 0;
    oy = parseInt(panel.style.top) || 0;
    sx = e.clientX; sy = e.clientY;
    panel.style.zIndex = 10;
    e.preventDefault();
  });
  document.addEventListener('mousemove', e => {
    if (!dragging) return;
    const scale = window._canvas ? window._canvas.getPan().s : 1;
    const dx = (e.clientX - sx) / scale;
    const dy = (e.clientY - sy) / scale;
    if (Math.abs(dx) > 3 || Math.abs(dy) > 3) moved = true;
    panel.style.left = (ox + dx) + 'px';
    panel.style.top = (oy + dy) + 'px';
    redrawAllEscalationLines();
  });
  document.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    panel.style.zIndex = '';
    // If user clicked header without dragging, toggle minimize
    if (!moved) {
      const id = panel.id;
      if (id && agents[id]) toggleMin(id);
    }
  });
}

// === RESIZABLE FROM ANY EDGE/CORNER ===
function makeResizable(panel) {
  const handles = panel.querySelectorAll('.resize-handle');
  handles.forEach(h => {
    h.addEventListener('mousedown', e => {
      if (panel.classList.contains('maximized')) return;
      const isMin = panel.classList.contains('minimized');
      e.preventDefault();
      e.stopPropagation();
      const dir = h.dataset.dir;
      const scale = window._canvas ? window._canvas.getPan().s : 1;
      const startX = e.clientX, startY = e.clientY;
      const startW = panel.offsetWidth, startH = panel.offsetHeight;
      const startL = parseInt(panel.style.left) || 0;
      const startT = parseInt(panel.style.top) || 0;

      const onMove = ev => {
        const dx = (ev.clientX - startX) / scale;
        const dy = (ev.clientY - startY) / scale;
        let newW = startW, newH = startH, newL = startL, newT = startT;

        if (dir.includes('e')) newW = Math.max(160, startW + dx);
        if (dir.includes('w')) { newW = Math.max(160, startW - dx); newL = startL + dx; if (newW <= 160) newL = startL + (startW - 160); }
        if (!isMin) {
          if (dir.includes('s')) newH = Math.max(120, startH + dy);
          if (dir.includes('n')) { newH = Math.max(120, startH - dy); newT = startT + dy; if (newH <= 120) newT = startT + (startH - 120); }
        }

        panel.style.width = newW + 'px';
        if (!isMin) panel.style.height = newH + 'px';
        panel.style.left = newL + 'px';
        panel.style.top = newT + 'px';
        redrawAllEscalationLines();
      };
      const onUp = () => {
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
      };
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
  });
}

// === TERMINAL FONT SCALING based on panel size ===
function observeTerminalSize(panel) {
  if (!window.ResizeObserver) return;
  const terminal = panel.querySelector('.agent-terminal');
  if (!terminal) return;
  const ro = new ResizeObserver(entries => {
    for (const entry of entries) {
      const w = entry.contentRect.width;
      const h = entry.contentRect.height;
      // Scale font between 9px (small) and 13px (large)
      const area = w * h;
      let fontSize;
      if (area < 60000) fontSize = 9;
      else if (area < 120000) fontSize = 10;
      else if (area < 200000) fontSize = 11;
      else if (area < 350000) fontSize = 12;
      else fontSize = 13;
      terminal.style.fontSize = fontSize + 'px';
    }
  });
  ro.observe(panel);
}

// === PANEL FOLLOW-UP INPUT ===
function panelKey(e, id) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); panelSend(id); }
}
function panelAttach(id) {
  const panel = agents[id]?.el;
  if (panel) panel.querySelector('.panel-file-input').click();
}
function panelFiles(id, fl) {
  const a = agents[id];
  if (!a) return;
  for (const f of fl) addPanelFile(id, f);
}

function addPanelFile(id, file) {
  const a = agents[id];
  if (!a) return;
  a.panelFiles.push(file);
  const isImage = file.type.startsWith('image/');
  const url = isImage ? URL.createObjectURL(file) : null;
  a.panelPreviews.push({ file, url, isImage });
  renderPanelPreviews(id);
}

function renderPanelPreviews(id) {
  const a = agents[id];
  if (!a) return;
  const container = a.el.querySelector('.panel-attach-preview');
  if (!container) return;
  container.innerHTML = a.panelPreviews.map((p, i) => {
    const ext = p.file.name.split('.').pop() || '?';
    if (p.isImage) {
      return '<div class="file-tile" title="' + esc(p.file.name) + '">' +
        '<img src="' + p.url + '" />' +
        '<button class="tile-rm" onclick="rmPanelFile(\'' + id + '\',' + i + ')">&times;</button>' +
      '</div>';
    }
    return '<div class="file-tile" title="' + esc(p.file.name) + '">' +
      '<span class="file-icon">' + fileIcon(ext) + '</span>' +
      '<span class="file-ext">' + esc(ext) + '</span>' +
      '<button class="tile-rm" onclick="rmPanelFile(\'' + id + '\',' + i + ')">&times;</button>' +
    '</div>';
  }).join('');
}

function rmPanelFile(id, i) {
  const a = agents[id];
  if (!a) return;
  if (a.panelPreviews[i]?.url) URL.revokeObjectURL(a.panelPreviews[i].url);
  a.panelFiles.splice(i, 1);
  a.panelPreviews.splice(i, 1);
  renderPanelPreviews(id);
}

// Setup drag/drop on an agent panel
function setupPanelDrop(panel, id) {
  let dragDepth = 0;
  panel.addEventListener('dragenter', e => {
    e.preventDefault();
    dragDepth++;
    panel.classList.add('drag-over');
  });
  panel.addEventListener('dragleave', e => {
    dragDepth--;
    if (dragDepth <= 0) { dragDepth = 0; panel.classList.remove('drag-over'); }
  });
  panel.addEventListener('dragover', e => { e.preventDefault(); });
  panel.addEventListener('drop', e => {
    e.preventDefault(); e.stopPropagation();
    dragDepth = 0;
    panel.classList.remove('drag-over');
    if (e.dataTransfer.files.length) {
      for (const f of e.dataTransfer.files) addPanelFile(id, f);
    }
  });
}

// === FILE ICON HELPER ===
function fileIcon(ext) {
  const map = {
    pdf:'&#128196;', doc:'&#128196;', docx:'&#128196;',
    xls:'&#128202;', xlsx:'&#128202;', csv:'&#128202;',
    py:'&#128187;', js:'&#128187;', ts:'&#128187;', c:'&#128187;', cpp:'&#128187;', rs:'&#128187;',
    zip:'&#128230;', rar:'&#128230;', '7z':'&#128230;', tar:'&#128230;', gz:'&#128230;',
    txt:'&#128209;', md:'&#128209;', log:'&#128209;', json:'&#128209;',
  };
  return map[ext.toLowerCase()] || '&#128196;';
}

// === MAIN BAR FILE PREVIEWS ===
let mainPreviews = [];  // {file, url, isImage}

function addMainFile(file) {
  attachedFiles.push(file);
  const isImage = file.type.startsWith('image/');
  const url = isImage ? URL.createObjectURL(file) : null;
  mainPreviews.push({ file, url, isImage });
  renderMainPreviews();
}

function renderMainPreviews() {
  const container = document.getElementById('attachPreview');
  if (!container) return;
  container.innerHTML = mainPreviews.map((p, i) => {
    const ext = p.file.name.split('.').pop() || '?';
    if (p.isImage) {
      return '<div class="file-tile" title="' + esc(p.file.name) + '">' +
        '<img src="' + p.url + '" />' +
        '<button class="tile-rm" onclick="rmMainFile(' + i + ')">&times;</button>' +
      '</div>';
    }
    return '<div class="file-tile" title="' + esc(p.file.name) + '">' +
      '<span class="file-icon">' + fileIcon(ext) + '</span>' +
      '<span class="file-ext">' + esc(ext) + '</span>' +
      '<span class="file-name">' + esc(p.file.name) + '</span>' +
      '<button class="tile-rm" onclick="rmMainFile(' + i + ')">&times;</button>' +
    '</div>';
  }).join('');
}

function rmMainFile(i) {
  if (mainPreviews[i]?.url) URL.revokeObjectURL(mainPreviews[i].url);
  attachedFiles.splice(i, 1);
  mainPreviews.splice(i, 1);
  renderMainPreviews();
}
async function panelSend(id) {
  const a = agents[id];
  if (!a) return;
  const textarea = a.el.querySelector('.panel-input-wrap textarea');
  const prompt = textarea.value.trim();
  if (!prompt) return;

  let fileNames = [], filePaths = [], fileCtx = '';
  if (a.panelFiles.length > 0) {
    const fd = new FormData();
    a.panelFiles.forEach((f, i) => fd.append('file_' + i, f));
    try {
      const res = await fetch('/api/upload', { method: 'POST', body: fd });
      const d = await res.json();
      fileNames = d.uploaded || [];
      filePaths = d.file_paths || [];
      for (const [n, c] of Object.entries(d.previews || {}))
        fileCtx += '\n\nContents of ' + n + ':\n```\n' + c + '\n```';
    } catch (e) { console.error(e); }
  }

  // Show in terminal
  const line = document.createElement('span');
  line.className = 'line step';
  line.textContent = '[FOLLOWUP] ' + prompt.slice(0, 100);
  a.terminal.appendChild(line);
  a.terminal.scrollTop = a.terminal.scrollHeight;

  socket.emit('followup_agent', {
    agent_id: id,
    prompt: prompt + fileCtx,
    file_paths: filePaths,
  });

  textarea.value = '';
  textarea.style.height = 'auto';
  a.panelFiles = [];
  a.panelPreviews.forEach(p => { if (p.url) URL.revokeObjectURL(p.url); });
  a.panelPreviews = [];
  renderPanelPreviews(id);
}

// === SEND ===
async function sendPrompt() {
  const input = document.getElementById('promptInput');
  const prompt = input.value.trim();
  if (!prompt) return;

  let fileNames = [], filePaths = [], fileCtx = '';
  if (attachedFiles.length > 0) {
    const fd = new FormData();
    attachedFiles.forEach((f, i) => fd.append('file_' + i, f));
    try {
      const res = await fetch('/api/upload', { method: 'POST', body: fd });
      const d = await res.json();
      fileNames = d.uploaded || [];
      filePaths = d.file_paths || [];
      for (const [n, c] of Object.entries(d.previews || {}))
        fileCtx += '\n\nContents of ' + n + ':\n```\n' + c + '\n```';
    } catch (e) { console.error(e); }
  }

  socket.emit('run_agent', {
    prompt: prompt + fileCtx,
    target: document.getElementById('targetSel').value,
    max_retries: document.getElementById('retriesIn').value,
    timeout: document.getElementById('timeoutIn').value,
    headless: document.getElementById('headlessSel').value === 'headless',
    attachments: fileNames,
    file_paths: filePaths,
  });

  input.value = '';
  input.style.height = 'auto';
  mainPreviews.forEach(p => { if (p.url) URL.revokeObjectURL(p.url); });
  mainPreviews = [];
  attachedFiles = [];
  renderMainPreviews();
}

function runTests() { socket.emit('run_tests', {}); }

// === PUSH TO GITHUB ===
async function pushToGithub() {
  // Quick commit message
  const msg = prompt('Commit message:', 'Update from VerifyBot');
  if (!msg) return;

  // First try the simple push
  try {
    const res = await fetch('/api/git_push', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ message: msg }),
    });
    const d = await res.json();

    if (d.success) {
      showToast('Pushed to GitHub!');
      return;
    }

    // Push failed -- build error summary and send through the agent system
    let errorSummary = 'Git push failed. Here are the results:\n\n';
    for (const r of d.results) {
      errorSummary += r.cmd + ' (exit ' + r.exit + ')\n';
      if (r.stdout) errorSummary += 'STDOUT: ' + r.stdout.trim() + '\n';
      if (r.stderr) errorSummary += 'STDERR: ' + r.stderr.trim() + '\n';
      errorSummary += '\n';
    }

    // Spawn an agent to fix it
    showToast('Push failed - spawning agent to fix...');
    const fixPrompt = 'I tried to push to GitHub but it failed. ' +
      'Diagnose and fix the issue. Run the necessary git commands to resolve it ' +
      '(pull, merge, rebase, set upstream, etc.) and then push successfully.\n\n' +
      'Error details:\n' + errorSummary;

    socket.emit('run_agent', {
      prompt: fixPrompt,
      target: 'local',
      max_retries: 3,
      timeout: 30,
      headless: false,
    });

  } catch (e) {
    showToast('Error: ' + e.message);
  }
}

// === COPY MODAL CONTENT ===
function copyModalContent() {
  const body = document.getElementById('modalBody');
  if (!body) return;
  const text = body.textContent || body.innerText;
  navigator.clipboard.writeText(text).then(() => {
    const btn = document.getElementById('modalCopyBtn');
    if (btn) {
      const orig = btn.innerHTML;
      btn.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#34d399" stroke-width="2" style="vertical-align:-1px;margin-right:3px"><polyline points="20 6 9 17 4 12"/></svg>Copied!';
      btn.style.borderColor = '#34d399';
      btn.style.color = '#34d399';
      setTimeout(() => { btn.innerHTML = orig; btn.style.borderColor = ''; btn.style.color = ''; }, 1500);
    }
  }).catch(() => {
    // Fallback for older browsers
    const range = document.createRange();
    range.selectNodeContents(body);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    document.execCommand('copy');
    sel.removeAllRanges();
    showToast('Copied!');
  });
}

function handleKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendPrompt(); }
}
function autoResize(el) { el.style.height = 'auto'; el.style.height = Math.min(el.scrollHeight, 150) + 'px'; }
function useExample(el) { document.getElementById('promptInput').value = el.textContent; document.getElementById('promptInput').focus(); }
function handleFiles(fl) { for (const f of fl) addMainFile(f); document.getElementById('fileInput').value = ''; }
function rmFile(i) { rmMainFile(i); }
function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

// Paste handler — also creates previews
document.addEventListener('paste', e => {
  for (const item of (e.clipboardData?.items || [])) {
    if (item.type.startsWith('image/')) {
      const f = item.getAsFile();
      if (f) addMainFile(f);
    }
  }
});

// Main bar drag/drop with visual feedback
const promptBar = document.querySelector('.prompt-bar');
let mainDragDepth = 0;
promptBar.addEventListener('dragenter', e => {
  e.preventDefault(); mainDragDepth++;
  promptBar.classList.add('drag-over');
});
promptBar.addEventListener('dragleave', e => {
  mainDragDepth--;
  if (mainDragDepth <= 0) { mainDragDepth = 0; promptBar.classList.remove('drag-over'); }
});
promptBar.addEventListener('dragover', e => { e.preventDefault(); });
promptBar.addEventListener('drop', e => {
  e.preventDefault(); mainDragDepth = 0;
  promptBar.classList.remove('drag-over');
  if (e.dataTransfer.files.length) handleFiles(e.dataTransfer.files);
});

// Workbench-level drop fallback (files dropped outside a panel go to main bar)
const wb = document.querySelector('.workbench');
wb.addEventListener('dragover', e => e.preventDefault());
wb.addEventListener('drop', e => {
  // Only handle if not caught by a panel
  if (!e.defaultPrevented) {
    e.preventDefault();
    if (e.dataTransfer.files.length) handleFiles(e.dataTransfer.files);
  }
});

// === DROPDOWNS ===
function toggleDropdown(id) {
  const el = document.getElementById(id);
  if (openDropdown && openDropdown !== el) openDropdown.classList.remove('open');
  el.classList.toggle('open');
  openDropdown = el.classList.contains('open') ? el : null;
  if (el.classList.contains('open')) {
    if (id === 'historyDrop') loadHistory();
    else if (id === 'programsDrop') loadPrograms();
    else if (id === 'outputsDrop') loadOutputs();
    else if (id === 'uploadsDrop') loadUploads();
  }
}
document.addEventListener('click', e => {
  if (openDropdown && !e.target.closest('.dropdown-wrap')) {
    openDropdown.classList.remove('open');
    openDropdown = null;
  }
});

async function loadHistory() {
  const list = document.getElementById('historyList');
  try {
    const d = await (await fetch('/api/history')).json();
    list.innerHTML = d.length ? d.map(i =>
      '<div class="dropdown-item" onclick="viewFile(\'raw_md\',\'' + esc(i.filename) + '\')">' +
        esc(i.prompt) + '<div class="meta">' + i.date + '</div></div>'
    ).join('') : '<div class="dropdown-item" style="color:var(--tx3)">No runs yet</div>';
  } catch (e) { list.innerHTML = '<div class="dropdown-item">Error loading</div>'; }
}

async function loadPrograms() {
  const list = document.getElementById('programsList');
  try {
    const d = await (await fetch('/api/programs')).json();
    list.innerHTML = d.length ? d.map(i =>
      '<div class="dropdown-item" onclick="viewFile(\'programs\',\'' + esc(i.name) + '\')">' +
        esc(i.name) + '<div class="meta">' + i.modified + ' &middot; ' + (i.size / 1024).toFixed(1) + 'KB</div></div>'
    ).join('') : '<div class="dropdown-item" style="color:var(--tx3)">No programs</div>';
  } catch (e) { list.innerHTML = '<div class="dropdown-item">Error loading</div>'; }
}

async function loadOutputs() {
  const list = document.getElementById('outputsList');
  try {
    const d = await (await fetch('/api/outputs')).json();
    list.innerHTML = d.length ? d.map(i =>
      '<div class="dropdown-item" onclick="viewFile(\'outputs\',\'' + esc(i.name) + '\')">' +
        esc(i.name) + '<div class="meta">' + i.modified + ' &middot; ' + (i.size / 1024).toFixed(1) + 'KB</div></div>'
    ).join('') : '<div class="dropdown-item" style="color:var(--tx3)">No outputs</div>';
  } catch (e) { list.innerHTML = '<div class="dropdown-item">Error loading</div>'; }
}

async function loadUploads() {
  const list = document.getElementById('uploadsList');
  try {
    const d = await (await fetch('/api/uploads')).json();
    list.innerHTML = d.length ? d.map(i =>
      '<div class="dropdown-item" onclick="viewFile(\'uploads\',\'' + esc(i.name) + '\')">' +
        esc(i.name) + '<div class="meta">' + i.modified + ' &middot; ' + (i.size / 1024).toFixed(1) + 'KB</div></div>'
    ).join('') : '<div class="dropdown-item" style="color:var(--tx3)">No uploads</div>';
  } catch (e) { list.innerHTML = '<div class="dropdown-item">Error loading</div>'; }
}

async function viewFile(dir, name) {
  if (openDropdown) { openDropdown.classList.remove('open'); openDropdown = null; }
  document.getElementById('modalTitle').textContent = name;
  document.getElementById('modalBody').textContent = 'Loading...';
  document.getElementById('modalOverlay').classList.add('open');
  try {
    const d = await (await fetch('/api/file/' + dir + '/' + encodeURIComponent(name))).json();
    document.getElementById('modalBody').textContent = d.content || d.error || 'Empty';
  } catch (e) { document.getElementById('modalBody').textContent = 'Error: ' + e; }
}
function closeModal() { document.getElementById('modalOverlay').classList.remove('open'); }

// === IN-APP CONFIRM DIALOG ===
let _confirmResolve = null;
function appConfirm(msg) {
  return new Promise(resolve => {
    _confirmResolve = resolve;
    document.getElementById('confirmMsg').textContent = msg;
    document.getElementById('confirmOverlay').classList.add('open');
  });
}
function confirmOk() {
  document.getElementById('confirmOverlay').classList.remove('open');
  if (_confirmResolve) { _confirmResolve(true); _confirmResolve = null; }
}
function confirmCancel() {
  document.getElementById('confirmOverlay').classList.remove('open');
  if (_confirmResolve) { _confirmResolve(false); _confirmResolve = null; }
}

// === CLEAR FOLDER (themed confirm) ===
async function clearFolder(folder, listId) {
  try {
    const res = await fetch('/api/clear/' + folder, { method: 'POST' });
    const d = await res.json();
    document.getElementById(listId).innerHTML =
      '<div class="dropdown-item" style="color:var(--tx3)">Cleared ' + (d.cleared || 0) + ' files</div>';
  } catch (e) {
    console.error('Error clearing:', e);
  }
}

// === ONE-CLICK AGENT FILE OPEN ===
// Fetches the most recent file for that agent in the given folder and opens it directly
async function openAgentFile(id, folder) {
  try {
    const res = await fetch('/api/agent_files/' + id + '/' + folder);
    const data = await res.json();
    if (!data.length) {
      // Brief tooltip-style feedback
      showToast('No ' + folder + ' files yet');
      return;
    }
    // Open the most recent file directly (already sorted by mtime desc)
    const item = data[0];
    if (folder === 'history') {
      viewFile('raw_md', item.filename);
    } else {
      const dir = folder === 'programs' ? 'programs' : 'outputs';
      viewFile(dir, item.name);
    }
  } catch (e) {
    showToast('Error loading ' + folder);
  }
}

// Minimal toast notification
function showToast(msg) {
  let toast = document.getElementById('toast');
  if (!toast) {
    toast = document.createElement('div');
    toast.id = 'toast';
    toast.style.cssText = 'position:fixed;bottom:80px;left:50%;transform:translateX(-50%);' +
      'background:var(--bg2);border:1px solid var(--brd);color:var(--tx2);padding:8px 16px;' +
      'border-radius:8px;font-size:12px;z-index:500;opacity:0;transition:opacity .2s;pointer-events:none';
    document.body.appendChild(toast);
  }
  toast.textContent = msg;
  toast.style.opacity = '1';
  setTimeout(() => { toast.style.opacity = '0'; }, 1800);
}

// === INFINITE CANVAS (Prezi-style pan & zoom) ===
(function initCanvas() {
  const wb = document.getElementById('workbench');
  const canvas = document.getElementById('canvas');
  let panX = 0, panY = 0, scale = 1;
  let isPanning = false, startX, startY, startPanX, startPanY;

  function applyTransform() {
    canvas.style.transform = 'translate(' + panX + 'px,' + panY + 'px) scale(' + scale + ')';
  }

  // Center the canvas initially so (0,0) is near top-left of view
  panX = 20;
  panY = 20;
  applyTransform();

  // Pan: click & drag on empty workbench space
  wb.addEventListener('mousedown', e => {
    // Only pan if clicking directly on workbench or canvas bg, not on a panel
    if (e.target !== wb && e.target !== canvas && !e.target.classList.contains('welcome')
        && !e.target.closest('.welcome')) return;
    isPanning = true;
    startX = e.clientX; startY = e.clientY;
    startPanX = panX; startPanY = panY;
    wb.classList.add('panning');
    e.preventDefault();
  });

  document.addEventListener('mousemove', e => {
    if (!isPanning) return;
    panX = startPanX + (e.clientX - startX);
    panY = startPanY + (e.clientY - startY);
    applyTransform();
  });

  document.addEventListener('mouseup', () => {
    if (isPanning) {
      isPanning = false;
      wb.classList.remove('panning');
    }
  });

  // Zoom: scroll wheel (no Ctrl needed, but only on empty canvas area)
  wb.addEventListener('wheel', e => {
    // If scrolling inside a panel (terminal, browser, etc.), let it scroll normally
    const target = e.target;
    if (target.closest('.agent-terminal') || target.closest('.agent-browser') ||
        target.closest('.panel-input-bar') || target.closest('.dropdown-panel') ||
        target.closest('.modal-body') || target.closest('.confirm-box')) return;
    // Only zoom on workbench/canvas background
    if (target !== wb && target !== canvas && !target.classList.contains('welcome')) return;
    e.preventDefault();
    const rect = wb.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;

    const oldScale = scale;
    const delta = e.deltaY > 0 ? 0.9 : 1.1;
    scale = Math.max(0.2, Math.min(3, scale * delta));

    // Zoom toward mouse position
    panX = mx - (mx - panX) * (scale / oldScale);
    panY = my - (my - panY) * (scale / oldScale);
    applyTransform();
  }, { passive: false });

  // Reset view: double-click on empty canvas area
  wb.addEventListener('dblclick', e => {
    if (e.target !== wb && e.target !== canvas) return;
    panX = 20; panY = 20; scale = 1;
    applyTransform();
  });

  // Expose for panel creation positioning
  window._canvas = { getPan: () => ({x: panX, y: panY, s: scale}) };
})();

// === ESCALATION SYSTEM ===
// Tracks parent -> escalation panel links and draws curved SVG lines
let escalationLinks = {};  // escalation_agent_id -> { parentId, childId }
let activeEscalation = null;  // agent_id currently escalating

// Listen for escalation marker in agent output
socket.on('agent_output', function handleEscalationDetect(d) {
  // Detect escalation start marker printed by _escalate_to_thinking
  const text = d.text || '';
  if (text.includes('MODEL ESCALATION')) {
    activeEscalation = d.agent_id;
  }
  // When we see the Thinking model session load, spawn the child panel
  if (activeEscalation === d.agent_id && text.includes('ChatGPT loaded (model=thinking)')) {
    spawnEscalationPanel(d.agent_id);
    activeEscalation = null;
  }
});

function spawnEscalationPanel(parentId) {
  const parent = agents[parentId];
  if (!parent) return;
  const childId = parentId + '-escalation';
  if (agents[childId]) return;  // already exists

  // Create the escalation panel
  createPanel(childId, 'Thinking (escalation)', false);
  const child = agents[childId];
  if (!child) return;

  // Style it as escalation panel
  child.el.classList.add('escalation-panel');

  // Add the "THINKING" badge to the header
  const label = child.el.querySelector('.agent-head .label');
  if (label) {
    const badge = document.createElement('span');
    badge.className = 'escalation-badge';
    badge.textContent = 'Thinking';
    label.parentNode.insertBefore(badge, label);
    label.textContent = 'Escalation for: ' + (parent.el.querySelector('.label')?.textContent || parentId).slice(0, 50);
  }

  // Position it to the right of the parent panel
  const parentLeft = parseInt(parent.el.style.left) || 0;
  const parentTop = parseInt(parent.el.style.top) || 0;
  const parentW = parent.el.offsetWidth || 680;
  child.el.style.left = (parentLeft + parentW + 80) + 'px';
  child.el.style.top = (parentTop + 40) + 'px';

  // Track the link
  escalationLinks[childId] = { parentId, childId };

  // Draw the initial line
  drawEscalationLine(parentId, childId);

  // Redirect escalation output from parent terminal to child terminal
  redirectEscalationOutput(parentId, childId);
}

function redirectEscalationOutput(parentId, childId) {
  // After escalation panel spawns, intercept agent_output for the parent
  // and route escalation-phase lines to the child panel instead.
  // We do this by marking the parent as "in escalation mode"
  const parent = agents[parentId];
  if (parent) {
    parent._escalatingTo = childId;
  }
}

// Draw a curved bezier line from parent panel's right edge to child panel's left edge
function drawEscalationLine(parentId, childId) {
  const svg = document.getElementById('escalationSvg');
  if (!svg) return;
  const parent = agents[parentId];
  const child = agents[childId];
  if (!parent || !child) return;

  // Remove existing line for this link
  const existingId = 'esc-line-' + childId.replace(/[^a-z0-9]/gi, '_');
  const existing = document.getElementById(existingId);
  if (existing) existing.remove();
  const existingDot1 = document.getElementById(existingId + '-dot1');
  if (existingDot1) existingDot1.remove();
  const existingDot2 = document.getElementById(existingId + '-dot2');
  if (existingDot2) existingDot2.remove();
  const existingLbl = document.getElementById(existingId + '-label');
  if (existingLbl) existingLbl.remove();

  // Get positions (in canvas coordinates)
  const pL = parseInt(parent.el.style.left) || 0;
  const pT = parseInt(parent.el.style.top) || 0;
  const pW = parent.el.offsetWidth || 680;
  const pH = parent.el.offsetHeight || 420;
  const cL = parseInt(child.el.style.left) || 0;
  const cT = parseInt(child.el.style.top) || 0;
  const cH = child.el.offsetHeight || 420;

  // Start: right-center of parent
  const x1 = pL + pW;
  const y1 = pT + pH / 2;
  // End: left-center of child
  const x2 = cL;
  const y2 = cT + cH / 2;

  // Control points for a smooth S-curve
  const dx = Math.abs(x2 - x1);
  const cpOffset = Math.max(60, dx * 0.4);
  const cp1x = x1 + cpOffset;
  const cp1y = y1;
  const cp2x = x2 - cpOffset;
  const cp2y = y2;

  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.id = existingId;
  path.setAttribute('d', 'M' + x1 + ',' + y1 + ' C' + cp1x + ',' + cp1y + ' ' + cp2x + ',' + cp2y + ' ' + x2 + ',' + y2);
  path.setAttribute('class', 'escalation-line animating');
  svg.appendChild(path);

  // Dots at connection points
  const dot1 = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
  dot1.id = existingId + '-dot1';
  dot1.setAttribute('cx', x1); dot1.setAttribute('cy', y1);
  dot1.setAttribute('r', '3'); dot1.setAttribute('class', 'escalation-dot');
  svg.appendChild(dot1);

  const dot2 = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
  dot2.id = existingId + '-dot2';
  dot2.setAttribute('cx', x2); dot2.setAttribute('cy', y2);
  dot2.setAttribute('r', '3'); dot2.setAttribute('class', 'escalation-dot');
  svg.appendChild(dot2);

  // Label at midpoint
  const mx = (x1 + x2) / 2;
  const my = (y1 + y2) / 2 - 8;
  const lbl = document.createElementNS('http://www.w3.org/2000/svg', 'text');
  lbl.id = existingId + '-label';
  lbl.setAttribute('x', mx); lbl.setAttribute('y', my);
  lbl.setAttribute('text-anchor', 'middle');
  lbl.setAttribute('class', 'escalation-label');
  lbl.textContent = 'context handoff';
  svg.appendChild(lbl);
}

// Redraw all escalation lines (called on drag/resize)
function redrawAllEscalationLines() {
  for (const link of Object.values(escalationLinks)) {
    drawEscalationLine(link.parentId, link.childId);
  }
}

// Stop animating the line when escalation finishes
function finalizeEscalationLine(childId) {
  const existingId = 'esc-line-' + childId.replace(/[^a-z0-9]/gi, '_');
  const path = document.getElementById(existingId);
  if (path) path.classList.remove('animating');
}

function updateGlobalStatus() {
  const dot = document.getElementById('statusDot');
  const txt = document.getElementById('statusText');
  const running = Object.values(agents).filter(a => a.dot.classList.contains('run')).length;
  const total = Object.keys(agents).length;
  if (running > 0) {
    dot.className = 'status-dot busy';
    txt.textContent = running + ' agent' + (running > 1 ? 's' : '') + ' running';
  } else if (total > 0) {
    dot.className = 'status-dot';
    txt.textContent = total + ' agent' + (total > 1 ? 's' : '') + ' finished';
  } else {
    dot.className = 'status-dot';
    txt.textContent = 'Ready';
  }
}
</script>
</body>
</html>'''


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    port = int(os.environ.get("VERIFYBOT_PORT", 5000))
    host = "127.0.0.1"

    print()
    print("=" * 60)
    print("  VerifyBot Workbench")
    print("=" * 60)
    print(f"  URL:  http://{host}:{port}")
    print(f"  Root: {ROOT}")
    print()
    print("  Press Ctrl+C to stop.")
    print("=" * 60)
    print()

    def open_browser():
        time.sleep(1.5)
        webbrowser.open(f"http://{host}:{port}")

    threading.Thread(target=open_browser, daemon=True).start()
    socketio.run(app, host=host, port=port, debug=False, allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()
