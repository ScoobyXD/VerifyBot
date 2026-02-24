"""
extract_skill.py -- Extract code blocks from LLM responses.

Two paths:
1. FENCED: Standard ```language ... ``` blocks. Parse with regex.
2. UNFENCED: The response text has code without fences (from inner_text()).
   Find the first line that is definitely code, take everything from there
   to the last line that is definitely code. That's your script.

That's it. No scoring, no heuristics, no longest-run detection.
"""

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class CodeBlock:
    language: str
    code: str
    index: int

    @property
    def extension(self) -> str:
        ext_map = {
            "python": ".py", "py": ".py",
            "bash": ".sh", "sh": ".sh", "shell": ".sh",
            "c": ".c", "cpp": ".cpp", "c++": ".cpp",
            "rust": ".rs", "java": ".java",
            "javascript": ".js", "js": ".js",
            "typescript": ".ts", "ts": ".ts",
        }
        return ext_map.get(self.language, ".py")


# ---------------------------------------------------------------------------
# Regex for fenced code blocks
# ---------------------------------------------------------------------------

FENCED_BLOCK_RE = re.compile(
    r"```(\w*)\s*\n"
    r"(?:Copy\s*code\s*\n)?"
    r"(.*?)"
    r"\n```",
    re.DOTALL,
)

# Language labels that ChatGPT's UI leaks into the text
KNOWN_LANG_LABELS = {
    "python", "bash", "sh", "shell", "c", "cpp", "c++",
    "javascript", "typescript", "rust", "java", "go", "ruby",
    "powershell", "sql", "html", "css", "json", "yaml",
    "makefile", "toml", "r", "matlab", "lua",
}

# Lines that definitely start code (anchors)
CODE_START_MARKERS = (
    "#!/",
    "import ",
    "from ",
    "def ",
    "class ",
    "#include",
    "int main",
    "fn ",
    "package ",
    "using ",
)


# ---------------------------------------------------------------------------
# Main extraction
# ---------------------------------------------------------------------------

def extract_blocks(text: str) -> List[CodeBlock]:
    """Extract code blocks from LLM response text.

    Path 1: If fenced blocks exist, use them.
    Path 2: If no fences, find code by start markers and take the whole block.
    """
    blocks = _try_fenced(text)
    if blocks:
        return blocks

    blocks = _try_unfenced(text)
    return blocks


def _try_fenced(text: str) -> List[CodeBlock]:
    """Try to extract fenced ```lang ... ``` blocks."""
    blocks = []
    seen = set()

    for match in FENCED_BLOCK_RE.finditer(text):
        lang = match.group(1).strip().lower() or "txt"
        code = match.group(2).strip()

        # Clean: strip language label glued to first line
        code = _strip_leading_label(code, lang)
        code = _strip_copy_code(code)
        code = code.strip()

        # Clean: strip trailing prose and foreign-language blocks that
        # leaked in due to imperfect fence reconstruction from inner_text().
        # This happens when ChatGPT's response has a main code block followed
        # by small bat/shell usage examples -- if the fences aren't
        # reconstructed, the prose + examples get glued onto the code.
        code = _strip_trailing_prose_from_code(code, lang)
        code = code.strip()

        if code and code not in seen:
            seen.add(code)
            blocks.append(CodeBlock(language=lang, code=code, index=len(blocks)))

    return blocks


