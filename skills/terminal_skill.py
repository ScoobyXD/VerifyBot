"""Terminal-focused helper utilities used by the main pipeline.

Keeping these helpers in skills/ makes terminal automation easier to expand
without bloating main.py routing/prompt logic.
"""

import re


def build_terminal_verification_suffix() -> str:
    """Return verification instructions that accept command-sequence fixes."""
    return (
        "- FAIL: if there are errors, and then provide the complete fixed code or command sequence\n"
        "- REVISE: if it partially works but needs changes, and then provide the complete revised code or command sequence"
    )


def extract_simple_rollback_sha(prompt: str) -> str | None:
    """Extract commit SHA for prompts like 'rollback ... to <sha>'."""
    p = prompt.lower()
    if "rollback" not in p and "revert" not in p and "reset" not in p:
        return None

    match = re.search(r"\b[0-9a-f]{7,40}\b", prompt, flags=re.IGNORECASE)
    if not match:
        return None
    return match.group(0)
