"""
extract_skill.py -- Extract code blocks from LLM markdown responses.

Handles:
    - Fenced code blocks (```python ... ```)
    - Language detection and file extension mapping
    - Two-way classification: script (save as file) vs command (run directly)

Usage:
    from skills.extract_skill import extract_blocks, classify_blocks
    blocks = extract_blocks(llm_response_text)
    scripts, commands = classify_blocks(blocks)
"""

import re
from dataclasses import dataclass
from typing import List, Tuple


# ---------------------------------------------------------------------------
# Code block extraction
# ---------------------------------------------------------------------------

FENCED_BLOCK_RE = re.compile(
    r"```(\w*)\s*\n"
    r"(?:Copy\s*code\s*\n)?"
    r"(.*?)"
    r"\n```",
    re.DOTALL,
)


@dataclass
class CodeBlock:
    """A single extracted code block."""
    language: str
    code: str
    index: int

    @property
    def extension(self) -> str:
        ext_map = {
            "python": ".py", "py": ".py",
            "bash": ".sh", "sh": ".sh", "shell": ".sh",
            "c": ".c", "cpp": ".cpp", "c++": ".cpp",
            "javascript": ".js", "js": ".js",
            "rust": ".rs", "java": ".java",
            "json": ".json", "yaml": ".yaml", "yml": ".yaml",
            "html": ".html", "css": ".css",
        }
        return ext_map.get(self.language.lower(), ".txt")


def extract_blocks(text: str) -> List[CodeBlock]:
    """Extract all fenced code blocks from an LLM response.

    Primary: standard ```language ... ``` fences.
    Fallback: if no fences found, look for indented code blocks or
    recognizable code patterns (def/import/class/#!/...).
    """
    blocks = []
    seen = set()

    # Primary: fenced blocks
    for match in FENCED_BLOCK_RE.finditer(text):
        lang = match.group(1).strip().lower() or "txt"
        code = match.group(2).strip()
        if code and code not in seen:
            seen.add(code)
            blocks.append(CodeBlock(language=lang, code=code, index=len(blocks)))

    if blocks:
        return blocks

    # =====================================================================
    # FALLBACK: No fences found. The text is likely from inner_text() with
    # fences stripped. Find the largest contiguous region of code.
    #
    # Strategy: score each line as "code" or "prose", then find the longest
    # run of code lines and treat it as one block.
    # =====================================================================
    lines = text.split("\n")
    scored = []  # (line_index, is_code, line)

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Definite code indicators
        is_code = (
            stripped.startswith(("def ", "class ", "import ", "from ", "if __name__",
                                "#!/", "#include", "int main", "void ", "fn ")) or
            stripped.startswith(("    ", "\t")) and len(stripped) > 1 or
            re.match(r"^\s*(if|elif|else|for|while|try|except|with|return|yield|raise|pass|break|continue)\b", stripped) or
            re.match(r"^\s*\w+\s*=\s*", stripped) or
            re.match(r"^\s*\w+\s*[+\-*/]=", stripped) or  # augmented assignment: i += 2
            re.match(r"^\s*\w+\.\w+\(", stripped) or
            re.match(r"^\s*\w+\s*\(", stripped) or  # function calls like main()
            re.match(r"^\s*print\s*\(", stripped) or
            re.match(r"^\s*#\s", stripped) or  # comments
            stripped == ""  # blank lines (neutral, OK within code)
        )

        # Definite prose indicators (override)
        # Be conservative — only flag things that are clearly English sentences
        is_prose = (
            re.match(r"^(Here |This version|The previous|It |To avoid|I |You |Note:|Now |If you |Run |Save |Usage|Why |So |Your )", stripped) or
            re.match(r"^[A-Z][a-z]+ [a-z]+ [a-z]+ [a-z]+", stripped) and not any(kw in stripped for kw in ["import ", "from ", "class ", "def "]) or  # 4+ word sentence
            stripped.startswith(("- ", "* ", "> ")) or  # Markdown list/quote
            False
        )

        if is_prose:
            scored.append((i, False, line))
        else:
            scored.append((i, is_code, line))

    # Find the longest contiguous run of code lines
    # Allow up to 2 consecutive blank lines within a code run
    best_start, best_end, best_len = 0, 0, 0
    run_start = None
    blank_streak = 0

    for i, (_, is_code, line) in enumerate(scored):
        stripped = line.strip()
        if is_code:
            if stripped == "":
                blank_streak += 1
                if blank_streak > 3:  # Python convention: 2 blank lines between top-level defs
                    # Too many blanks — end this run
                    run_len = i - blank_streak - run_start if run_start is not None else 0
                    if run_len > best_len:
                        best_start, best_end, best_len = run_start, i - blank_streak, run_len
                    run_start = None
                    blank_streak = 0
            else:
                blank_streak = 0
                if run_start is None:
                    run_start = i
        else:
            if run_start is not None:
                run_len = i - run_start
                if run_len > best_len:
                    best_start, best_end, best_len = run_start, i, run_len
            run_start = None
            blank_streak = 0

    # Check final run
    if run_start is not None:
        run_len = len(scored) - run_start
        if run_len > best_len:
            best_start, best_end, best_len = run_start, len(scored), run_len

    if best_len >= 3:
        code_lines = [scored[i][2] for i in range(best_start, best_end)]
        code = "\n".join(code_lines).strip()
        if code and len(code) > 20:
            lang = _guess_language(code)
            blocks.append(CodeBlock(language=lang, code=code, index=0))

    return blocks


