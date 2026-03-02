#!/usr/bin/env python3
"""
ui.py -- Web UI for VerifyBot Agent.

Launch with:
    python ui.py

Opens a browser window with a modern chat interface.
All existing CLI functionality works through the UI.

Dependencies (auto-installed on first run):
    pip install flask flask-socketio
"""

import base64
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Auto-install UI dependencies
# ---------------------------------------------------------------------------

def _ensure_deps():
    """Install Flask and Flask-SocketIO if missing."""
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

from flask import Flask, send_from_directory, request, jsonify
from flask_socketio import SocketIO, emit

# ---------------------------------------------------------------------------
# Determine project root (ui.py lives alongside main.py at project root)
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
PROGRAMS_DIR = ROOT / "programs"
OUTPUTS_DIR = ROOT / "outputs"
RAW_MD_DIR = ROOT / "raw_md"
CONTEXT_DIR = ROOT / "context"
UPLOADS_DIR = ROOT / "uploads"  # For files attached via UI

# Make sure we can import project modules
sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__, static_folder=None)
app.config["SECRET_KEY"] = "verifybot-ui-" + str(os.getpid())
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ---------------------------------------------------------------------------
# Pipeline output capture -- redirect prints to WebSocket
# ---------------------------------------------------------------------------

class SocketIOWriter:
    """Captures stdout/stderr and streams to the WebSocket client."""

    def __init__(self, original, stream_name="stdout"):
        self.original = original
        self.stream_name = stream_name
        self.sid = None  # active socket session

    def write(self, text):
        if self.original:
            self.original.write(text)
            self.original.flush()
        if text.strip() and self.sid:
            socketio.emit("pipeline_output", {
                "stream": self.stream_name,
                "text": text,
                "timestamp": datetime.now().isoformat(),
            }, to=self.sid)

    def flush(self):
        if self.original:
            self.original.flush()


# Global writers
_stdout_writer = SocketIOWriter(sys.stdout, "stdout")
_stderr_writer = SocketIOWriter(sys.stderr, "stderr")

# ---------------------------------------------------------------------------
# Active pipeline state
# ---------------------------------------------------------------------------

_pipeline_lock = threading.Lock()
_pipeline_running = False
_pipeline_thread = None

# ---------------------------------------------------------------------------
# Routes -- serve the SPA
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return _get_html()


@app.route("/api/status")
def api_status():
    """Return current agent status."""
    return jsonify({
        "running": _pipeline_running,
        "has_browser_profile": (ROOT / ".browser_profile").exists(),
        "setup_complete": (ROOT / "core" / ".setup_complete").exists(),
        "has_env": (ROOT / ".env").exists(),
    })


@app.route("/api/history")
def api_history():
    """Return recent pipeline runs from raw_md/."""
    runs = []
    if RAW_MD_DIR.exists():
        for f in sorted(RAW_MD_DIR.iterdir(), reverse=True):
            if f.suffix == ".md" and f.name != "test.md":
                try:
                    content = f.read_text(encoding="utf-8")
                    # Extract prompt from first few lines
                    prompt_match = re.search(r"\*\*Prompt\*\*:\s*(.+)", content)
                    prompt = prompt_match.group(1) if prompt_match else f.stem
                    runs.append({
                        "filename": f.name,
                        "prompt": prompt[:120],
                        "timestamp": f.stat().st_mtime,
                        "date": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                    })
                except Exception:
                    pass
    return jsonify(runs[:50])


@app.route("/api/programs")
def api_programs():
    """List saved programs."""
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
    """List output files."""
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


@app.route("/api/file/<path:filepath>")
def api_file(filepath):
    """Read a file's content."""
    # Only allow reading from safe directories
    safe_dirs = [PROGRAMS_DIR, OUTPUTS_DIR, RAW_MD_DIR, CONTEXT_DIR]
    for safe in safe_dirs:
        candidate = safe / filepath
        if candidate.exists() and candidate.is_file():
            try:
                return jsonify({"content": candidate.read_text(encoding="utf-8")})
            except Exception:
                return jsonify({"content": "(binary file)"}), 200
    return jsonify({"error": "File not found"}), 404


