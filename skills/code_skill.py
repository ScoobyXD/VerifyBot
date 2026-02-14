#!/usr/bin/env python3
"""
code_skill.py — Extract, save, execute, and verify code from ChatGPT responses.

Usage:
    # Extract code blocks from a raw_md response
    python code_skill.py extract raw_md/response.md

    # Extract and run
    python code_skill.py extract raw_md/response.md --run

    # Run a previously extracted file
    python code_skill.py run ./programs/tictactoe.py

    # Full pipeline: prompt → extract → save to programs/ → run
    python code_skill.py pipeline "generate a python fizzbuzz"

    # Pipeline with verification loop (multi-turn, same conversation)
    python code_skill.py pipeline "generate a python fizzbuzz" --verify

    # Pipeline for C code (compiles with gcc)
    python code_skill.py pipeline "write a linked list in C" --verify

    # Review past runs
    python code_skill.py history
"""

import argparse
import json
import re
import subprocess
import sys
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import List, Optional

PROGRAMS_DIR = Path(__file__).resolve().parent.parent / "programs"
RAW_MD_DIR = Path(__file__).resolve().parent.parent / "raw_md"

# ---------------------------------------------------------------------------
# Code block extraction
# ---------------------------------------------------------------------------

FENCED_BLOCK_RE = re.compile(
    r"```(\w*)\s*\n"
    r"(?:Copy\s*code\s*\n)?"
    r"(.*?)"
    r"\n```",
    re.DOTALL
)

KNOWN_LANGS = (
    "python|bash|c|cpp|c\\+\\+|javascript|typescript|rust|java|sh|"
    "cmake|makefile|json|yaml|yml|html|css|xml|sql|go|ruby|perl|"
    "swift|kotlin|scala|r|matlab|lua|zig|toml|ini|dockerfile|"
    "plaintext|text|txt|assembly|asm|verilog|vhdl"
)

LOOSE_BLOCK_RE = re.compile(
    rf"^({KNOWN_LANGS})\s*\n"
    r"(?:Copy\s*code\s*\n)?"
    r"(.*?)"
    rf"(?=\n(?:{KNOWN_LANGS})\s*\n"
    r"|\n[0-9]+[️⃣]"
    r"|\n\d+\.\s+[A-Z]"
    r"|\nRun it:"
    r"|\nIf you want"
    r"|\nJust say"
    r"|\nWant to"
    r"|\nTell me"
    r"|\Z"
    r")",
    re.DOTALL | re.MULTILINE
)


class CodeBlock:
    """A single extracted code block."""
    def __init__(self, language: str, code: str, index: int):
        self.language = language.lower().strip() or "txt"
        self.code = code.strip()
        self.index = index

    @property
    def extension(self) -> str:
        ext_map = {
            "python": ".py", "py": ".py",
            "bash": ".sh", "sh": ".sh",
            "c": ".c", "cpp": ".cpp", "c++": ".cpp",
            "javascript": ".js", "js": ".js",
            "typescript": ".ts", "ts": ".ts",
            "rust": ".rs", "java": ".java",
            "cmake": ".cmake", "makefile": "",
            "json": ".json", "yaml": ".yaml", "yml": ".yaml",
            "html": ".html", "css": ".css", "txt": ".txt",
        }
        return ext_map.get(self.language, ".txt")

    def __repr__(self):
        preview = self.code[:60].replace("\n", "\\n")
        return f"CodeBlock({self.language}, {len(self.code)} chars, '{preview}...')"


def extract_code_blocks(text: str) -> List[CodeBlock]:
    """Extract all code blocks from a ChatGPT response."""
    blocks = []
    seen_code = set()

    for match in FENCED_BLOCK_RE.finditer(text):
        lang = match.group(1)
        code = match.group(2).strip()
        if code and code not in seen_code:
            seen_code.add(code)
            blocks.append(CodeBlock(lang, code, len(blocks)))

    if not blocks:
        for match in LOOSE_BLOCK_RE.finditer(text):
            lang = match.group(1)
            code = match.group(2).strip()
            if code and code not in seen_code:
                seen_code.add(code)
                blocks.append(CodeBlock(lang, code, len(blocks)))

    return blocks


