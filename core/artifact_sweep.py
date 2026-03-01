"""
artifact_sweep.py -- Post-execution file artifact cleanup.

After a script runs, it may create output files (word_freq.txt,
fib_cipher.txt, data.csv, etc.) in the project root or programs/
directory. These artifacts clutter the workspace.

This module provides sweep_artifacts() which:
    1. Snapshots directories BEFORE execution
    2. After execution, diffs to find new files
    3. Moves non-code artifacts to outputs/
    4. Leaves code files (.py, .sh, .c, etc.) in place
    5. Leaves files written to explicit external paths alone

Usage in the pipeline:
    pre = snapshot_dirs()
    # ... run the script ...
    sweep_artifacts(pre)
"""

import shutil
from pathlib import Path
from datetime import datetime

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
PROGRAMS_DIR = ROOT / "programs"
OUTPUTS_DIR = ROOT / "outputs"

# File extensions that are considered "code" -- leave these in place
CODE_EXTENSIONS = {
    ".py", ".sh", ".bash", ".c", ".cpp", ".h", ".hpp",
    ".rs", ".java", ".js", ".ts", ".go", ".rb",
    ".makefile", ".cmake",
}

# Files/dirs that belong in the project root -- never move these
ROOT_IGNORE = {
    "main.py", "tests.py", "test.md",
    ".env", ".gitignore", "LLM.md", "README.md",
    "requirements.txt", "pyproject.toml", "setup.cfg",
    "context", "core", "skills", "programs", "outputs",
    "raw_md", "docs", ".browser_profile", ".git",
    "__pycache__",
}


# ---------------------------------------------------------------------------
# Snapshot + Sweep
# ---------------------------------------------------------------------------

def snapshot_dirs() -> dict:
    """Take a snapshot of files in ROOT and programs/ before execution.

    Returns a dict with sets of file paths that existed before the script ran.
    Call this BEFORE running the script, then pass the result to sweep_artifacts().
    """
    snap = {
        "root": set(),
        "programs": set(),
    }

    # Snapshot project root (top-level files only, not dirs)
    for p in ROOT.iterdir():
        if p.is_file():
            snap["root"].add(p.name)

    # Snapshot programs/
    if PROGRAMS_DIR.exists():
        for p in PROGRAMS_DIR.iterdir():
            if p.is_file():
                snap["programs"].add(p.name)

    return snap


def sweep_artifacts(pre_snapshot: dict) -> list:
    """Find new non-code files created during execution and move them to outputs/.

    Compares current state against pre_snapshot to find newly created files.
    Moves non-code artifacts to outputs/, preserving the filename.
    If a file with the same name already exists in outputs/, adds a timestamp.

    Returns list of (original_path, new_path) for files that were moved.
    """
    OUTPUTS_DIR.mkdir(exist_ok=True)
    moved = []

    # Check project root for new files
    for p in ROOT.iterdir():
        if not p.is_file():
            continue
        if p.name in pre_snapshot["root"]:
            continue  # existed before execution
        if p.name in ROOT_IGNORE:
            continue  # known project file
        if p.name.startswith("."):
            continue  # dotfiles
        if _is_code_file(p):
            continue  # code stays

        new_path = _move_to_outputs(p)
        if new_path:
            moved.append((p, new_path))

    # Check programs/ for new non-code files
    if PROGRAMS_DIR.exists():
        for p in PROGRAMS_DIR.iterdir():
            if not p.is_file():
                continue
            if p.name in pre_snapshot["programs"]:
                continue  # existed before execution
            if _is_code_file(p):
                continue  # code stays in programs/

            new_path = _move_to_outputs(p)
            if new_path:
                moved.append((p, new_path))

    if moved:
        print(f"  [SWEEP] Moved {len(moved)} artifact(s) to outputs/")
        for orig, dest in moved:
            print(f"    {orig.name} -> outputs/{dest.name}")

    return moved


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_code_file(path: Path) -> bool:
    """Check if a file is source code that should stay in place."""
    return path.suffix.lower() in CODE_EXTENSIONS


def _move_to_outputs(src: Path) -> Path:
    """Move a file to outputs/. Handles name collisions with timestamps."""
    dest = OUTPUTS_DIR / src.name

    if dest.exists():
        # Add timestamp to avoid overwriting
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        stem = src.stem
        suffix = src.suffix
        dest = OUTPUTS_DIR / f"{stem}_{ts}{suffix}"

    try:
        shutil.move(str(src), str(dest))
        return dest
    except Exception as e:
        print(f"  [WARN] Could not move {src.name}: {e}")
        return None