@app.route("/api/upload", methods=["POST"])
def api_upload():
    """Handle file uploads from the UI."""
    UPLOADS_DIR.mkdir(exist_ok=True)
    uploaded = []
    for key in request.files:
        f = request.files[key]
        if f.filename:
            dest = UPLOADS_DIR / f.filename
            f.save(str(dest))
            uploaded.append(f.filename)
    return jsonify({"uploaded": uploaded})


# ---------------------------------------------------------------------------
# WebSocket events
# ---------------------------------------------------------------------------

@socketio.on("connect")
def on_connect():
    emit("status", {"running": _pipeline_running})


@socketio.on("run_pipeline")
def on_run_pipeline(data):
    """Start a pipeline run from the UI."""
    global _pipeline_running, _pipeline_thread

    if _pipeline_running:
        emit("pipeline_error", {"error": "Pipeline already running. Wait for it to finish."})
        return

    prompt = data.get("prompt", "").strip()
    if not prompt:
        emit("pipeline_error", {"error": "Empty prompt."})
        return

    target = data.get("target", None)  # None = auto-detect
    max_retries = int(data.get("max_retries", 3))
    timeout = int(data.get("timeout", 30))
    headless = data.get("headless", False)
    attachments = data.get("attachments", [])

    # If there are attached files, mention them in the prompt context
    if attachments:
        attachment_note = "\n\n[Attached files: " + ", ".join(attachments) + "]"
        prompt += attachment_note

    sid = request.sid
    _stdout_writer.sid = sid
    _stderr_writer.sid = sid

    def run():
        global _pipeline_running
        _pipeline_running = True
        socketio.emit("pipeline_started", {"prompt": prompt}, to=sid)

        # Redirect stdout/stderr to capture pipeline output
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout = _stdout_writer
        sys.stderr = _stderr_writer

        try:
            from main import run_pipeline
            result = run_pipeline(
                prompt=prompt,
                target=target if target != "auto" else None,
                max_retries=max_retries,
                timeout=timeout,
                headed=not headless,
            )
            socketio.emit("pipeline_complete", {
                "success": result,
                "prompt": prompt,
            }, to=sid)
        except Exception as e:
            socketio.emit("pipeline_error", {
                "error": str(e),
            }, to=sid)
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr
            _pipeline_running = False
            _stdout_writer.sid = None
            _stderr_writer.sid = None

    _pipeline_thread = threading.Thread(target=run, daemon=True)
    _pipeline_thread.start()


@socketio.on("stop_pipeline")
def on_stop_pipeline():
    """Signal to stop (best effort -- pipelines aren't easily cancellable)."""
    global _pipeline_running
    if _pipeline_running:
        _pipeline_running = False
        emit("pipeline_output", {
            "stream": "stderr",
            "text": "\n[UI] Stop requested. Pipeline will halt after current step.\n",
            "timestamp": datetime.now().isoformat(),
        })


# ---------------------------------------------------------------------------
# The HTML UI (single-page app, embedded)
# ---------------------------------------------------------------------------

def _get_html():
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>VerifyBot Agent</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.5/socket.io.min.js"></script>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=DM+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root {
    --bg-primary: #0a0a0f;
    --bg-secondary: #12121a;
    --bg-tertiary: #1a1a26;
    --bg-input: #16161f;
    --border: #2a2a3a;
    --border-focus: #5b5bff;
    --text-primary: #e8e8f0;
    --text-secondary: #8888a0;
    --text-muted: #555568;
    --accent: #6c6cff;
    --accent-dim: #4a4abd;
    --accent-glow: rgba(108, 108, 255, 0.15);
    --success: #3ddc84;
    --error: #ff5555;
    --warning: #ffb347;
    --stdout-color: #c8c8dc;
    --stderr-color: #ff6b6b;
    --sidebar-width: 280px;
    --header-height: 56px;
}