def extract_filename_hint(text: str) -> Optional[str]:
    """Try to find a suggested filename in the response text."""
    patterns = [
        r"save\s+(?:it\s+)?as\s+[`\"']?(\S+\.\w+)[`\"']?",
        r"(?:file|name)\s+(?:it\s+)?(?:called|named)\s+[`\"']?(\S+\.\w+)[`\"']?",
        r"create\s+(?:a\s+)?(?:file\s+)?[`\"']?(\S+\.\w+)[`\"']?",
        # ChatGPT often puts filenames in bold or backticks at the start
        r"`(\w+\.\w+)`",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            fname = m.group(1).strip("`\"'")
            # Sanity check — must have a reasonable extension
            if "." in fname and len(fname) < 60:
                return fname
    return None


def extract_all_filename_hints(text: str) -> List[str]:
    """Extract ALL filename hints from the response, in order.
    
    Looks for patterns like:
    - **CAN.c** or **CAN.h** (bold filenames)
    - `main.c` (backtick filenames)
    - ### CAN.c or ### main.c (heading filenames)
    """
    hints = []
    seen = set()
    patterns = [
        r"\*\*(\w[\w.-]*\.\w+)\*\*",    # **filename.ext**
        r"###?\s+`?(\w[\w.-]*\.\w+)`?",  # ### filename.ext
        r"`(\w[\w.-]*\.\w+)`",            # `filename.ext`
    ]
    for pat in patterns:
        for m in re.finditer(pat, text):
            fname = m.group(1)
            if fname not in seen and len(fname) < 60:
                seen.add(fname)
                hints.append(fname)
    return hints


# ---------------------------------------------------------------------------
# File operations
# ---------------------------------------------------------------------------

def save_code_block(block: CodeBlock, dest_dir: Path, filename: Optional[str] = None) -> Path:
    """Save a code block to a file."""
    dest_dir.mkdir(parents=True, exist_ok=True)

    if not filename:
        filename = f"program_{block.index}{block.extension}"

    filepath = dest_dir / filename
    filepath.write_text(block.code, encoding="utf-8")
    print(f"  [SAVED] {filepath}  ({block.language}, {len(block.code)} chars)")
    return filepath


def compile_c_file(filepath: Path, compiler: str = "gcc", flags: list = None) -> dict:
    """Compile a .c or .cpp file and return the result."""
    filepath = Path(filepath).resolve()
    ext = filepath.suffix.lower()

    if ext == ".c":
        default_compiler = compiler or "gcc"
    elif ext == ".cpp":
        default_compiler = compiler or "g++"
    else:
        return {"success": False, "error": f"Not a C/C++ file: {ext}"}

    output_path = filepath.with_suffix("")  # strip .c/.cpp for binary name
    cmd = [default_compiler]
    if flags:
        cmd.extend(flags)
    else:
        cmd.extend(["-Wall", "-Wextra", "-o", str(output_path)])
    cmd.append(str(filepath))

    # Check for other .c/.h files in same directory that might be part of the project
    # (but don't auto-include — just the specified file)

    print(f"  [COMPILE] {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=filepath.parent,
        )
        output = {
            "success": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "binary": str(output_path) if result.returncode == 0 else None,
        }

        if result.returncode == 0:
            print(f"  [OK] Compiled to {output_path.name}")
        else:
            print(f"  [FAIL] Compilation failed (exit {result.returncode})")
            if result.stderr:
                print(f"  [STDERR]\n{result.stderr[:800]}")

        return output

    except FileNotFoundError:
        msg = f"Compiler not found: {default_compiler}"
        print(f"  [ERROR] {msg}")
        return {"success": False, "error": msg}
    except subprocess.TimeoutExpired:
        print(f"  [TIMEOUT] Compilation killed after 30s")
        return {"success": False, "error": "compilation timeout"}


def detect_missing_imports(filepath: Path) -> list:
    """Scan a Python file for imports and check which ones are missing."""
    if filepath.suffix != ".py":
        return []
    
    missing = []
    text = filepath.read_text(encoding="utf-8")
    
    # Match "import X" and "from X import Y"
    import_re = re.compile(r"^\s*(?:import|from)\s+([\w]+)", re.MULTILINE)
    stdlib_modules = {
        "os", "sys", "re", "math", "json", "csv", "time", "datetime",
        "pathlib", "subprocess", "shutil", "collections", "itertools",
        "functools", "typing", "io", "string", "random", "copy",
        "argparse", "logging", "unittest", "dataclasses", "abc",
        "contextlib", "traceback", "threading", "multiprocessing",
        "socket", "http", "urllib", "hashlib", "base64", "struct",
        "array", "bisect", "heapq", "statistics", "decimal", "fractions",
        "enum", "textwrap", "pprint", "tempfile", "glob", "fnmatch",
        "pickle", "shelve", "sqlite3", "xml", "html", "email",
        "configparser", "platform", "signal", "ctypes", "inspect",
        "ast", "dis", "code", "codeop", "operator", "weakref",
    }
    
    for match in import_re.finditer(text):
        module = match.group(1)
        if module in stdlib_modules:
            continue
        # Check if importable
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    
    return missing


def classify_program(filepath: Path) -> dict:
    """Static analysis to detect program behavior patterns.
    
    Returns hints about whether a program is likely long-running,
    which affects how we interpret timeouts.
    """
    try:
        code = filepath.read_text(encoding="utf-8")
    except Exception:
        return {"long_running": False, "reason": None}
    
    hints = {
        "long_running": False,
        "reason": None,
        "suggested_timeout": None,
    }
    
    # Patterns that indicate intentionally long-running programs
    long_running_patterns = [
        (r"\bwhile\s+True\b", "while True loop"),
        (r"\bwhile\s+1\b", "while 1 loop"),
        (r"\bfor\s+\w+\s+in\s+(?:itertools\.)?count\b", "infinite counter"),
        (r"\bsignal\.pause\b", "signal wait"),
        (r"\bserver\.serve_forever\b", "server"),
        (r"\bapp\.run\b", "web server"),
        (r"\bHTTPServer\b", "HTTP server"),
        (r"\basyncio\.run\b.*\bwhile\b", "async event loop"),
        (r"\btime\.sleep\b.*\bwhile\b", "polling loop"),
        (r"\bsched(?:uler)?\.run\b", "scheduler"),
        (r"\binput\s*\(", "waiting for user input"),
    ]
    
    for pattern, reason in long_running_patterns:
        if re.search(pattern, code, re.DOTALL):
            hints["long_running"] = True
            hints["reason"] = reason
            break
    
    # Check for file I/O patterns that suggest the program does something useful
    # even if killed (writes to file, prints output continuously)
    if re.search(r"\.(?:write|to_csv|dump)\s*\(", code):
        hints["produces_files"] = True
    
    return hints


def run_file_with_streaming(cmd: list, filepath: Path, timeout: int = 30) -> dict:
    """Run a command with streaming output capture and smart timeout handling.
    
    Instead of subprocess.run() which waits until completion or timeout,
    this uses Popen to capture output as it arrives. When a timeout hits,
    we can distinguish between:
    
    1. Program was producing output OR created files (working, just long-running) → timeout_ok
    2. Program produced nothing and created no files (likely hung/broken) → timeout_fail
    3. Program produced stderr only (crashing slowly) → timeout_fail
    """
    import threading
    
    stdout_lines = []
    stderr_lines = []
    
    # Snapshot files in the working directory BEFORE running, so we can
    # detect if the program created or modified files during execution.
    work_dir = filepath.parent
    pre_files = _snapshot_dir(work_dir)
    
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=work_dir,
        )
        
        # Read stdout/stderr in threads to avoid deadlock
        def read_stream(stream, target):
            for line in stream:
                target.append(line)
        
        t_out = threading.Thread(target=read_stream, args=(proc.stdout, stdout_lines))
        t_err = threading.Thread(target=read_stream, args=(proc.stderr, stderr_lines))
        t_out.daemon = True
        t_err.daemon = True
        t_out.start()
        t_err.start()
        
        # Wait for process with timeout
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            
            # Give threads a moment to finish reading
            t_out.join(timeout=2)
            t_err.join(timeout=2)
            
            stdout_text = "".join(stdout_lines)
            stderr_text = "".join(stderr_lines)
            
            # --- Smart timeout classification ---
            has_stdout = bool(stdout_text.strip())
            has_stderr = bool(stderr_text.strip())
            
            # Check if the program created or modified files (e.g. writing CSV)
            post_files = _snapshot_dir(work_dir)
            new_or_changed = _diff_snapshots(pre_files, post_files)
            has_file_output = bool(new_or_changed)
            
            # Classify the program's behavior
            hints = classify_program(filepath)
            
            # Program was producing output OR creating files → it was working
            if (has_stdout or has_file_output) and not has_stderr:
                print(f"  [TIMEOUT] Killed after {timeout}s — but program was producing output")
                if has_file_output:
                    print(f"  [FILES] Created/modified during execution: {', '.join(new_or_changed)}")
                if hints.get("long_running"):
                    print(f"  [INFO] Detected as long-running ({hints['reason']})")
                if has_stdout:
                    print(f"  [STDOUT] (last 500 chars)\n{stdout_text[-500:]}")
                return {
                    "success": True,
                    "timeout": True,
                    "timeout_ok": True,
                    "returncode": -1,
                    "stdout": stdout_text,
                    "stderr": stderr_text,
                    "files_created": new_or_changed,
                    "note": f"Program was working when killed after {timeout}s"
                           + (f" (created files: {', '.join(new_or_changed)})" if has_file_output else "")
                           + ". This is likely intentional behavior.",
                }
            elif (has_stdout or has_file_output) and has_stderr:
                # Had output/files and errors — ambiguous, but leaning toward working
                print(f"  [TIMEOUT] Killed after {timeout}s — produced both output and errors")
                if has_file_output:
                    print(f"  [FILES] Created/modified: {', '.join(new_or_changed)}")
                if has_stdout:
                    print(f"  [STDOUT] (last 300 chars)\n{stdout_text[-300:]}")
                print(f"  [STDERR] (last 300 chars)\n{stderr_text[-300:]}")
                return {
                    "success": True,
                    "timeout": True,
                    "timeout_ok": True,
                    "returncode": -1,
                    "stdout": stdout_text,
                    "stderr": stderr_text,
                    "files_created": new_or_changed,
                    "note": f"Program had output and warnings when killed after {timeout}s.",
                }
            else:
                # No stdout, no files created, or only stderr — this is a real problem
                print(f"  [TIMEOUT] Killed after {timeout}s — no useful output produced")
                if has_stderr:
                    print(f"  [STDERR]\n{stderr_text[:500]}")
                return {
                    "success": False,
                    "timeout": True,
                    "timeout_ok": False,
                    "returncode": -1,
                    "stdout": stdout_text,
                    "stderr": stderr_text,
                    "error": f"Timed out after {timeout}s with no output (possibly hung)",
                }
        
        # Process completed within timeout
        t_out.join(timeout=2)
        t_err.join(timeout=2)
        
        stdout_text = "".join(stdout_lines)
        stderr_text = "".join(stderr_lines)
        
        return {
            "success": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": stdout_text,
            "stderr": stderr_text,
        }
        
    except FileNotFoundError as e:
        print(f"  [ERROR] {e}")
        return {"success": False, "error": str(e)}


