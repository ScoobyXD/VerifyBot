#!/usr/bin/env python3
"""
terminal_loop.py -- Interactive ChatGPT <-> Pi terminal conversation loop.

Instead of asking ChatGPT for a complete program, this module treats ChatGPT
as a human operator sitting at a terminal. The loop:

    1. Send user's task + system context to ChatGPT
    2. ChatGPT responds with shell commands (and explanation)
    3. Extract the FIRST actionable command from the response
    4. Execute it on Pi via SSH
    5. Capture the terminal output
    6. Send the output back to ChatGPT: "I ran [command], here's the output: [...]"
    7. ChatGPT decides what to do next (another command, or declare done)
    8. Repeat until ChatGPT says done, or max iterations reached

This handles tasks like:
    - "kill the infinite counter" → ps aux | grep → sees PIDs → kill PID → verify
    - "check disk usage" → df -h → done
    - "install and run a package" → apt install → run → observe output

Usage (from scheduler.py):
    from terminal_loop import run_terminal_loop
    result = run_terminal_loop(session, prompt, log, max_turns=10)
"""

import re
from typing import Optional
try:
    from skills.ssh_skill import ssh_run, REMOTE_WORK_DIR
except ImportError:
    REMOTE_WORK_DIR = "/home/scoobyxd/Documents"
    ssh_run = None


# ---------------------------------------------------------------------------
# Command extraction from ChatGPT response
# ---------------------------------------------------------------------------

def extract_terminal_commands(response: str) -> list:
    """Extract actionable shell commands from a ChatGPT response.

    Returns a list of (command, context) tuples where context is a brief
    description of why this command is being suggested.

    Strategy:
    - Look for bash/sh code blocks first
    - Filter out junk (example output, placeholder PIDs, Ctrl+C, etc.)
    - For multi-command blocks, split into individual commands
    - Return in order of appearance
    """
    commands = []

    # Extract code blocks
    block_pattern = re.compile(
        r"```(?:bash|sh|shell|zsh|terminal)?\s*\n(.*?)```",
        re.DOTALL | re.IGNORECASE,
    )
    blocks = block_pattern.findall(response)

    # Also look for inline code that looks like commands
    # (single backtick blocks that start with common command prefixes)
    inline_pattern = re.compile(r"`([^`]{3,80})`")
    inline_matches = inline_pattern.findall(response)

    # Combine: code blocks first, then inline
    raw_commands = []
    for block in blocks:
        for line in block.strip().split("\n"):
            line = line.strip()
            if line and not line.startswith("#"):
                raw_commands.append(line)

    for inline in inline_matches:
        inline = inline.strip()
        if _looks_like_command(inline) and inline not in raw_commands:
            raw_commands.append(inline)

    # Filter
    for cmd in raw_commands:
        if _is_junk_command(cmd):
            continue
        if _is_actionable_command(cmd):
            commands.append(cmd)

    return commands


def _looks_like_command(text: str) -> bool:
    """Does this text look like a shell command?"""
    cmd_prefixes = [
        "ps ", "kill ", "pkill ", "killall ", "grep ", "ls ", "cat ",
        "cd ", "mkdir ", "rm ", "mv ", "cp ", "chmod ", "chown ",
        "apt ", "pip ", "python", "systemctl ", "service ",
        "df ", "du ", "free ", "top ", "htop ", "uname ", "hostname",
        "pgrep ", "pidof ", "head ", "tail ", "wc ", "find ", "which ",
        "echo ", "export ", "source ", "wget ", "curl ",
    ]
    text_lower = text.lower()
    return any(text_lower.startswith(p) for p in cmd_prefixes)


def _is_junk_command(cmd: str) -> bool:
    """Filter out commands that shouldn't be executed."""
    # Placeholder PIDs
    if re.match(r"^kill\s+(-\d+\s+)?(\d{4,5})$", cmd):
        # Only filter if the PID looks like a placeholder (exactly 5 digits, round number)
        pid_match = re.search(r"(\d+)$", cmd)
        if pid_match:
            pid = pid_match.group(1)
            if pid in ("12345", "1234", "54321"):
                return True
    if re.search(r"<PID>|<pid>|\bPID\b", cmd):
        return True

    # Ctrl+C is not a command
    if re.match(r"^Ctrl\+C$", cmd, re.IGNORECASE):
        return True
    if "Ctrl+C" in cmd:
        return True

    # fg / bg / jobs are session-specific, not useful over SSH
    if re.match(r"^(fg|bg|jobs)\b", cmd):
        return True

    # sudo reboot/shutdown -- too dangerous
    if re.match(r"^sudo\s+(reboot|shutdown|halt|poweroff)", cmd):
        return True

    return False