* { margin: 0; padding: 0; box-sizing: border-box; }

html, body {
    height: 100%;
    overflow: hidden;
    font-family: 'DM Sans', sans-serif;
    background: var(--bg-primary);
    color: var(--text-primary);
}

/* --- Layout --- */
.app {
    display: flex;
    height: 100vh;
}

/* --- Sidebar --- */
.sidebar {
    width: var(--sidebar-width);
    background: var(--bg-secondary);
    border-right: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    flex-shrink: 0;
    transition: transform 0.3s ease;
}

.sidebar-header {
    padding: 16px 20px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    gap: 10px;
    height: var(--header-height);
}

.sidebar-header .logo {
    width: 28px;
    height: 28px;
    background: linear-gradient(135deg, var(--accent), #9b6cff);
    border-radius: 8px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-weight: 700;
    font-size: 14px;
    color: white;
    flex-shrink: 0;
}

.sidebar-header h1 {
    font-size: 16px;
    font-weight: 600;
    letter-spacing: -0.3px;
}

.sidebar-header .version {
    font-size: 11px;
    color: var(--text-muted);
    margin-left: auto;
}

.new-chat-btn {
    margin: 12px 14px;
    padding: 10px 14px;
    background: var(--bg-tertiary);
    border: 1px solid var(--border);
    border-radius: 10px;
    color: var(--text-primary);
    font-family: inherit;
    font-size: 13px;
    font-weight: 500;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 8px;
    transition: all 0.15s ease;
}

.new-chat-btn:hover {
    background: var(--bg-input);
    border-color: var(--accent-dim);
}

.new-chat-btn svg { opacity: 0.6; }

/* Sidebar tabs */
.sidebar-tabs {
    display: flex;
    padding: 0 14px;
    gap: 2px;
    margin-bottom: 8px;
}

.sidebar-tab {
    flex: 1;
    padding: 7px 8px;
    background: none;
    border: none;
    border-radius: 6px;
    color: var(--text-muted);
    font-family: inherit;
    font-size: 12px;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.15s;
}

.sidebar-tab.active {
    background: var(--bg-tertiary);
    color: var(--text-primary);
}

.sidebar-tab:hover:not(.active) { color: var(--text-secondary); }

.sidebar-list {
    flex: 1;
    overflow-y: auto;
    padding: 0 8px;
}

.sidebar-item {
    padding: 10px 12px;
    border-radius: 8px;
    cursor: pointer;
    font-size: 13px;
    color: var(--text-secondary);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    transition: all 0.12s;
    margin-bottom: 2px;
}

.sidebar-item:hover { background: var(--bg-tertiary); color: var(--text-primary); }
.sidebar-item .meta {
    font-size: 11px;
    color: var(--text-muted);
    margin-top: 3px;
}

/* Sidebar footer -- settings */
.sidebar-footer {
    padding: 12px 14px;
    border-top: 1px solid var(--border);
}

.settings-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 8px;
}

.setting-item {
    display: flex;
    flex-direction: column;
    gap: 4px;
}

.setting-item label {
    font-size: 11px;
    color: var(--text-muted);
    font-weight: 500;
    text-transform: uppercase;
    letter-spacing: 0.5px;
}

.setting-item select, .setting-item input {
    padding: 5px 8px;
    background: var(--bg-tertiary);
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text-primary);
    font-family: inherit;
    font-size: 12px;
    outline: none;
}

.setting-item select:focus, .setting-item input:focus {
    border-color: var(--border-focus);
}

.setting-item select option { background: var(--bg-secondary); }

/* --- Main panel --- */
.main {
    flex: 1;
    display: flex;
    flex-direction: column;
    min-width: 0;
}

.main-header {
    height: var(--header-height);
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    padding: 0 24px;
    gap: 12px;
    flex-shrink: 0;
}

.status-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--text-muted);
    flex-shrink: 0;
}

.status-dot.ready { background: var(--success); }
.status-dot.running {
    background: var(--accent);
    animation: pulse 1.5s ease-in-out infinite;
}
.status-dot.error { background: var(--error); }