def _snapshot_dir(directory: Path) -> dict:
    """Take a snapshot of files in a directory (name → mtime+size).
    
    Only goes one level deep + one level of subdirs to catch things like
    output/ folders that programs commonly create.
    """
    snap = {}
    try:
        for item in directory.iterdir():
            if item.is_file() and not item.name.startswith("."):
                try:
                    stat = item.stat()
                    snap[str(item.relative_to(directory))] = (stat.st_mtime, stat.st_size)
                except OSError:
                    pass
            elif item.is_dir() and not item.name.startswith("."):
                # One level deep into subdirs
                try:
                    for sub in item.iterdir():
                        if sub.is_file():
                            try:
                                stat = sub.stat()
                                snap[str(sub.relative_to(directory))] = (stat.st_mtime, stat.st_size)
                            except OSError:
                                pass
                except OSError:
                    pass
    except OSError:
        pass
    return snap


def _diff_snapshots(before: dict, after: dict) -> list:
    """Compare two directory snapshots. Return list of new or modified file names."""
    changed = []
    for name, (mtime, size) in after.items():
        if name not in before:
            changed.append(name)
        elif before[name] != (mtime, size):
            changed.append(name)
    return changed


def run_file(filepath: Path, timeout: int = 30) -> dict:
    """Execute a file and capture output. Handles compilation for C/C++.
    
    Timeout behavior:
    - Programs that produce stdout before timeout → SUCCESS (they were working)
    - Programs that produce nothing before timeout → FAILURE (likely hung)
    - Long-running patterns (while True, servers) are detected and noted
    - Use --timeout to adjust the limit for your use case
    """
    filepath = Path(filepath).resolve()
    ext = filepath.suffix.lower()

    # --- C/C++: compile first, then run ---
    if ext in (".c", ".cpp"):
        compile_result = compile_c_file(filepath)
        if not compile_result["success"]:
            return compile_result

        binary = Path(compile_result["binary"])
        if not binary.exists():
            return {"success": False, "error": "Binary not found after compilation"}

        cmd = [str(binary)]
        print(f"  [RUN] {binary.name}")
        result = run_file_with_streaming(cmd, filepath, timeout)
        if result.get("success"):
            if result.get("stdout"):
                print(f"  [STDOUT]\n{result['stdout'][:500]}")
            if not result.get("timeout"):
                print(f"  [OK] Ran successfully")
        else:
            print(f"  [FAIL] Exit code: {result.get('returncode', '?')}")
        return result

    # --- Shell scripts: handle Windows ---
    if ext == ".sh":
        if os.name == "nt":
            wsl = shutil.which("wsl")
            git_bash = shutil.which("bash")
            if wsl:
                cmd = ["wsl", "bash", filepath.as_posix().replace("C:", "/mnt/c")]
            elif git_bash and "Git" in git_bash:
                cmd = [git_bash, str(filepath)]
            else:
                msg = (f"Cannot run .sh on Windows (no WSL/Git Bash). "
                       f"Contents: {filepath.read_text(encoding='utf-8').strip()[:200]}")
                print(f"  [SKIP] {msg}")
                return {"success": False, "error": msg, "skipped": False,
                        "suggestion": "Convert bash script to PowerShell or Python equivalent"}
        else:
            cmd = ["bash", str(filepath)]
        
        print(f"  [RUN] {' '.join(cmd)}")
        result = run_file_with_streaming(cmd, filepath, timeout)
        _print_run_result(result, timeout)
        return result

    # --- Python: check for missing imports first ---
    if ext == ".py":
        missing = detect_missing_imports(filepath)
        if missing:
            print(f"  [MISSING] Python modules not installed: {', '.join(missing)}")
            return {
                "success": False,
                "error": f"Missing modules: {', '.join(missing)}",
                "missing_imports": missing,
                "returncode": 1,
                "stdout": "",
                "stderr": f"ModuleNotFoundError: Missing modules: {', '.join(missing)}",
            }
        
        # Check if program is likely long-running and warn
        hints = classify_program(filepath)
        if hints.get("long_running"):
            print(f"  [INFO] Detected long-running pattern: {hints['reason']}")
            print(f"  [INFO] Will run for {timeout}s then evaluate output")
        
        cmd = [sys.executable, str(filepath)]
    elif ext in (".js", ".mjs"):
        cmd = ["node", str(filepath)]
    elif ext == ".h":
        print(f"  [SKIP] Header file {filepath.name} (not directly runnable)")
        return {"success": True, "stdout": "", "stderr": "", "returncode": 0, "skipped": True}
    else:
        print(f"  [SKIP] Don't know how to run {ext} files")
        return {"success": False, "error": f"Unknown extension: {ext}", "skipped": True}

    print(f"  [RUN] {' '.join(cmd)}")
    result = run_file_with_streaming(cmd, filepath, timeout)
    _print_run_result(result, timeout)
    return result


