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
    """Extract all fenced code blocks from an LLM response."""
    blocks = []
    seen = set()

    for match in FENCED_BLOCK_RE.finditer(text):
        lang = match.group(1).strip().lower() or "txt"
        code = match.group(2).strip()
        if code and code not in seen:
            seen.add(code)
            blocks.append(CodeBlock(language=lang, code=code, index=len(blocks)))

    return blocks


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