@keyframes pulse {
    0%, 100% { opacity: 1; box-shadow: 0 0 0 0 var(--accent-glow); }
    50% { opacity: 0.7; box-shadow: 0 0 0 8px transparent; }
}

.status-text {
    font-size: 13px;
    color: var(--text-secondary);
    font-weight: 500;
}

.stop-btn {
    margin-left: auto;
    padding: 6px 14px;
    background: var(--error);
    color: white;
    border: none;
    border-radius: 6px;
    font-family: inherit;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
    display: none;
    transition: opacity 0.15s;
}

.stop-btn:hover { opacity: 0.85; }
.stop-btn.visible { display: block; }

/* --- Chat area --- */
.chat-container {
    flex: 1;
    overflow-y: auto;
    padding: 24px;
    display: flex;
    flex-direction: column;
    gap: 16px;
}

.welcome {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    flex: 1;
    gap: 16px;
    text-align: center;
    opacity: 0.6;
}

.welcome .icon {
    width: 64px;
    height: 64px;
    background: linear-gradient(135deg, var(--accent), #9b6cff);
    border-radius: 18px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 28px;
    color: white;
    font-weight: 700;
}

.welcome h2 {
    font-size: 22px;
    font-weight: 600;
    letter-spacing: -0.4px;
}

.welcome p {
    font-size: 14px;
    color: var(--text-muted);
    max-width: 420px;
    line-height: 1.6;
}

.welcome .examples {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    justify-content: center;
    margin-top: 8px;
}

.welcome .example-chip {
    padding: 8px 14px;
    background: var(--bg-tertiary);
    border: 1px solid var(--border);
    border-radius: 20px;
    font-size: 12px;
    color: var(--text-secondary);
    cursor: pointer;
    transition: all 0.15s;
}

.welcome .example-chip:hover {
    border-color: var(--accent-dim);
    color: var(--text-primary);
    background: var(--accent-glow);
}

/* Messages */
.message {
    max-width: 800px;
    width: 100%;
    margin: 0 auto;
    animation: fadeIn 0.2s ease;
}

@keyframes fadeIn {
    from { opacity: 0; transform: translateY(6px); }
    to { opacity: 1; transform: translateY(0); }
}

.message.user {
    display: flex;
    justify-content: flex-end;
}

.message.user .bubble {
    background: var(--accent-dim);
    color: white;
    padding: 10px 16px;
    border-radius: 16px 16px 4px 16px;
    max-width: 70%;
    font-size: 14px;
    line-height: 1.5;
    word-break: break-word;
}

.message.agent .bubble {
    background: var(--bg-tertiary);
    border: 1px solid var(--border);
    border-radius: 4px 16px 16px 16px;
    padding: 0;
    overflow: hidden;
    width: 100%;
}

.message.agent .bubble-header {
    padding: 10px 16px;
    font-size: 12px;
    font-weight: 600;
    color: var(--accent);
    display: flex;
    align-items: center;
    gap: 6px;
    border-bottom: 1px solid var(--border);
    text-transform: uppercase;
    letter-spacing: 0.5px;
}

.message.agent .terminal {
    padding: 12px 16px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 12px;
    line-height: 1.6;
    white-space: pre-wrap;
    word-break: break-all;
    max-height: 500px;
    overflow-y: auto;
    color: var(--stdout-color);
}

.message.agent .terminal .stderr { color: var(--stderr-color); }

.message.agent .result-bar {
    padding: 8px 16px;
    border-top: 1px solid var(--border);
    font-size: 12px;
    font-weight: 600;
    display: flex;
    align-items: center;
    gap: 6px;
}

.message.agent .result-bar.pass { color: var(--success); }
.message.agent .result-bar.fail { color: var(--error); }
.message.agent .result-bar.running {
    color: var(--accent);
    animation: pulse 1.5s ease-in-out infinite;
}

/* Attachments display */
.attachments-display {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
    margin-bottom: 4px;
}

.attachment-tag {
    padding: 4px 10px;
    background: var(--bg-tertiary);
    border: 1px solid var(--border);
    border-radius: 14px;
    font-size: 11px;
    color: var(--text-secondary);
    display: flex;
    align-items: center;
    gap: 4px;
}

.attachment-tag .remove {
    cursor: pointer;
    opacity: 0.5;
    font-size: 14px;
    line-height: 1;
}

.attachment-tag .remove:hover { opacity: 1; color: var(--error); }

/* --- Input area --- */
.input-area {
    padding: 16px 24px 20px;
    border-top: 1px solid var(--border);
    flex-shrink: 0;
}

.input-wrapper {
    max-width: 800px;
    margin: 0 auto;
    background: var(--bg-input);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 4px;
    display: flex;
    flex-direction: column;
    transition: border-color 0.2s;
}

.input-wrapper:focus-within { border-color: var(--border-focus); }

.input-row {
    display: flex;
    align-items: flex-end;
    gap: 4px;
}

.input-actions {
    display: flex;
    padding: 4px 8px;
    gap: 2px;
}

.input-action-btn {
    width: 36px;
    height: 36px;
    background: none;
    border: none;
    border-radius: 8px;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--text-muted);
    transition: all 0.12s;
}