def _print_run_result(result: dict, timeout: int):
    """Print run results consistently."""
    if result.get("timeout") and result.get("timeout_ok"):
        # Already printed by run_file_with_streaming
        return
    
    if result.get("stdout"):
        print(f"  [STDOUT]\n{result['stdout'][:500]}")
    if result.get("stderr"):
        print(f"  [STDERR]\n{result['stderr'][:500]}")
    if result.get("success") and not result.get("timeout"):
        print(f"  [OK] Ran successfully")
    elif not result.get("success") and not result.get("timeout"):
        print(f"  [FAIL] Exit code: {result.get('returncode', '?')}")


# ---------------------------------------------------------------------------
# Result summarization (for JSON embedding in md)
# ---------------------------------------------------------------------------

def _summarize_results(run_results: list) -> list:
    """Create a JSON-serializable summary of run results.
    
    Truncates stdout/stderr to keep the JSON block readable.
    """
    summaries = []
    for r in run_results:
        if r is None:
            continue
        s = {
            "success": r.get("success"),
            "returncode": r.get("returncode"),
        }
        if r.get("skipped"):
            s["skipped"] = True
        if r.get("timeout"):
            s["timeout"] = True
            s["timeout_ok"] = r.get("timeout_ok", False)
        if r.get("error"):
            s["error"] = r["error"][:300]
        if r.get("files_created"):
            s["files_created"] = r["files_created"]
        if r.get("missing_imports"):
            s["missing_imports"] = r["missing_imports"]
        if r.get("stdout"):
            s["stdout_chars"] = len(r["stdout"])
            s["stdout_preview"] = r["stdout"][:200].strip()
        if r.get("stderr"):
            s["stderr_chars"] = len(r["stderr"])
            s["stderr_preview"] = r["stderr"][:200].strip()
        if r.get("note"):
            s["note"] = r["note"]
        summaries.append(s)
    return summaries


# ---------------------------------------------------------------------------
# Feedback prompt
# ---------------------------------------------------------------------------