def _is_actionable_command(cmd: str) -> bool:
    """Is this a command worth executing?"""
    # Must have some substance
    if len(cmd) < 2:
        return False
    # Filter pure comments
    if cmd.startswith("#"):
        return False
    return True


# ---------------------------------------------------------------------------
# Completion detection
# ---------------------------------------------------------------------------

def _detect_completion(response: str, commands: list) -> bool:
    """Detect if ChatGPT considers the task complete.

    Heuristics:
    - No more commands to execute
    - Response contains completion phrases
    - Response asks "would you like me to..." (offering next steps, not commands)
    """
    response_lower = response.lower()

    # If there are still actionable commands, not done yet
    if commands:
        return False

    # Completion indicators
    completion_phrases = [
        "has been killed", "has been stopped", "has been terminated",
        "is no longer running", "successfully stopped", "successfully killed",
        "process is dead", "process has been", "no longer running",
        "task is complete", "that should do it", "done",
        "the script has stopped", "the process is gone",
        "would you like", "want me to", "anything else",
        "let me know if", "is there anything",
    ]
    return any(phrase in response_lower for phrase in completion_phrases)


# ---------------------------------------------------------------------------
# The terminal conversation loop
# ---------------------------------------------------------------------------

def run_terminal_loop(session, prompt: str, log, max_turns: int = 10,
                      timeout: int = 15) -> dict:
    """Run an interactive ChatGPT <-> Pi terminal loop.

    Args:
        session: ChatGPTSession instance (already open, in a conversation)
        prompt: The user's original task description
        log: PipelineLogger for output
        max_turns: Maximum number of command-execute-observe cycles
        timeout: SSH command timeout in seconds

    Returns:
        {
            "success": bool,
            "turns": int,
            "commands_executed": [(cmd, stdout, stderr, exit_code), ...],
            "final_response": str,
        }
    """
    commands_executed = []
    last_response = ""

    # Build the initial prompt that tells ChatGPT to act as a terminal operator
    system_msg = _build_terminal_system_prompt(prompt)

    # Step 1: Send initial prompt
    log.section("Terminal Loop: Turn 0 (Initial Prompt)")
    log.log(f"[TERMINAL] Starting interactive terminal loop (max {max_turns} turns)")
    log.log(f"[TERMINAL] Sending task to ChatGPT...")
    log.log_quiet(f"Prompt:\n```\n{system_msg}\n```")

    response = session.prompt(system_msg)
    last_response = response
    log.log(f"[TERMINAL] Got response ({len(response)} chars)")
    log.log_quiet(f"Response:\n{response}")

    for turn in range(1, max_turns + 1):
        # Extract commands from response
        commands = extract_terminal_commands(response)

        # Check if ChatGPT thinks we're done
        if _detect_completion(response, commands):
            log.section(f"Terminal Loop: Complete")
            log.log(f"[TERMINAL] ChatGPT indicates task is complete after {turn - 1} command(s)")
            return {
                "success": True,
                "turns": turn - 1,
                "commands_executed": commands_executed,
                "final_response": last_response,
            }

        if not commands:
            # No commands and not explicitly done -- might need to ask for commands
            log.log(f"[TERMINAL] No actionable commands found in response, asking for specific command...")
            followup = (
                "I need you to give me the exact command to run. "
                "Please respond with just the shell command in a code block."
            )
            response = session.followup(followup)
            last_response = response
            commands = extract_terminal_commands(response)
            if not commands:
                log.log(f"[TERMINAL] Still no commands. Ending loop.")
                break

        # Execute the first command (one at a time, so ChatGPT can react to output)
        cmd = commands[0]

        log.section(f"Terminal Loop: Turn {turn}")
        log.log(f"[TERMINAL] Executing: {cmd}")

        result = ssh_run(cmd, timeout=timeout)
        stdout = result.get("stdout", "").strip()
        stderr = result.get("stderr", "").strip()
        exit_code = result.get("exit_code", -1)
        timed_out = result.get("timed_out", False)

        commands_executed.append((cmd, stdout, stderr, exit_code))

        # Log the output
        if stdout:
            log.log(f"[TERMINAL] stdout:")
            for line in stdout.split("\n")[:30]:
                log.log(f"  {line}")
        if stderr:
            log.log(f"[TERMINAL] stderr:")
            for line in stderr.split("\n")[:10]:
                log.log(f"  {line}")
        log.log(f"[TERMINAL] exit code: {exit_code}")
        if timed_out:
            log.log(f"[TERMINAL] (command timed out after {timeout}s)")

        # Build the feedback to send back to ChatGPT
        feedback = _build_terminal_feedback(cmd, stdout, stderr, exit_code, timed_out)
        log.log(f"[TERMINAL] Sending output back to ChatGPT...")
        log.log_quiet(f"Feedback:\n```\n{feedback}\n```")

        response = session.followup(feedback)
        last_response = response
        log.log(f"[TERMINAL] Got response ({len(response)} chars)")
        log.log_quiet(f"Response:\n{response}")

        # Quick check: if there are remaining commands from the SAME response
        # and ChatGPT gave multiple commands at once, execute them all
        # (but still one at a time with feedback)

    log.section("Terminal Loop: Max Turns Reached")
    log.log(f"[TERMINAL] Reached max turns ({max_turns})")
    return {
        "success": len(commands_executed) > 0,
        "turns": max_turns,
        "commands_executed": commands_executed,
        "final_response": last_response,
    }


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def _build_terminal_system_prompt(user_task: str) -> str:
    """Build the initial prompt that frames ChatGPT as a terminal operator."""
    return (
        "You are remotely operating a Raspberry Pi 5 (Linux aarch64) terminal via SSH.\n"
        "I will execute your commands on the Pi and show you the output.\n\n"
        "RULES:\n"
        "- Give me ONE command at a time in a ```bash code block\n"
        "- Wait for my output before giving the next command\n"
        "- Use the output to decide what to do next\n"
        "- When the task is complete, say \"DONE\" clearly\n"
        "- Do NOT give placeholder PIDs -- I will show you the real output\n"
        "- Do NOT give long Python scripts -- use simple shell commands\n"
        "- Prefer pkill, kill, ps, grep, cat, ls over writing scripts\n"
        "- The working directory is /home/scoobyxd/Documents\n\n"
        f"TASK: {user_task}\n\n"
        "Start by giving me the first command to run."
    )