.input-action-btn:hover { background: var(--bg-tertiary); color: var(--text-secondary); }

#promptInput {
    flex: 1;
    background: none;
    border: none;
    outline: none;
    color: var(--text-primary);
    font-family: inherit;
    font-size: 14px;
    line-height: 1.5;
    padding: 10px 4px;
    resize: none;
    min-height: 24px;
    max-height: 200px;
}

#promptInput::placeholder { color: var(--text-muted); }

.send-btn {
    width: 36px;
    height: 36px;
    background: var(--accent);
    border: none;
    border-radius: 8px;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    color: white;
    transition: all 0.15s;
    margin: 4px 8px 4px 0;
    flex-shrink: 0;
}

.send-btn:hover { background: var(--accent-dim); transform: scale(1.05); }
.send-btn:disabled { opacity: 0.3; cursor: not-allowed; transform: none; }

/* Hidden file input */
#fileInput { display: none; }

/* Scrollbar */
::-webkit-scrollbar { width: 6px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: var(--text-muted); }

/* Responsive */
@media (max-width: 768px) {
    .sidebar { display: none; }
}
</style>
</head>
<body>
<div class="app">
    <!-- Sidebar -->
    <aside class="sidebar">
        <div class="sidebar-header">
            <div class="logo">V</div>
            <h1>VerifyBot</h1>
            <span class="version">v2</span>
        </div>

        <button class="new-chat-btn" onclick="newSession()">
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><line x1="8" y1="3" x2="8" y2="13"/><line x1="3" y1="8" x2="13" y2="8"/></svg>
            New Session
        </button>

        <div class="sidebar-tabs">
            <button class="sidebar-tab active" onclick="switchTab('history', this)">History</button>
            <button class="sidebar-tab" onclick="switchTab('programs', this)">Programs</button>
            <button class="sidebar-tab" onclick="switchTab('outputs', this)">Outputs</button>
        </div>

        <div class="sidebar-list" id="sidebarList"></div>

        <div class="sidebar-footer">
            <div class="settings-grid">
                <div class="setting-item">
                    <label>Target</label>
                    <select id="targetSelect">
                        <option value="auto">Auto-detect</option>
                        <option value="local">Local</option>
                        <option value="raspi">Raspberry Pi</option>
                    </select>
                </div>
                <div class="setting-item">
                    <label>Retries</label>
                    <input type="number" id="retriesInput" value="3" min="1" max="10">
                </div>
                <div class="setting-item">
                    <label>Timeout</label>
                    <input type="number" id="timeoutInput" value="30" min="5" max="600">
                </div>
                <div class="setting-item">
                    <label>Browser</label>
                    <select id="headlessSelect">
                        <option value="visible">Visible</option>
                        <option value="headless">Headless</option>
                    </select>
                </div>
            </div>
        </div>
    </aside>

    <!-- Main -->
    <main class="main">
        <header class="main-header">
            <div class="status-dot ready" id="statusDot"></div>
            <span class="status-text" id="statusText">Ready</span>
            <button class="stop-btn" id="stopBtn" onclick="stopPipeline()">Stop</button>
        </header>

        <div class="chat-container" id="chatContainer">
            <div class="welcome" id="welcome">
                <div class="icon">V</div>
                <h2>VerifyBot Agent</h2>
                <p>Describe what you want built or debugged. Code gets written, deployed, and tested automatically.</p>
                <div class="examples">
                    <div class="example-chip" onclick="useExample(this)">write a fizzbuzz script</div>
                    <div class="example-chip" onclick="useExample(this)">make a random number generator</div>
                    <div class="example-chip" onclick="useExample(this)">read I2C sensor data on raspi</div>
                    <div class="example-chip" onclick="useExample(this)">blink GPIO 17 LED</div>
                </div>
            </div>
        </div>

        <div class="input-area">
            <div id="attachmentsDisplay" class="attachments-display"></div>
            <div class="input-wrapper">
                <div class="input-row">
                    <div class="input-actions">
                        <button class="input-action-btn" onclick="document.getElementById('fileInput').click()" title="Attach files">
                            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M21.44 11.05l-9.19 9.19a6 6 0 01-8.49-8.49l9.19-9.19a4 4 0 015.66 5.66l-9.2 9.19a2 2 0 01-2.83-2.83l8.49-8.48"/></svg>
                        </button>
                    </div>
                    <textarea id="promptInput" rows="1" placeholder="Describe what you want built or debugged..." onkeydown="handleKey(event)" oninput="autoResize(this)"></textarea>
                    <button class="send-btn" id="sendBtn" onclick="sendPrompt()">
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
                    </button>
                </div>
            </div>
            <input type="file" id="fileInput" multiple onchange="handleFiles(this.files)">
        </div>
    </main>
