#!/usr/bin/env python3
"""
code_skill.py — Extract, save, execute, and verify code from ChatGPT responses.

Usage:
    # Extract code blocks from a raw_md response
    python code_skill.py extract raw_md/20260212_130358_generate_me_a_python.md

    # Extract and save to a specific project folder
    python code_skill.py extract raw_md/response.md --dest ./my_project/

    # Extract, save, and run
    python code_skill.py extract raw_md/response.md --run

    # Run a previously extracted file
    python code_skill.py run ./generated/tictactoe.py

    # Full pipeline: prompt ChatGPT, extract code, save, run
    python code_skill.py pipeline "generate a python fizzbuzz"

    # Full pipeline with verification feedback loop
    python code_skill.py pipeline "generate a python fizzbuzz" --verify
"""

import argparse
import re
import subprocess
import sys
import os
from datetime import datetime
from pathlib import Path
from typing import List, Optional

GENERATED_DIR = Path(__file__).parent / "generated"
RAW_MD_DIR = Path(__file__).parent / "raw_md"

# ---------------------------------------------------------------------------
# Code block extraction
# ---------------------------------------------------------------------------

# Matches fenced code blocks: ```lang\n...code...\n```
# Also handles ChatGPT's "Copy code" artifact that appears in scraped text
FENCED_BLOCK_RE = re.compile(
    r"```(\w*)\s*\n"        # opening fence + optional language
    r"(?:Copy\s*code\s*\n)?" # optional "Copy code" line from ChatGPT UI
    r"(.*?)"                 # code content (non-greedy)
    r"\n```",                # closing fence
    re.DOTALL
)

# Sometimes the scrape picks up the language on a separate line like:
#   html
#   Copy code
#   <!DOCTYPE html>
#   ...
# This pattern catches that. The block ends at:
#   - another language\nCopy code (next block)
#   - a line with emoji (ChatGPT commentary like "2️⃣ What this teaches")
#   - a numbered list item like "2." or "3."
#   - end of string

KNOWN_LANGS = (
    "python|bash|c|cpp|c\\+\\+|javascript|typescript|rust|java|sh|"
    "cmake|makefile|json|yaml|yml|html|css|xml|sql|go|ruby|perl|"
    "swift|kotlin|scala|r|matlab|lua|zig|toml|ini|dockerfile|"
    "plaintext|text|txt|assembly|asm|verilog|vhdl"
)

LOOSE_BLOCK_RE = re.compile(
    rf"^({KNOWN_LANGS})\s*\n"       # language label on its own line
    r"(?:Copy\s*code\s*\n)?"        # optional "Copy code" line
    r"(.*?)"                         # code content (non-greedy)
    rf"(?=\n(?:{KNOWN_LANGS})\s*\n"  # stop before next lang\n block
    r"|\n[0-9]+[️⃣]"                  # stop before emoji numbered list
    r"|\n\d+\.\s+[A-Z]"             # stop before "2. What..." style list
    r"|\nRun it:"                    # stop before "Run it:" instruction
    r"|\nIf you want"               # stop before follow-up offer
    r"|\nJust say"                   # stop before "Just say the word"
    r"|\nWant to"                    # stop before "Want to level it up"
    r"|\nTell me"                    # stop before "Tell me what direction"
    r"|\Z"                           # or end of string
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
            "rust": ".rs",
            "java": ".java",
            "cmake": ".cmake",
            "makefile": "",
            "json": ".json",
            "yaml": ".yaml", "yml": ".yaml",
            "html": ".html",
            "css": ".css",
            "txt": ".txt",
        }
        return ext_map.get(self.language, ".txt")

    def __repr__(self):
        preview = self.code[:60].replace("\n", "\\n")
        return f"CodeBlock({self.language}, {len(self.code)} chars, '{preview}...')"


def extract_code_blocks(text: str) -> List[CodeBlock]:
    """Extract all code blocks from a ChatGPT response."""
    blocks = []
    seen_code = set()  # deduplicate

    # Primary: fenced code blocks
    for match in FENCED_BLOCK_RE.finditer(text):
        lang = match.group(1)
        code = match.group(2).strip()
        if code and code not in seen_code:
            seen_code.add(code)
            blocks.append(CodeBlock(lang, code, len(blocks)))

    # Fallback: loose blocks (language\nCopy code\n...)
    if not blocks:
        for match in LOOSE_BLOCK_RE.finditer(text):
            lang = match.group(1)
            code = match.group(2).strip()
            if code and code not in seen_code:
                seen_code.add(code)
                blocks.append(CodeBlock(lang, code, len(blocks)))

    return blocks


