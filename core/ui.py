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

    On Windows, the original console may use cp1252 which can't handle
    Unicode box-drawing chars etc. We catch those encoding errors silently.
    """
    def __init__(self, original, stream_name="stdout"):
        self.original = original
        self.stream_name = stream_name
        self.agent_id = None
        self.sid = None

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
        for f in sorted(RAW_MD_DIR.iterdir(), reverse=True):
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
        for f in sorted(PROGRAMS_DIR.iterdir(), reverse=True):
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
        for f in sorted(OUTPUTS_DIR.iterdir(), reverse=True):
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
               "outputs": OUTPUTS_DIR, "context": CONTEXT_DIR}
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
        try:
            from main import run_pipeline
            from core.session import ChatGPTSession
            from skills.chatgpt_skill import append_to_log
            import base64 as _b64

            # --- Create session on THIS thread so Playwright is happy ---
            session = ChatGPTSession(headed=not headless)
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
            # Close the browser session
            if session:
                try:
                    session.__exit__(None, None, None)
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
    """Run tests.py as a subprocess and stream output."""
    global _agent_counter
    with _agents_lock:
        _agent_counter += 1
        agent_id = f"test-{_agent_counter}"

    sid = request.sid
    emit("agent_created", {"agent_id": agent_id, "prompt": "Test Suite", "is_test": True})

    def run():
        try:
            proc = subprocess.Popen(
                [sys.executable, str(ROOT / "tests.py")],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, cwd=str(ROOT),
                encoding="utf-8", errors="replace",
            )
            for line in proc.stdout:
                socketio.emit("agent_output", {
                    "agent_id": agent_id, "stream": "stdout", "text": line,
                }, to=sid)
            proc.wait()
            socketio.emit("agent_done", {
                "agent_id": agent_id, "success": proc.returncode == 0,
            }, to=sid)
        except Exception as e:
            socketio.emit("agent_error", {"agent_id": agent_id, "error": str(e)}, to=sid)
        finally:
            with _agents_lock:
                _agents.pop(agent_id, None)

    t = threading.Thread(target=run, daemon=True, name=agent_id)
    with _agents_lock:
        _agents[agent_id] = t
    t.start()

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
  display:flex;align-items:center;gap:8px;
}
.dropdown-list{flex:1;overflow-y:auto;padding:4px}
.dropdown-item{
  padding:8px 12px;border-radius:7px;cursor:pointer;font-size:12px;
  color:var(--tx2);transition:all .1s;margin-bottom:1px;
}
.dropdown-item:hover{background:var(--bg3);color:var(--tx)}
.dropdown-item .meta{font-size:10px;color:var(--tx3);margin-top:2px}

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
  flex:1;overflow:auto;padding:12px;display:flex;flex-wrap:wrap;
  align-content:flex-start;gap:12px;position:relative;
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
  width:calc(50% - 6px);height:420px;
  position:relative;
}
.agent-panel.maximized{
  position:fixed!important;inset:var(--toolbar-h) 0 0 0!important;
  width:auto!important;height:auto!important;z-index:50;border-radius:0;
}
.agent-panel.minimized .agent-body,
.agent-panel.minimized .agent-status,
.agent-panel.minimized .panel-input-bar,
.agent-panel.minimized .resize-handle{display:none}
.agent-panel.minimized{height:34px!important;min-height:34px!important;overflow:hidden}

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

::-webkit-scrollbar{width:5px;height:5px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--brd);border-radius:3px}
::-webkit-scrollbar-thumb:hover{background:var(--tx3)}
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
    <button class="tb-btn" onclick="toggleDropdown('historyDrop')">History</button>
    <div class="dropdown-panel" id="historyDrop">
      <div class="dropdown-header">Pipeline History (raw_md/)</div>
      <div class="dropdown-list" id="historyList"></div>
    </div>
  </div>

  <div class="dropdown-wrap">
    <button class="tb-btn" onclick="toggleDropdown('programsDrop')">Programs</button>
    <div class="dropdown-panel" id="programsDrop">
      <div class="dropdown-header">Saved Programs</div>
      <div class="dropdown-list" id="programsList"></div>
    </div>
  </div>

  <div class="dropdown-wrap">
    <button class="tb-btn" onclick="toggleDropdown('outputsDrop')">Outputs</button>
    <div class="dropdown-panel" id="outputsDrop">
      <div class="dropdown-header">Execution Outputs</div>
      <div class="dropdown-list" id="outputsList"></div>
    </div>
  </div>

  <div class="sep"></div>
  <button class="tb-btn accent" onclick="runTests()">Run Tests</button>
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
      <span class="close" onclick="closeModal()">&times;</span>
    </div>
    <div class="modal-body" id="modalBody"></div>
  </div>
</div>

<!-- WORKBENCH -->
<div class="workbench" id="workbench">
  <div class="welcome" id="welcome">
    <div class="icon">V</div>
    <h2>VerifyBot Workbench</h2>
    <p>Type a prompt below. Each task spawns an agent panel here. Drag headers to reorder, resize from corners, maximize with the button.</p>
    <div class="examples">
      <div class="example-chip" onclick="useExample(this)">write a fizzbuzz script</div>
      <div class="example-chip" onclick="useExample(this)">make a random number generator</div>
      <div class="example-chip" onclick="useExample(this)">read I2C sensor on raspi</div>
      <div class="example-chip" onclick="useExample(this)">analyze my CSV data</div>
    </div>
  </div>
</div>

<!-- INPUT BAR -->
<div class="input-bar">
  <div id="attachTags" class="attach-tags"></div>
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
  const a = agents[d.agent_id];
  if (!a) return;
  const term = a.terminal;

  // Parse and render structured output
  const lines = d.text.split('\n');
  for (const raw of lines) {
    if (!raw && raw !== '') continue;
    const line = stripAnsi(raw);

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

function stripAnsi(s) {
  // Remove ANSI escape sequences
  return s.replace(/\x1b\[[0-9;]*m/g, '').replace(/\033\[[0-9;]*m/g, '');
}

socket.on('agent_done', d => {
  const a = agents[d.agent_id];
  if (!a) return;
  const ok = d.success;
  a.dot.className = 'dot ' + (ok ? 'pass' : 'fail');
  a.status.className = 'agent-status ' + (ok ? 'pass' : 'fail');
  a.status.textContent = ok ? 'PASS \u2014 completed' : 'FAIL \u2014 see output';
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
  const wb = document.getElementById('workbench');
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
    // Header
    '<div class="agent-head">' +
      '<div class="dot run"></div>' +
      '<span class="label" title="' + safePrompt + '">' + shortPrompt + '</span>' +
      '<button class="head-btn" onclick="toggleMin(\'' + id + '\')" title="Minimize">&#9644;</button>' +
      '<button class="head-btn" onclick="toggleMax(\'' + id + '\')" title="Maximize">&#9634;</button>' +
      '<button class="head-btn" onclick="closePanel(\'' + id + '\')" title="Close">&times;</button>' +
    '</div>' +
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
      '<div class="panel-attach-tags" data-agent="' + id + '"></div>' +
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

  wb.appendChild(panel);

  const head = panel.querySelector('.agent-head');
  makeDraggable(panel, head);
  makeResizable(panel);
  observeTerminalSize(panel);

  agents[id] = {
    el: panel,
    terminal: panel.querySelector('.agent-terminal'),
    dot: panel.querySelector('.dot'),
    status: panel.querySelector('.agent-status'),
    browserImg: panel.querySelector('.agent-browser-top img'),
    browserPlaceholder: panel.querySelector('.agent-browser-top .placeholder'),
    panelFiles: [],  // attached files for follow-up
  };

  updateGlobalStatus();
}

// Live screenshots pushed from the pipeline thread
socket.on('agent_screenshot', d => {
  const a = agents[d.agent_id];
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

function toggleMax(id) {
  const el = agents[id]?.el;
  if (el) {
    el.classList.remove('minimized');
    el.classList.toggle('maximized');
  }
}

function toggleMin(id) {
  const el = agents[id]?.el;
  if (el) {
    el.classList.remove('maximized');
    el.classList.toggle('minimized');
  }
}

function closePanel(id) {
  const a = agents[id];
  if (a) {
    a.el.remove();
    socket.emit('close_agent', { agent_id: id });
  }
  delete agents[id];
  if (Object.keys(agents).length === 0) {
    document.getElementById('workbench').innerHTML =
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
    const r = panel.getBoundingClientRect();
    const wr = panel.parentElement.getBoundingClientRect();
    ox = r.left - wr.left + panel.parentElement.scrollLeft;
    oy = r.top - wr.top + panel.parentElement.scrollTop;
    sx = e.clientX; sy = e.clientY;
    panel.style.position = 'absolute';
    panel.style.left = ox + 'px';
    panel.style.top = oy + 'px';
    panel.style.zIndex = 10;
    e.preventDefault();
  });
  document.addEventListener('mousemove', e => {
    if (!dragging) return;
    const dx = e.clientX - sx, dy = e.clientY - sy;
    if (Math.abs(dx) > 3 || Math.abs(dy) > 3) moved = true;
    panel.style.left = (ox + dx) + 'px';
    panel.style.top = (oy + dy) + 'px';
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
      if (panel.classList.contains('maximized') || panel.classList.contains('minimized')) return;
      e.preventDefault();
      e.stopPropagation();
      const dir = h.dataset.dir;
      const startX = e.clientX, startY = e.clientY;
      const rect = panel.getBoundingClientRect();
      const startW = rect.width, startH = rect.height;
      const startL = panel.offsetLeft, startT = panel.offsetTop;

      // Ensure absolute positioning
      if (panel.style.position !== 'absolute') {
        const wr = panel.parentElement.getBoundingClientRect();
        panel.style.position = 'absolute';
        panel.style.left = (rect.left - wr.left + panel.parentElement.scrollLeft) + 'px';
        panel.style.top = (rect.top - wr.top + panel.parentElement.scrollTop) + 'px';
      }

      const onMove = ev => {
        const dx = ev.clientX - startX, dy = ev.clientY - startY;
        let newW = startW, newH = startH, newL = startL, newT = startT;

        if (dir.includes('e')) newW = Math.max(420, startW + dx);
        if (dir.includes('w')) { newW = Math.max(420, startW - dx); newL = startL + dx; if (newW <= 420) newL = startL + (startW - 420); }
        if (dir.includes('s')) newH = Math.max(120, startH + dy);
        if (dir.includes('n')) { newH = Math.max(120, startH - dy); newT = startT + dy; if (newH <= 120) newT = startT + (startH - 120); }

        panel.style.width = newW + 'px';
        panel.style.height = newH + 'px';
        panel.style.left = newL + 'px';
        panel.style.top = newT + 'px';
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
  for (const f of fl) a.panelFiles.push(f);
  updatePanelAttachTags(id);
}
function updatePanelAttachTags(id) {
  const a = agents[id];
  if (!a) return;
  const tags = a.el.querySelector('.panel-attach-tags');
  tags.innerHTML = a.panelFiles.map((f, i) =>
    '<div class="attach-tag">' + esc(f.name) + '<span class="rm" onclick="rmPanelFile(\'' + id + '\',' + i + ')">&times;</span></div>'
  ).join('');
}
function rmPanelFile(id, i) {
  const a = agents[id];
  if (!a) return;
  a.panelFiles.splice(i, 1);
  updatePanelAttachTags(id);
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
  updatePanelAttachTags(id);
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
  attachedFiles = [];
  updateAttachTags();
}

function runTests() { socket.emit('run_tests', {}); }

function handleKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendPrompt(); }
}
function autoResize(el) { el.style.height = 'auto'; el.style.height = Math.min(el.scrollHeight, 150) + 'px'; }
function useExample(el) { document.getElementById('promptInput').value = el.textContent; document.getElementById('promptInput').focus(); }
function handleFiles(fl) { for (const f of fl) attachedFiles.push(f); updateAttachTags(); document.getElementById('fileInput').value = ''; }
function updateAttachTags() {
  document.getElementById('attachTags').innerHTML = attachedFiles.map((f, i) =>
    '<div class="attach-tag">' + esc(f.name) + '<span class="rm" onclick="rmFile(' + i + ')">&times;</span></div>'
  ).join('');
}
function rmFile(i) { attachedFiles.splice(i, 1); updateAttachTags(); }
function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

// Paste / drag-drop
document.addEventListener('paste', e => {
  for (const item of (e.clipboardData?.items || [])) {
    if (item.type.startsWith('image/')) { const f = item.getAsFile(); if (f) { attachedFiles.push(f); updateAttachTags(); } }
  }
});
const wb = document.querySelector('.workbench');
wb.addEventListener('dragover', e => e.preventDefault());
wb.addEventListener('drop', e => { e.preventDefault(); if (e.dataTransfer.files.length) handleFiles(e.dataTransfer.files); });

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