</div>

<script>
// --- State ---
const socket = io();
let isRunning = false;
let currentTerminalEl = null;
let attachedFiles = [];

// --- Socket events ---
socket.on('connect', () => {
    console.log('Connected to VerifyBot server');
    loadHistory();
});

socket.on('status', (data) => {
    updateStatus(data.running ? 'running' : 'ready');
});

socket.on('pipeline_started', (data) => {
    isRunning = true;
    updateStatus('running');
    addAgentMessage(data.prompt);
});

socket.on('pipeline_output', (data) => {
    if (currentTerminalEl) {
        const span = document.createElement('span');
        if (data.stream === 'stderr') span.className = 'stderr';
        span.textContent = data.text;
        currentTerminalEl.appendChild(span);
        currentTerminalEl.scrollTop = currentTerminalEl.scrollHeight;
        // Also scroll chat
        const chat = document.getElementById('chatContainer');
        chat.scrollTop = chat.scrollHeight;
    }
});

socket.on('pipeline_complete', (data) => {
    isRunning = false;
    updateStatus('ready');
    if (currentTerminalEl) {
        const msg = currentTerminalEl.closest('.message');
        const bar = msg.querySelector('.result-bar');
        if (bar) {
            bar.className = 'result-bar ' + (data.success ? 'pass' : 'fail');
            bar.innerHTML = (data.success ? '&#10003; ' : '&#10007; ') +
                            (data.success ? 'PASS — Task completed successfully' : 'FAIL — Max retries reached');
        }
    }
    loadHistory();
});

socket.on('pipeline_error', (data) => {
    isRunning = false;
    updateStatus('error');
    if (currentTerminalEl) {
        const span = document.createElement('span');
        span.className = 'stderr';
        span.textContent = '\\n[ERROR] ' + data.error + '\\n';
        currentTerminalEl.appendChild(span);
        const msg = currentTerminalEl.closest('.message');
        const bar = msg.querySelector('.result-bar');
        if (bar) {
            bar.className = 'result-bar fail';
            bar.textContent = 'ERROR — ' + data.error;
        }
    }
    setTimeout(() => updateStatus('ready'), 3000);
});