def _try_unfenced(text: str) -> List[CodeBlock]:
    """Extract code when there are no fences.

    Strategy:
    1. Find the first line that starts with a CODE_START_MARKER
    2. Take everything from there to the end of the text
    3. Strip trailing prose lines from the bottom

    This works because ChatGPT always puts code AFTER its prose intro,
    and any trailing prose ("Run this with...", "This will...") is short.
    """
    lines = text.split("\n")

    # Step 1: Find the first code start line
    code_start = None
    for i, line in enumerate(lines):
        stripped = line.strip()

        # Skip if this is a known language label on its own line
        if stripped.lower() in KNOWN_LANG_LABELS:
            continue
        # Skip "Copy code" artifact
        if stripped.lower() in ("copy code", "copy"):
            continue

        # Check for code start markers
        for marker in CODE_START_MARKERS:
            if stripped.startswith(marker):
                code_start = i
                break
        if code_start is not None:
            break

    if code_start is None:
        return []

    # Step 2: Take everything from code_start to end
    candidate_lines = lines[code_start:]

    # Step 3: Strip trailing prose from the bottom
    # Walk backwards, removing lines that are clearly English prose
    while candidate_lines:
        last = candidate_lines[-1].strip()
        if not last:
            candidate_lines.pop()  # remove trailing blank lines
            continue
        if _is_prose(last):
            candidate_lines.pop()
            continue
        break

    code = "\n".join(candidate_lines).strip()

    if not code or len(code) < 10:
        return []

    lang = _guess_language(code)
    return [CodeBlock(language=lang, code=code, index=0)]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_leading_label(code: str, detected_lang: str) -> str:
    """Strip language label leaked into code by ChatGPT UI.

    E.g. first line is just "Python" or "Bash", or glued: "Pythonimport math"
    """
    if not code:
        return code

    first_line = code.split("\n")[0].strip()

    # Label on its own line
    if first_line.lower() in KNOWN_LANG_LABELS:
        return "\n".join(code.split("\n")[1:])

    # Label glued to first token: "Pythonimport math" -> "import math"
    for label in KNOWN_LANG_LABELS:
        if first_line.lower().startswith(label) and len(first_line) > len(label):
            rest = first_line[len(label):]
            if rest[0] not in (" ", "\n"):  # glued
                remaining_lines = code.split("\n")[1:]
                return rest + "\n" + "\n".join(remaining_lines)

    return code


def _strip_copy_code(code: str) -> str:
    """Strip 'Copy code' artifact from ChatGPT UI."""
    if code.startswith("Copy code\n"):
        return code[len("Copy code\n"):]
    if code.startswith("Copy\n"):
        return code[len("Copy\n"):]
    return code


def _strip_trailing_prose_from_code(code: str, lang: str) -> str:
    """Strip trailing prose and foreign-language blocks from extracted code.

    When session.py's _insert_fences fails to reconstruct small bat/shell
    code blocks at the end of a ChatGPT response, they get glued onto the
    main code block along with prose like "Run it from your folder:".

    Strategy: find the last line that is a definitive code construct for the
    language (like `if __name__` for Python, `return 0;` for C, etc.) and
    cut everything after that logical block. If no definitive end is found,
    fall back to walking backwards stripping prose.
    """
    if lang not in ("python", "py", "c", "cpp", "c++", "rust", "java",
                     "javascript", "js", "typescript", "ts", "go", "ruby"):
        return code

    lines = code.split("\n")
    if len(lines) < 3:
        return code

    # --- Strategy 1: Find the last definitive code boundary ---
    # For Python: the `if __name__` block is always the end of a script.
    # Find it, then include all indented lines after it, and cut the rest.
    if lang in ("python", "py"):
        cut_point = _find_python_code_end(lines)
        if cut_point is not None and cut_point < len(lines):
            trimmed = "\n".join(lines[:cut_point])
            if trimmed.strip():
                return trimmed

    # --- Strategy 2: Walk backwards stripping non-code lines ---
    code_end = len(lines)
    i = len(lines) - 1
    found_prose = False

    while i >= 0:
        stripped = lines[i].strip()

        if not stripped:
            i -= 1
            continue

        if stripped.lower() in KNOWN_LANG_LABELS or stripped.lower() in ("bat", "cmd", "powershell"):
            code_end = i
            i -= 1
            found_prose = True
            continue

        if stripped.lower() in ("copy code", "copy"):
            code_end = i
            i -= 1
            found_prose = True
            continue

        if _is_prose(stripped):
            code_end = i
            i -= 1
            found_prose = True
            continue

        if _is_shell_command_line(stripped) and lang in ("python", "py", "c", "cpp",
                                                          "c++", "rust", "java", "go"):
            code_end = i
            i -= 1
            found_prose = True
            continue

        # If we've already found prose above and now hit a non-code short
        # line (like "running" as example output), it's part of the prose block
        if found_prose and len(stripped) < 60 and not _looks_like_code(stripped, lang):
            code_end = i
            i -= 1
            continue

        break

    if code_end < len(lines):
        while code_end > 0 and not lines[code_end - 1].strip():
            code_end -= 1
        return "\n".join(lines[:code_end])

    return code