def _build_terminal_feedback(cmd: str, stdout: str, stderr: str,
                              exit_code: int, timed_out: bool) -> str:
    """Build the feedback message showing command output to ChatGPT."""
    lines = [f"I ran: `{cmd}`", ""]

    if timed_out:
        lines.append(f"(Command timed out after 15 seconds)")
        lines.append("")

    if stdout:
        lines.append(f"Output:")
        lines.append(f"```")
        # Truncate very long output
        if len(stdout) > 3000:
            lines.append(stdout[:3000])
            lines.append(f"... (truncated, {len(stdout)} chars total)")
        else:
            lines.append(stdout)
        lines.append(f"```")
    else:
        lines.append("(no output)")

    if stderr:
        lines.append(f"Stderr:")
        lines.append(f"```")
        lines.append(stderr[:1000])
        lines.append(f"```")

    lines.append(f"Exit code: {exit_code}")
    lines.append("")
    lines.append("What should I do next? (Give next command, or say DONE if the task is complete)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Detect if a task should use terminal loop vs file-based pipeline
# ---------------------------------------------------------------------------

# Tasks that are better solved by interactive terminal commands
TERMINAL_TASK_PATTERNS = [
    r"\bkill\b.*\b(process|script|program|running)\b",
    r"\bstop\b.*\b(process|script|program|running)\b",
    r"\bterminate\b.*\b(process|script|running)\b",
    r"\bcheck\b.*\b(status|running|process|disk|memory)\b",
    r"\brestart\b.*\b(service|process|script)\b",
    r"\binstall\b.*\bpackage\b",
    r"\bupdate\b.*\b(system|packages|apt)\b",
    r"\bclean\b.*\b(up|files|temp)\b",
    r"\bfind\b.*\b(file|process|port)\b",
    r"\blist\b.*\b(process|file|running)\b",
    r"\bshow\b.*\b(process|log|status)\b",
    r"\bwhat.*(running|listening|open)\b",
]

# Tasks that need a proper program (file-based pipeline)
PROGRAM_TASK_PATTERNS = [
    r"\b(write|create|make|build|generate)\b.*\b(script|program|app|code|file)\b",
    r"\bloop\b",
    r"\bcounter\b.*\b(count|up\s+to)\b",
    r"\bserver\b",
    r"\bdaemon\b",
    r"\bblink\b.*\bled\b",
    r"\bsensor\b",
    r"\bgpio\b",
]


def should_use_terminal_loop(prompt: str) -> bool:
    """Determine if a task is better served by interactive terminal commands
    vs the file-based pipeline.

    Terminal tasks: kill process, check status, install, find files
    Program tasks: write code, create scripts, build applications
    """
    prompt_lower = prompt.lower()

    terminal_score = sum(1 for p in TERMINAL_TASK_PATTERNS
                         if re.search(p, prompt_lower))
    program_score = sum(1 for p in PROGRAM_TASK_PATTERNS
                        if re.search(p, prompt_lower))

    # Terminal wins on tie for destructive/query tasks
    if terminal_score > 0 and terminal_score >= program_score:
        return True
    return False