// --- UI functions ---
function updateStatus(state) {
    const dot = document.getElementById('statusDot');
    const text = document.getElementById('statusText');
    const stop = document.getElementById('stopBtn');
    const send = document.getElementById('sendBtn');

    dot.className = 'status-dot ' + state;
    text.textContent = state === 'running' ? 'Pipeline running...' :
                        state === 'error' ? 'Error occurred' : 'Ready';
    stop.className = 'stop-btn' + (state === 'running' ? ' visible' : '');
    send.disabled = state === 'running';
}

function sendPrompt() {
    const input = document.getElementById('promptInput');
    const prompt = input.value.trim();
    if (!prompt || isRunning) return;

    // Upload any attached files first
    if (attachedFiles.length > 0) {
        const formData = new FormData();
        attachedFiles.forEach((f, i) => formData.append('file_' + i, f));
        fetch('/api/upload', { method: 'POST', body: formData });
    }

    // Hide welcome
    const welcome = document.getElementById('welcome');
    if (welcome) welcome.style.display = 'none';

    // Add user message
    addUserMessage(prompt);

    // Send to server
    socket.emit('run_pipeline', {
        prompt: prompt,
        target: document.getElementById('targetSelect').value,
        max_retries: document.getElementById('retriesInput').value,
        timeout: document.getElementById('timeoutInput').value,
        headless: document.getElementById('headlessSelect').value === 'headless',
        attachments: attachedFiles.map(f => f.name),
    });

    // Clear input
    input.value = '';
    input.style.height = 'auto';
    attachedFiles = [];
    updateAttachmentDisplay();
}

function addUserMessage(text) {
    const chat = document.getElementById('chatContainer');
    const div = document.createElement('div');
    div.className = 'message user';
    div.innerHTML = '<div class="bubble">' + escapeHtml(text) + '</div>';
    chat.appendChild(div);
    chat.scrollTop = chat.scrollHeight;
}

function addAgentMessage(prompt) {
    const chat = document.getElementById('chatContainer');
    const div = document.createElement('div');
    div.className = 'message agent';
    div.innerHTML = `
        <div class="bubble">
            <div class="bubble-header">
                <svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor"><circle cx="8" cy="8" r="8"/></svg>
                Agent Pipeline
            </div>
            <div class="terminal"></div>
            <div class="result-bar running">Running...</div>
        </div>
    `;
    chat.appendChild(div);
    currentTerminalEl = div.querySelector('.terminal');
    chat.scrollTop = chat.scrollHeight;
}

function stopPipeline() {
    socket.emit('stop_pipeline');
}

function newSession() {
    const chat = document.getElementById('chatContainer');
    // Keep welcome, clear messages
    const welcome = document.getElementById('welcome');
    chat.innerHTML = '';
    if (welcome) {
        chat.appendChild(welcome);
        welcome.style.display = '';
    } else {
        // Recreate welcome
        chat.innerHTML = `
            <div class="welcome" id="welcome">
                <div class="icon">V</div>
                <h2>VerifyBot Agent</h2>
                <p>Describe what you want built or debugged. Code gets written, deployed, and tested automatically.</p>
                <div class="examples">
                    <div class="example-chip" onclick="useExample(this)">write a fizzbuzz script</div>
                    <div class="example-chip" onclick="useExample(this)">make a random number generator</div>
                    <div class="example-chip" onclick="useExample(this)">read I2C sensor data on raspi</div>
                    <div class="example-chip" onclick="useExample(this)">blink GPIO 17 LED</div>
                </div>
            </div>
        `;
    }
    currentTerminalEl = null;
}

function useExample(el) {
    document.getElementById('promptInput').value = el.textContent;
    document.getElementById('promptInput').focus();
}

function handleKey(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendPrompt();
    }
}

function autoResize(el) {
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 200) + 'px';
}