def _find_python_code_end(lines: list) -> Optional[int]:
    """Find where Python code logically ends.

    Looks for `if __name__` guard and returns the line AFTER its block.
    """
    main_guard_idx = None
    for i, line in enumerate(lines):
        if line.strip().startswith("if __name__"):
            main_guard_idx = i

    if main_guard_idx is None:
        return None

    # Walk forward from the guard, include all indented/blank lines
    end = main_guard_idx + 1
    while end < len(lines):
        stripped = lines[end].strip()
        if not stripped:
            # Blank line — could be inside the block or after it
            # Look ahead: if next non-blank line is indented, it's still in the block
            peek = end + 1
            while peek < len(lines) and not lines[peek].strip():
                peek += 1
            if peek < len(lines) and (lines[peek].startswith(" ") or lines[peek].startswith("\t")):
                end = peek
                continue
            else:
                # Blank line after the block — stop here
                break
        elif lines[end].startswith(" ") or lines[end].startswith("\t"):
            # Indented — still in the if __name__ block
            end += 1
        else:
            # Non-indented, non-blank — this is after the block
            break

    return end


def _looks_like_code(line: str, lang: str) -> bool:
    """Return True if line looks like it could be code in the given language."""
    s = line.strip()
    if not s:
        return False

    # Universal code indicators
    code_chars = ("(", ")", "{", "}", "[", "]", "=", ";", "//", "/*", "*/",
                  "->", "=>", "::", "&&", "||", "!=", "==", "<=", ">=")
    if any(c in s for c in code_chars):
        return True

    # Python-specific
    if lang in ("python", "py"):
        py_markers = ("import ", "from ", "def ", "class ", "return ",
                      "if ", "elif ", "else:", "for ", "while ", "with ",
                      "try:", "except", "finally:", "raise ", "yield ",
                      "print(", "self.", "assert ", "@", "#")
        if any(s.startswith(m) for m in py_markers):
            return True
        # Indented lines are almost always code
        if line.startswith(" ") or line.startswith("\t"):
            return True

    return False


def _is_shell_command_line(line: str) -> bool:
    """Return True if line looks like a shell command (not valid Python/C code)."""
    s = line.strip()
    # Common CLI invocation patterns
    shell_starters = (
        "python ", "python3 ", "pip ", "pip3 ",
        "bash ", "sh ", "cmd ", "powershell ",
        "gcc ", "g++ ", "make ", "cargo ",
        "npm ", "node ", "ruby ", "go ",
        "cd ", "ls ", "mkdir ", "rm ", "cp ", "mv ",
        "cat ", "grep ", "curl ", "wget ",
        "./", ".\\",
    )
    return any(s.startswith(p) for p in shell_starters)


def _is_prose(line: str) -> bool:
    """Return True if line is clearly English prose, not code."""
    s = line.strip()
    if not s:
        return False

    # Starts with common English sentence openers
    prose_starters = (
        "Here ", "This ", "The ", "It ", "I ", "You ", "Note", "Now ",
        "If you", "Run ", "Save ", "Usage", "Why ", "So ", "Your ",
        "To ", "Let ", "We ", "For ", "In ", "That ", "These ",
        "Make sure", "Please ", "Copy ", "Output", "Example",
        "Explanation", "How ", "What ", "When ", "Where ",
    )
    if any(s.startswith(p) for p in prose_starters):
        return True

    # Markdown list items
    if s.startswith(("- ", "* ", "> ", "1. ", "2. ", "3. ")):
        return True

    # English sentence: starts uppercase, has spaces, ends with period
    if (s[0].isupper() and " " in s and s.endswith((".", "!", "?", ":"))
            and not s.startswith(("print(", "return ", "import ", "from ", "def ", "class "))):
        return True

    return False