def extract_filename_hint(text: str) -> Optional[str]:
    """Try to find a suggested filename in the response text.
    
    ChatGPT often says things like:
    - "save as tictactoe.py"
    - "save it as main.c"
    - "create a file called hello.py"
    """
    patterns = [
        r"save\s+(?:it\s+)?as\s+[`\"']?(\S+\.\w+)[`\"']?",
        r"(?:file|name)\s+(?:it\s+)?(?:called|named)\s+[`\"']?(\S+\.\w+)[`\"']?",
        r"create\s+(?:a\s+)?(?:file\s+)?[`\"']?(\S+\.\w+)[`\"']?",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1).strip("`\"'")
    return None


# ---------------------------------------------------------------------------
# File operations
# ---------------------------------------------------------------------------

def save_code_block(block: CodeBlock, dest_dir: Path, filename: Optional[str] = None) -> Path:
    """Save a code block to a file."""
    dest_dir.mkdir(parents=True, exist_ok=True)

    if not filename:
        filename = f"block_{block.index}{block.extension}"

    filepath = dest_dir / filename
    filepath.write_text(block.code, encoding="utf-8")
    print(f"  [SAVED] {filepath}  ({block.language}, {len(block.code)} chars)")
    return filepath


def run_file(filepath: Path, timeout: int = 30) -> dict:
    """Execute a file and capture output."""
    filepath = Path(filepath).resolve()
    ext = filepath.suffix.lower()

    # Determine how to run it
    if ext == ".py":
        cmd = [sys.executable, str(filepath)]
    elif ext == ".sh":
        cmd = ["bash", str(filepath)]
    elif ext in (".js", ".mjs"):
        cmd = ["node", str(filepath)]
    else:
        print(f"  [SKIP] Don't know how to run {ext} files")
        return {"success": False, "error": f"Unknown extension: {ext}"}

    print(f"  [RUN] {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=filepath.parent,
        )
        output = {
            "success": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }

        if result.stdout:
            print(f"  [STDOUT]\n{result.stdout[:500]}")
        if result.stderr:
            print(f"  [STDERR]\n{result.stderr[:500]}")
        if result.returncode != 0:
            print(f"  [FAIL] Exit code: {result.returncode}")
        else:
            print(f"  [OK] Ran successfully")

        return output

    except subprocess.TimeoutExpired:
        print(f"  [TIMEOUT] Killed after {timeout}s")
        return {"success": False, "error": "timeout"}
    except FileNotFoundError as e:
        print(f"  [ERROR] {e}")
        return {"success": False, "error": str(e)}


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

    # Determine destination
    dest_dir = Path(dest) if dest else GENERATED_DIR

    # Try to find a filename hint
    hint = extract_filename_hint(text)
    saved_files = []

    for b in blocks:
        # Use hint for the first/main block, generic names for rest
        if b.index == 0 and hint:
            fname = hint
        else:
            fname = None  # auto-generate
        fp = save_code_block(b, dest_dir, filename=fname)
        saved_files.append(fp)

    # Optionally run
    if run and saved_files:
        print("\n--- Running extracted code ---")
        for fp in saved_files:
            if fp.suffix in (".py", ".sh", ".js"):
                run_file(fp)


def cmd_run(filepath: str, timeout: int = 30):
    """Run a file directly."""
    run_file(Path(filepath), timeout=timeout)


def build_feedback_prompt(raw_md_path: Path, code_files: list, run_results: list) -> str:
    """Package the original raw_md and execution output into a follow-up prompt.
    
    This creates a structured prompt that gives ChatGPT:
    1. The original raw_md (its previous response)
    2. Each code file that was extracted
    3. The execution results (stdout, stderr, exit code)
    
    So ChatGPT can see exactly what went wrong and fix it.
    """
    raw_md_content = raw_md_path.read_text(encoding="utf-8")
    
    sections = []
    sections.append("I ran the code you generated and got errors. Here's the full context:\n")
    
    # Include the original raw response
    sections.append("## YOUR PREVIOUS RESPONSE (raw_md)")
    sections.append(f"File: {raw_md_path.name}")
    sections.append("```")
    sections.append(raw_md_content)
    sections.append("```\n")
    
    # Include each code file + its run result
    sections.append("## EXECUTION RESULTS")
    for code_file, result in zip(code_files, run_results):
        sections.append(f"### File: {code_file.name}")
        sections.append(f"```")
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