function handleFiles(fileList) {
    for (const f of fileList) {
        attachedFiles.push(f);
    }
    updateAttachmentDisplay();
    document.getElementById('fileInput').value = '';
}

function updateAttachmentDisplay() {
    const container = document.getElementById('attachmentsDisplay');
    container.innerHTML = attachedFiles.map((f, i) =>
        `<div class="attachment-tag">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M13 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V9z"/><polyline points="13 2 13 9 20 9"/></svg>
            ${escapeHtml(f.name)}
            <span class="remove" onclick="removeFile(${i})">&times;</span>
        </div>`
    ).join('');
}

function removeFile(i) {
    attachedFiles.splice(i, 1);
    updateAttachmentDisplay();
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

// --- Sidebar ---
function switchTab(tab, btn) {
    document.querySelectorAll('.sidebar-tab').forEach(t => t.classList.remove('active'));
    btn.classList.add('active');
    if (tab === 'history') loadHistory();
    else if (tab === 'programs') loadPrograms();
    else if (tab === 'outputs') loadOutputs();
}

async function loadHistory() {
    try {
        const res = await fetch('/api/history');
        const data = await res.json();
        const list = document.getElementById('sidebarList');
        list.innerHTML = data.map(item =>
            `<div class="sidebar-item" title="${escapeHtml(item.prompt)}">
                ${escapeHtml(item.prompt)}
                <div class="meta">${item.date}</div>
            </div>`
        ).join('') || '<div class="sidebar-item" style="color:var(--text-muted)">No runs yet</div>';
    } catch(e) { console.error(e); }
}

async function loadPrograms() {
    try {
        const res = await fetch('/api/programs');
        const data = await res.json();
        const list = document.getElementById('sidebarList');
        list.innerHTML = data.map(item =>
            `<div class="sidebar-item">
                ${escapeHtml(item.name)}
                <div class="meta">${item.modified} &middot; ${(item.size/1024).toFixed(1)}KB</div>
            </div>`
        ).join('') || '<div class="sidebar-item" style="color:var(--text-muted)">No programs yet</div>';
    } catch(e) { console.error(e); }
}

async function loadOutputs() {
    try {
        const res = await fetch('/api/outputs');
        const data = await res.json();
        const list = document.getElementById('sidebarList');
        list.innerHTML = data.map(item =>
            `<div class="sidebar-item">
                ${escapeHtml(item.name)}
                <div class="meta">${item.modified} &middot; ${(item.size/1024).toFixed(1)}KB</div>
            </div>`
        ).join('') || '<div class="sidebar-item" style="color:var(--text-muted)">No outputs yet</div>';
    } catch(e) { console.error(e); }
}

// Also support paste for images
document.addEventListener('paste', (e) => {
    const items = e.clipboardData?.items;
    if (!items) return;
    for (const item of items) {
        if (item.type.startsWith('image/')) {
            const file = item.getAsFile();
            if (file) {
                attachedFiles.push(file);
                updateAttachmentDisplay();
            }
        }
    }
});

// Also support drag and drop
const appEl = document.querySelector('.app');
appEl.addEventListener('dragover', (e) => { e.preventDefault(); });
appEl.addEventListener('drop', (e) => {
    e.preventDefault();
    if (e.dataTransfer.files.length > 0) {
        handleFiles(e.dataTransfer.files);
    }
});

// Init
loadHistory();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    port = int(os.environ.get("VERIFYBOT_PORT", 5000))
    host = "127.0.0.1"

    print()
    print("=" * 60)
    print("  VerifyBot Agent -- Web UI")
    print("=" * 60)
    print(f"  URL:  http://{host}:{port}")
    print(f"  Root: {ROOT}")
    print()
    print("  Opening browser...")
    print("  Press Ctrl+C to stop the server.")
    print("=" * 60)
    print()

    # Open browser after a short delay
    def open_browser():
        time.sleep(1.5)
        webbrowser.open(f"http://{host}:{port}")

    threading.Thread(target=open_browser, daemon=True).start()

    socketio.run(app, host=host, port=port, debug=False, allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()