def build_feedback_prompt(code_files: list, run_results: list) -> str:
    """Build a concise follow-up prompt for the SAME conversation.
    
    Since we're in the same ChatGPT chat, it already knows what it generated.
    We only need to send the errors.
    
    IMPORTANT: Uses plain text only — no markdown formatting, no triple
    backticks, no bold, no headings. Markdown gets mangled/lost when typed
    into ChatGPT's contenteditable input via Playwright.
    """
    lines = []
    lines.append("The code you just gave me has errors. Here are the execution results:")
    lines.append("")

    for code_file, result in zip(code_files, run_results):
        if result is None or result.get("skipped"):
            continue
        if result.get("success"):
            if result.get("timeout_ok"):
                lines.append(f"{code_file.name}: OK, produced output before timeout (long-running program)")
            else:
                lines.append(f"{code_file.name}: OK, ran successfully")
            continue

        lines.append(f"--- {code_file.name} FAILED ---")
        if result.get("returncode") is not None:
            lines.append(f"Exit code: {result['returncode']}")
        if result.get("stderr"):
            stderr = result['stderr'].strip()[:2000]
            lines.append(f"STDERR:")
            lines.append(stderr)
        if result.get("stdout"):
            stdout = result['stdout'].strip()[:500]
            lines.append(f"STDOUT:")
            lines.append(stdout)
        if result.get("error"):
            lines.append(f"Error: {result['error']}")
        if result.get("missing_imports"):
            lines.append(f"Missing modules: {', '.join(result['missing_imports'])}")
        lines.append("")

    lines.append("IMPORTANT: Do not use any third-party libraries unless absolutely necessary.")
    lines.append("If you must use external packages, list them at the top of your response")
    lines.append("in this exact format: DEPENDENCIES: package1, package2, package3")
    lines.append("If no external packages are needed, do NOT include a DEPENDENCIES line at all.")
    lines.append("")
    lines.append("Please fix the code. Return the complete corrected version, not just the changes.")

    return "\n".join(lines)


def build_feedback_prompt_standalone(raw_md_path: Path, code_files: list, run_results: list) -> str:
    """Build a full feedback prompt for a NEW conversation (no prior context).
    
    Used when session is not available (fallback to old behavior).
    """
    raw_md_content = raw_md_path.read_text(encoding="utf-8")

    sections = []
    sections.append("I ran the code you generated and got errors. Here's the full context:\n")
    sections.append("## YOUR PREVIOUS RESPONSE (raw_md)")
    sections.append(f"File: {raw_md_path.name}")
    sections.append("```")
    sections.append(raw_md_content)
    sections.append("```\n")

    sections.append("## EXECUTION RESULTS")
    for code_file, result in zip(code_files, run_results):
        sections.append(f"### File: {code_file.name}")
        sections.append("```")
        sections.append(code_file.read_text(encoding="utf-8"))
        sections.append("```")

        if result:
            sections.append(f"**Exit code**: {result.get('returncode', 'N/A')}")
            if result.get("stdout"):
                sections.append(f"**stdout**:\n```\n{result['stdout']}\n```")
            if result.get("stderr"):
                sections.append(f"**stderr**:\n```\n{result['stderr']}\n```")
            if result.get("error"):
                sections.append(f"**error**: {result['error']}")
        else:
            sections.append("*(not runnable / skipped)*")
        sections.append("")

    sections.append("## INSTRUCTIONS")
    sections.append("Please fix the code so it runs without errors. "
                     "Return the complete corrected code, not just the changes.")

    return "\n".join(sections)


# ---------------------------------------------------------------------------
# High-level commands
# ---------------------------------------------------------------------------

def cmd_extract(response_path: str, dest: str = None, run: bool = False):
    """Extract code blocks from a response file, optionally save and run."""
    rpath = Path(response_path)
    if not rpath.exists():
        print(f"[ERROR] File not found: {rpath}")
        return

    text = rpath.read_text(encoding="utf-8")
    blocks = extract_code_blocks(text)

    if not blocks:
        print("[WARN] No code blocks found in response.")
        return

    print(f"[OK] Found {len(blocks)} code block(s):")
    for b in blocks:
        print(f"  [{b.index}] {b.language} — {len(b.code)} chars")

    dest_dir = Path(dest) if dest else PROGRAMS_DIR

    # Try to match filename hints to blocks
    hints = extract_all_filename_hints(text)
    single_hint = extract_filename_hint(text)
    saved_files = []

    for b in blocks:
        # Try to match by extension
        fname = None
        if hints:
            for h in hints:
                h_ext = Path(h).suffix
                if h_ext == b.extension:
                    fname = h
                    hints.remove(h)
                    break
        if not fname and b.index == 0 and single_hint:
            fname = single_hint

        fp = save_code_block(b, dest_dir, filename=fname)
        saved_files.append(fp)

    if run and saved_files:
        print("\n--- Running extracted code ---")
        for fp in saved_files:
            run_file(fp)


def cmd_run(filepath: str, timeout: int = 30):
    """Run a file directly."""
    run_file(Path(filepath), timeout=timeout)