def cmd_pipeline(prompt: str, dest: str = None, run: bool = True,
                 headed: bool = False, verify: bool = False, max_retries: int = 3):
    """Full pipeline: prompt ChatGPT → extract code → save → run → [verify loop].
    
    With --verify: if code fails, packages the raw_md + execution output
    and sends it back to ChatGPT for a fix. Repeats up to max_retries times.
    """
    # Import the chatgpt skill
    try:
        from chatgpt_skill import run_single_prompt, RAW_MD_DIR, save_response, ensure_dirs
    except ImportError:
        print("[ERROR] chatgpt_skill.py not found in same directory")
        return

    ensure_dirs()
    dest_dir = Path(dest) if dest else GENERATED_DIR

    print("=" * 50)
    print("PIPELINE: prompt → extract → save → run" + (" → verify" if verify else ""))
    print("=" * 50)

    current_prompt = prompt
    attempt = 0

    while attempt <= max_retries:
        attempt += 1
        is_retry = attempt > 1

        if is_retry:
            print(f"\n{'='*50}")
            print(f"RETRY {attempt - 1}/{max_retries}: Sending fix request to ChatGPT...")
            print(f"{'='*50}")

        # Step 1: Send prompt to ChatGPT
        print(f"\n[1/4] {'Re-prompting' if is_retry else 'Prompting'} ChatGPT...")
        response = run_single_prompt(current_prompt, headed=headed)
        if not response:
            print("[FAIL] No response from ChatGPT")
            return

        # The raw_md file was saved by run_single_prompt → save_response
        # Find the most recent raw_md file
        raw_md_files = sorted(RAW_MD_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime)
        raw_md_path = raw_md_files[-1] if raw_md_files else None

        # Step 2: Extract code
        print(f"\n[2/4] Extracting code blocks...")
        blocks = extract_code_blocks(response)
        if not blocks:
            print("[WARN] No code blocks in response. Raw response saved in raw_md/")
            return

        print(f"  Found {len(blocks)} block(s)")

        # Step 3: Save to generated/
        # Clear old generated files on retry to avoid stale files
        if is_retry:
            for old in dest_dir.glob("block_*"):
                old.unlink()

        hint = extract_filename_hint(response)
        saved_files = []

        for b in blocks:
            fname = hint if b.index == 0 and hint else None
            fp = save_code_block(b, dest_dir, filename=fname)
            saved_files.append(fp)

        # Step 4: Run
        if not run or not saved_files:
            print("\n[DONE] Pipeline complete (no execution requested).")
            return

        print(f"\n[3/4] Running...")
        run_results = []
        all_passed = True

        for fp in saved_files:
            if fp.suffix in (".py", ".sh", ".js"):
                result = run_file(fp)
                run_results.append(result)
                if not result["success"]:
                    all_passed = False
            else:
                run_results.append(None)

        if all_passed:
            print(f"\n[DONE] All code ran successfully!")
            return

        # --- Code failed ---
        if not verify or attempt > max_retries:
            if not verify:
                print(f"\n[INFO] Code failed. Use --verify to auto-retry with ChatGPT.")
            else:
                print(f"\n[FAIL] Max retries ({max_retries}) reached. Code still failing.")
            return

        # Step 5: Build feedback prompt and loop
        print(f"\n[4/4] Packaging raw_md + output for verification...")
        if raw_md_path:
            current_prompt = build_feedback_prompt(raw_md_path, saved_files, run_results)
            print(f"  Feedback prompt: {len(current_prompt)} chars")
        else:
            print("[WARN] Could not find raw_md file for feedback.")
            return

    print(f"\n[DONE] Pipeline complete after {attempt} attempt(s).")


def main():
    parser = argparse.ArgumentParser(description="Code extraction & execution skill")
    sub = parser.add_subparsers(dest="command")

    # extract
    p_ext = sub.add_parser("extract", help="Extract code from a raw_md response file")
    p_ext.add_argument("response", help="Path to saved response .md file (in raw_md/)")
    p_ext.add_argument("--dest", help="Destination folder for extracted code (default: generated/)")
    p_ext.add_argument("--run", action="store_true", help="Run after extracting")

    # run
    p_run = sub.add_parser("run", help="Run a code file")
    p_run.add_argument("filepath", help="Path to code file")
    p_run.add_argument("--timeout", type=int, default=30, help="Timeout in seconds")

    # pipeline
    p_pipe = sub.add_parser("pipeline", help="Prompt → extract → save → run [→ verify]")
    p_pipe.add_argument("prompt", help="Prompt to send to ChatGPT")
    p_pipe.add_argument("--dest", help="Destination folder for code (default: generated/)")
    p_pipe.add_argument("--no-run", action="store_true", help="Don't run the code")
    p_pipe.add_argument("--headed", action="store_true", help="Show browser")
    p_pipe.add_argument("--verify", action="store_true",
                        help="If code fails, send raw_md + output back to ChatGPT for a fix")
    p_pipe.add_argument("--max-retries", type=int, default=3,
                        help="Max verification retries (default: 3)")

    args = parser.parse_args()

    if args.command == "extract":
        cmd_extract(args.response, dest=args.dest, run=args.run)
    elif args.command == "run":
        cmd_run(args.filepath, timeout=args.timeout)
    elif args.command == "pipeline":
        cmd_pipeline(args.prompt, dest=args.dest, run=not args.no_run,
                     headed=args.headed, verify=args.verify,
                     max_retries=args.max_retries)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