def _guess_language(code: str) -> str:
    """Guess language from code content."""
    if "def " in code or "import " in code or "print(" in code:
        return "python"
    if "#include" in code or "int main" in code:
        return "c"
    if code.startswith("#!/bin/bash") or code.startswith("#!/bin/sh"):
        return "bash"
    return "txt"


def extract_filename_hint(text: str) -> str | None:
    """Try to find a suggested filename in the LLM response."""
    patterns = [
        r"save\s+(?:it\s+)?as\s+[`\"']?(\S+\.\w+)[`\"']?",
        r"(?:file|name)\s+(?:it\s+)?(?:called|named)\s+[`\"']?(\S+\.\w+)[`\"']?",
        r"\*\*(\w[\w.-]*\.\w+)\*\*",
        r"`(\w[\w.-]*\.\w+)`",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            fname = m.group(1).strip("`\"'")
            if "." in fname and len(fname) < 60:
                return fname
    return None


def extract_timeout_hint(text: str) -> int | None:
    """Extract predicted execution time from LLM response.

    The LLM is prompted to include TIMEOUT: <seconds> when the script
    needs longer than the default to run. Returns None if no hint found.
    """
    match = re.search(r"TIMEOUT:\s*(\d+)", text, re.IGNORECASE)
    if match:
        val = int(match.group(1))
        # Clamp to reasonable range
        return max(5, min(val, 600))
    return None


# ---------------------------------------------------------------------------
# Block classification
# ---------------------------------------------------------------------------

SCRIPT_LANGUAGES = {"python", "py", "c", "cpp", "c++", "rust", "java", "javascript", "js"}

JUNK_PATTERNS = [
    r"python3?\s+\S+\.py\s*&",          # backgrounding tip
    r"nohup\s+python",                    # nohup example
    r"\bfg\b.*ctrl",                      # fg/Ctrl+C tip
    r"^\$\s+",                            # terminal prompt ($ command)
    r"^\d+\s+\d+\.\d+\s+\d+\.\d+",      # ps aux output
    r"kill\s+(-\d+\s+)?(12345|<PID>)",   # placeholder PID
    r"^sudo\s+(reboot|shutdown|halt)",    # dangerous
]

CMD_STARTERS = [
    "ps ", "kill ", "pkill ", "killall ", "grep ", "ls ", "cat ", "cd ",
    "mkdir ", "rm ", "mv ", "cp ", "chmod ", "systemctl ", "service ",
    "df ", "du ", "free ", "pgrep ", "pidof ", "head ", "tail ", "find ",
    "echo ", "wget ", "curl ", "apt ", "python3 ", "bash ", "test ",
    "i2cdetect", "i2cget", "i2cset", "gpio", "can", "ip ", "ifconfig",
    "ping ", "ss ", "netstat", "pip3 ", "pip ",
]


def classify_blocks(blocks: List[CodeBlock]) -> Tuple[List[CodeBlock], List[str]]:
    """Split blocks into (scripts, commands).

    scripts:  Multi-line code to save as a file, upload, and run.
    commands: Short bash one-liners to execute directly via SSH.
    """
    scripts = []
    commands = []

    for block in blocks:
        code = block.code.strip()
        lang = block.language.lower()
        lines = [l for l in code.split("\n") if l.strip() and not l.strip().startswith("#")]

        # Skip non-code blocks
        if lang in ("txt", "text", "plaintext", "yaml", "yml", "json", "xml"):
            continue

        # Known programming languages -> script
        if lang in SCRIPT_LANGUAGES and len(lines) >= 1:
            scripts.append(block)
            continue

        # Bash: script vs command
        if lang in ("bash", "sh", "shell", ""):
            # Multi-line with logic -> script
            if len(lines) >= 4 or any(kw in code for kw in
                    ["for ", "while ", "if ", "function ", "#!/"]):
                scripts.append(block)
                continue

            # Check for junk
            if any(re.search(p, code, re.IGNORECASE | re.MULTILINE) for p in JUNK_PATTERNS):
                continue

            # Short actionable commands -> extract individually
            for line in code.split("\n"):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # Strip leading $ prompt if present
                if line.startswith("$ "):
                    line = line[2:]
                first = line.lower()
                if any(first.startswith(s) for s in CMD_STARTERS):
                    commands.append(line)

    return scripts, commands