def _guess_language(code: str) -> str:
    """Guess language from code content."""
    if "def " in code or "import " in code or "print(" in code:
        return "python"
    if "#include" in code or "int main" in code:
        return "c"
    if code.startswith("#!/bin/bash") or code.startswith("#!/bin/sh"):
        return "bash"
    return "python"


# ---------------------------------------------------------------------------
# Metadata extraction
# ---------------------------------------------------------------------------

def extract_filename_hint(text: str) -> Optional[str]:
    """Try to extract a filename from the LLM response."""
    patterns = [
        r"[Ss]ave (?:as|to|it as)\s+[`'\"]?(\w[\w.-]+\.\w+)",
        r"[Ff]ile(?:name)?:\s*[`'\"]?(\w[\w.-]+\.\w+)",
        r"[Cc]reate\s+[`'\"]?(\w[\w.-]+\.\w+)",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return m.group(1)
    return None


def extract_timeout_hint(text: str) -> Optional[int]:
    """Parse TIMEOUT: <seconds> from the top of an LLM response."""
    m = re.search(r"TIMEOUT:\s*(\d+)", text[:300])
    if m:
        val = int(m.group(1))
        if 5 <= val <= 600:
            return val
    return None


# ---------------------------------------------------------------------------
# Classification: script vs command
# ---------------------------------------------------------------------------

SCRIPT_LANGUAGES = {"python", "py", "c", "cpp", "c++", "rust", "java",
                    "javascript", "js", "typescript", "ts", "go", "ruby"}

CMD_STARTERS = re.compile(
    r"^\s*(ps|kill|ls|cat|grep|find|i2cdetect|i2cget|i2cset|gpio|"
    r"raspi-config|systemctl|journalctl|dmesg|lsusb|lsmod|modprobe|"
    r"apt|pip|pip3|python3?|chmod|mkdir|cd|rm|cp|mv|echo|curl|wget|"
    r"uname|hostname|uptime|free|df|top|htop|which|where|"
    r"git|make|gcc|g\+\+)\b",
    re.IGNORECASE,
)

JUNK_PATTERNS = [
    r"^\$\s",                   # $ prompt prefix
    r"^(sudo\s+)?kill\s+\d+$",  # kill <specific PID>
    r"^pip3?\s+install\b",      # pip install (handled separately)
]


def classify_blocks(blocks: List[CodeBlock]) -> Tuple[List[CodeBlock], List[str]]:
    """Classify code blocks into scripts (save+run) and commands (run directly).

    Returns (scripts, commands).
    """
    scripts = []
    commands = []

    for block in blocks:
        code = block.code.strip()
        lang = block.language.lower()

        # Skip non-code
        if lang in ("txt", "text", "plaintext", "yaml", "yml", "json", "xml"):
            continue

        # Known programming language -> script
        if lang in SCRIPT_LANGUAGES and len(code.split("\n")) >= 1:
            scripts.append(block)
            continue

        # Bash: multi-line or has logic -> script
        if lang in ("bash", "sh", "shell", ""):
            lines = [l for l in code.split("\n") if l.strip() and not l.strip().startswith("#")]

            if len(lines) >= 4 or any(kw in code for kw in
                    ["for ", "while ", "if ", "function ", "#!/"]):
                scripts.append(block)
                continue

            # Skip junk
            if any(re.search(p, code, re.IGNORECASE | re.MULTILINE) for p in JUNK_PATTERNS):
                continue

            # Short commands
            for line in code.split("\n"):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if CMD_STARTERS.match(line):
                    commands.append(line)

    return scripts, commands