def cmd_pipeline(prompt: str, dest: str = None, run: bool = True,
                 headed: bool = False, verify: bool = False, max_retries: int = 3,
                 timeout: int = 30):
    """Full pipeline: prompt ChatGPT → extract → save → run → [verify loop].
    
    With --verify: uses a persistent browser session and multi-turn conversation.
    If code fails, sends ONLY the errors back (ChatGPT remembers context).
    
    All activity (prompts, responses, extraction, compilation, execution,
    feedback) is logged into the raw_md file for full audit trail.
    """
    from skills.chatgpt_skill import save_response, append_to_log, ensure_dirs
    from core.session import ChatGPTSession

    ensure_dirs()
    dest_dir = Path(dest) if dest else PROGRAMS_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 50)
    mode = "prompt → extract → save → run"
    if verify:
        mode += " → verify (multi-turn)"
    print(f"PIPELINE: {mode}")
    print("=" * 50)

    # We'll track the "master" raw_md file for the entire pipeline run.
    # Attempt 1 creates it; retries append to it (and also create their own).
    master_md_path = None

    # Structured run record — gets embedded as JSON into the master md file at the end
    run_log = {
        "prompt": prompt,
        "started_at": datetime.now().isoformat(),
        "verify": verify,
        "max_retries": max_retries,
        "platform": "windows" if os.name == "nt" else "linux",
        "attempts": [],
    }

    with ChatGPTSession(headed=headed) as session:
        attempt = 0

        # Prepend platform context to the initial prompt so ChatGPT
        # knows what environment the code will run in
        platform_prefix = ""
        if os.name == "nt":
            platform_prefix = (
                "[SYSTEM CONTEXT: Code will run on Windows with Python. "
                "Do NOT use bash/shell scripts — use Python or PowerShell instead. "
                "Avoid Linux-only tools. Prefer stdlib modules over third-party packages. "
                "If third-party packages are required, list them at the top of your response "
                "in this exact format: DEPENDENCIES: package1, package2, package3 "
                "If no external packages are needed, do NOT include a DEPENDENCIES line.]\n\n"
            )
        else:
            platform_prefix = (
                "[SYSTEM CONTEXT: Code will run on Linux with Python. "
                "Prefer stdlib modules over third-party packages. "
                "If third-party packages are required, list them at the top of your response "
                "in this exact format: DEPENDENCIES: package1, package2, package3 "
                "If no external packages are needed, do NOT include a DEPENDENCIES line.]\n\n"
            )

        augmented_prompt = platform_prefix + prompt

        while attempt <= max_retries:
            attempt += 1
            is_retry = attempt > 1
            attempt_log = {
                "attempt": attempt,
                "is_retry": is_retry,
                "timestamp": datetime.now().isoformat(),
            }

            if is_retry:
                print(f"\n{'='*50}")
                print(f"RETRY {attempt - 1}/{max_retries}: Sending fix request (same conversation)...")
                print(f"{'='*50}")

            # Step 1: Send prompt
            print(f"\n[1/4] {'Re-prompting' if is_retry else 'Prompting'} ChatGPT...")
            if is_retry:
                response = session.followup(current_prompt)
            else:
                response = session.prompt(augmented_prompt)

            # Save the response to raw_md
            prompt_label = prompt if not is_retry else f"(verify followup #{attempt - 1})"
            raw_md_path = save_response(prompt_label, response,
                                        attempt=attempt, is_retry=is_retry)

            # Log the actual prompt that was sent (including platform prefix / feedback)
            if is_retry:
                append_to_log(raw_md_path, "Prompt Sent to ChatGPT (Feedback)",
                              f"```\n{current_prompt}\n```")
            else:
                append_to_log(raw_md_path, "Prompt Sent to ChatGPT (Initial)",
                              f"```\n{augmented_prompt}\n```")

            # On first attempt, this becomes the master log file
            if master_md_path is None:
                master_md_path = raw_md_path

            # For retries, also append to the master md so it has the full history
            if is_retry and master_md_path and master_md_path != raw_md_path:
                append_to_log(master_md_path,
                              f"Retry {attempt - 1}/{max_retries} — ChatGPT Response",
                              f"**Feedback prompt sent:**\n```\n{current_prompt}\n```\n\n"
                              f"**ChatGPT response:**\n\n{response}")

            # Step 2: Extract code
            print(f"\n[2/4] Extracting code blocks...")
            blocks = extract_code_blocks(response)
            if not blocks:
                print("[WARN] No code blocks in response. Raw response saved in raw_md/")
                append_to_log(raw_md_path, "Code Extraction",
                              "**No code blocks found in response.**")
                if master_md_path and master_md_path != raw_md_path:
                    append_to_log(master_md_path, f"Attempt {attempt} — Code Extraction",
                                  "No code blocks found in response.")
                attempt_log["status"] = "no_code_blocks"
                run_log["attempts"].append(attempt_log)
                break

            extraction_info = f"Found **{len(blocks)}** code block(s):\n"
            for b in blocks:
                extraction_info += f"- `[{b.index}]` {b.language} — {len(b.code)} chars\n"
            print(f"  Found {len(blocks)} block(s)")
            append_to_log(raw_md_path, "Code Extraction", extraction_info)
            attempt_log["blocks"] = len(blocks)

            # Check if ChatGPT declared dependencies
            dep_match = re.search(r"DEPENDENCIES:\s*(.+)", response, re.IGNORECASE)
            if dep_match:
                raw_deps = dep_match.group(1).strip()
                # Filter out sentinel values ChatGPT uses to say "no deps"
                _no_dep_sentinels = {"none", "(none)", "n/a", "no", "null", "-", "0", ""}
                declared_deps = [
                    d.strip() for d in raw_deps.split(",")
                    if d.strip() and d.strip().lower().strip("()") not in _no_dep_sentinels
                ]
                if declared_deps:
                    print(f"\n[DEPS] ChatGPT declared dependencies: {', '.join(declared_deps)}")
                    answer = input(f"  Install them with pip? (y/n): ").strip().lower()
                    dep_log = f"ChatGPT declared: {', '.join(declared_deps)}\n"
                    if answer in ("y", "yes"):
                        for pkg in declared_deps:
                            print(f"  [INSTALL] pip install {pkg}")
                            install_result = subprocess.run(
                                [sys.executable, "-m", "pip", "install", pkg],
                                capture_output=True, text=True, timeout=120,
                            )
                            if install_result.returncode == 0:
                                print(f"  [OK] Installed {pkg}")
                                dep_log += f"- Installed {pkg}: OK\n"
                            else:
                                print(f"  [FAIL] Could not install {pkg}: {install_result.stderr[:200]}")
                                dep_log += f"- Installed {pkg}: FAILED — {install_result.stderr[:200]}\n"
                    else:
                        dep_log += "User declined to install.\n"
                    append_to_log(raw_md_path, "Dependency Installation", dep_log)

            # Step 3: Save to programs/
            if is_retry:
                for old in dest_dir.glob("program_*"):
                    old.unlink()

            hints = extract_all_filename_hints(response)
            single_hint = extract_filename_hint(response)
            saved_files = []

            for b in blocks:
                fname = None
                if hints:
                    for h in list(hints):
                        if Path(h).suffix == b.extension:
                            fname = h
                            hints.remove(h)
                            break
                if not fname and b.index == 0 and single_hint:
                    fname = single_hint
                fp = save_code_block(b, dest_dir, filename=fname)
                saved_files.append(fp)

            save_info = "Saved files:\n"
            for fp in saved_files:
                save_info += f"- `{fp}`\n"
            append_to_log(raw_md_path, "Saved Files", save_info)
            attempt_log["files"] = [str(f) for f in saved_files]

            # Step 4: Run
            if not run:
                print("\n[DONE] Pipeline complete (no execution requested).")
                append_to_log(raw_md_path, "Execution", "Skipped (no execution requested).")
                attempt_log["status"] = "no_run"
                run_log["attempts"].append(attempt_log)
                break

            print(f"\n[3/4] Running...")
            run_results = []
            all_passed = True
            any_ran = False  # Track if at least one file actually executed
            execution_log = ""

            for fp in saved_files:
                result = run_file(fp, timeout=timeout)
                run_results.append(result)

                # Build execution log for the md file
                execution_log += f"### `{fp.name}`\n"
                if result.get("skipped"):
                    execution_log += "Skipped (header file / not directly runnable)\n\n"
                    continue

                any_ran = True
                if result.get("success"):
                    if result.get("timeout_ok"):
                        execution_log += f"**Result:** SUCCESS (produced output before {timeout}s timeout)\n"
                    else:
                        execution_log += f"**Result:** SUCCESS (exit code 0)\n"
                else:
                    execution_log += f"**Result:** FAILED (exit code {result.get('returncode', '?')})\n"
                    all_passed = False

                if result.get("stdout"):
                    stdout_preview = result['stdout'][:2000]
                    execution_log += f"\n**stdout:**\n```\n{stdout_preview}\n```\n"
                if result.get("stderr"):
                    stderr_preview = result['stderr'][:2000]
                    execution_log += f"\n**stderr:**\n```\n{stderr_preview}\n```\n"
                if result.get("error"):
                    execution_log += f"\n**error:** {result['error']}\n"
                if result.get("note"):
                    execution_log += f"\n**note:** {result['note']}\n"
                execution_log += "\n"
            
            # If nothing actually ran (all skipped/unknown), that's not success
            if not any_ran:
                print(f"\n[WARN] No files were actually executed (all skipped or unrunnable)")
                execution_log += "\n**Warning:** No files were actually executed (all skipped or unrunnable)\n"
                all_passed = False

            append_to_log(raw_md_path, "Execution Results", execution_log)

            if all_passed and any_ran:
                print(f"\n[DONE] All code ran successfully!")
                append_to_log(raw_md_path, "Pipeline Status", "**SUCCESS** — All code ran successfully.")
                if master_md_path and master_md_path != raw_md_path:
                    append_to_log(master_md_path, f"Attempt {attempt} — Result", "SUCCESS")
                attempt_log["status"] = "success"
                attempt_log["results"] = _summarize_results(run_results)
                run_log["attempts"].append(attempt_log)
                break

            # --- Code failed ---
            attempt_log["status"] = "failed"
            attempt_log["results"] = _summarize_results(run_results)

            # --- Check for missing dependencies and offer to install ---
            missing_deps = set()
            for result in run_results:
                if result.get("missing_imports"):
                    missing_deps.update(result["missing_imports"])
            
            if missing_deps:
                print(f"\n[DEPS] Missing Python packages: {', '.join(sorted(missing_deps))}")
                answer = input(f"  Install them with pip? (y/n): ").strip().lower()
                dep_install_log = f"Missing packages detected: {', '.join(sorted(missing_deps))}\n"
                if answer in ("y", "yes"):
                    for pkg in sorted(missing_deps):
                        print(f"  [INSTALL] pip install {pkg}")
                        install_result = subprocess.run(
                            [sys.executable, "-m", "pip", "install", pkg],
                            capture_output=True, text=True, timeout=120,
                        )
                        if install_result.returncode == 0:
                            print(f"  [OK] Installed {pkg}")
                            dep_install_log += f"- Installed {pkg}: OK\n"
                        else:
                            print(f"  [FAIL] Could not install {pkg}: {install_result.stderr[:200]}")
                            dep_install_log += f"- Installed {pkg}: FAILED\n"
                    
                    append_to_log(raw_md_path, "Dependency Installation (Post-Run)", dep_install_log)

                    # Re-run after installing — don't waste a retry on a dependency issue
                    print(f"\n[RERUN] Re-executing after dependency install...")
                    run_results = []
                    all_passed = True
                    any_ran = False
                    rerun_log = ""
                    for fp in saved_files:
                        result = run_file(fp, timeout=timeout)
                        run_results.append(result)
                        rerun_log += f"### `{fp.name}`\n"
                        if result.get("skipped"):
                            rerun_log += "Skipped\n\n"
                            continue
                        any_ran = True
                        if not result.get("success"):
                            all_passed = False
                            rerun_log += f"FAILED (exit {result.get('returncode', '?')})\n"
                            if result.get("stderr"):
                                rerun_log += f"```\n{result['stderr'][:1000]}\n```\n"
                        else:
                            rerun_log += "SUCCESS\n"
                        if result.get("stdout"):
                            rerun_log += f"```\n{result['stdout'][:1000]}\n```\n"
                        rerun_log += "\n"

                    append_to_log(raw_md_path, "Re-Run After Dependency Install", rerun_log)
                    
                    if all_passed and any_ran:
                        print(f"\n[DONE] All code ran successfully after installing dependencies!")
                        append_to_log(raw_md_path, "Pipeline Status",
                                      "**SUCCESS** — All code ran after dependency install.")
                        attempt_log["status"] = "success"
                        attempt_log["results"] = _summarize_results(run_results)
                        attempt_log["deps_installed"] = list(missing_deps)
                        run_log["attempts"].append(attempt_log)
                        break
                else:
                    dep_install_log += "User declined to install.\n"
                    append_to_log(raw_md_path, "Dependency Installation (Post-Run)", dep_install_log)

            if not verify or attempt > max_retries:
                run_log["attempts"].append(attempt_log)
                if not verify:
                    status_msg = "Code failed. Use --verify to auto-retry with ChatGPT."
                    print(f"\n[INFO] {status_msg}")
                else:
                    status_msg = f"Max retries ({max_retries}) reached. Code still failing."
                    print(f"\n[FAIL] {status_msg}")
                append_to_log(raw_md_path, "Pipeline Status", f"**FAILED** — {status_msg}")
                if master_md_path and master_md_path != raw_md_path:
                    append_to_log(master_md_path, f"Attempt {attempt} — Result",
                                  f"FAILED — {status_msg}")
                break

            # Step 5: Build feedback and loop (multi-turn in same conversation)
            # NOTE: Only error messages go to ChatGPT (not the entire md file)
            print(f"\n[4/4] Sending errors back to ChatGPT (same conversation)...")
            current_prompt = build_feedback_prompt(saved_files, run_results)
            print(f"  Feedback prompt: {len(current_prompt)} chars")
            attempt_log["feedback_chars"] = len(current_prompt)
            run_log["attempts"].append(attempt_log)

            # Log the feedback that will be sent
            append_to_log(raw_md_path, "Feedback Prompt (to be sent to ChatGPT)",
                          f"```\n{current_prompt}\n```")
            if master_md_path and master_md_path != raw_md_path:
                append_to_log(master_md_path, f"Attempt {attempt} — Feedback Sent",
                              f"```\n{current_prompt}\n```")

    # --- Finalize run record and embed into master md ---
    run_log["finished_at"] = datetime.now().isoformat()
    run_log["total_attempts"] = attempt
    run_log["final_status"] = (
        run_log["attempts"][-1].get("status", "unknown")
        if run_log["attempts"] else "no_attempts"
    )

    if master_md_path:
        append_to_log(master_md_path, "Run Record (JSON)",
                      f"```json\n{json.dumps(run_log, indent=2, default=str)}\n```")

    print(f"\n[DONE] Pipeline finished after {attempt} attempt(s).")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Code extraction, execution & verification")
    sub = parser.add_subparsers(dest="command")

    # extract
    p_ext = sub.add_parser("extract", help="Extract code from a raw_md response file")
    p_ext.add_argument("response", help="Path to saved response .md file (in raw_md/)")
    p_ext.add_argument("--dest", help="Destination folder (default: programs/)")
    p_ext.add_argument("--run", action="store_true", help="Run after extracting")

    # run
    p_run = sub.add_parser("run", help="Run a code file")
    p_run.add_argument("filepath", help="Path to code file")
    p_run.add_argument("--timeout", type=int, default=30, help="Timeout in seconds")

    # pipeline
    p_pipe = sub.add_parser("pipeline", help="Prompt → extract → save → run [→ verify]")
    p_pipe.add_argument("prompt", help="Prompt to send to ChatGPT")
    p_pipe.add_argument("--dest", help="Destination folder (default: programs/)")
    p_pipe.add_argument("--no-run", action="store_true", help="Don't run the code")
    p_pipe.add_argument("--headed", action="store_true", help="Show browser")
    p_pipe.add_argument("--verify", action="store_true",
                        help="Auto-retry failures (multi-turn, same conversation)")
    p_pipe.add_argument("--max-retries", type=int, default=3,
                        help="Max verification retries (default: 3)")
    p_pipe.add_argument("--timeout", type=int, default=30,
                        help="Execution timeout in seconds (default: 30). "
                             "Long-running programs that produce output before "
                             "timeout are still considered successful.")

    args = parser.parse_args()

    if args.command == "extract":
        cmd_extract(args.response, dest=args.dest, run=args.run)
    elif args.command == "run":
        cmd_run(args.filepath, timeout=args.timeout)
    elif args.command == "pipeline":
        cmd_pipeline(args.prompt, dest=args.dest, run=not args.no_run,
                     headed=args.headed, verify=args.verify,
                     max_retries=args.max_retries, timeout=args.timeout)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
