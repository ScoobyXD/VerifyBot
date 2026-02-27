"""Intent helpers and prompt snippets for scalable task routing."""

TERMINAL_INTENT_KEYWORDS = [
    "terminal", "command", "bash", "shell", "git", "github", "commit",
    "rollback", "revert", "reset", "push", "pull", "checkout", "branch",
]


def is_terminal_intent(prompt: str) -> bool:
    """Return True when the user prompt looks like a terminal workflow."""
    text = prompt.lower()
    return any(keyword in text for keyword in TERMINAL_INTENT_KEYWORDS)


def terminal_prompt_rules() -> str:
    """Extra instructions to bias LLM output toward executable command blocks."""
    return (
        "- This is a TERMINAL EXECUTION task. Return executable bash commands only, each in fenced bash blocks.\n"
        "- For git rollback/reset/revert tasks, include exact git commands in the correct order for local + remote.\n"
        "- Do not explain before commands. Commands first.\n"
    )
