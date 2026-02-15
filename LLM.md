# LLM.md -- AI-Driven Hardware Self-Verification System

## What This Is

A set of Python scripts that turn ChatGPT's browser UI into a programmable code generation, execution, and verification pipeline -- extended to **deploy code to real hardware** (Raspberry Pi 5 + STM32L476RG) and verify it works.

The system sends a prompt to ChatGPT, scrapes the response, extracts code blocks, filters out junk (example output, run commands), **classifies where code should run** (local vs Pi), deploys each file to the correct target, executes it, captures output, and -- if anything fails -- sends the errors back to ChatGPT in the same conversation for a fix. All from one command.

## Why

LLMs generate embedded code that compiles but doesn't work. The only way to know if it actually works is to run it on the hardware and observe what happens. This system closes that loop automatically.

## How It Works (Quick Version)

```
python scheduler.py "make a random word generator that saves to a text file in raspi"
```

What happens:
1. `classify_target()` reads "raspi" in prompt -> routes to Pi
2. Builds platform-aware prefix telling ChatGPT "this runs on Pi 5, Linux, ASCII only"
3. Opens browser, sends prompt to ChatGPT
4. Saves response to raw_md/
5. Extracts code blocks, filters junk (one-liner bash, example output)
6. Saves real program(s) to programs/ with meaningful filenames
7. SFTP uploads to Pi, SSH executes python3 <script>
8. Captures stdout/stderr
9. If failed + verify on: builds error feedback, sends back to ChatGPT, loops

Replace "raspi" with "local" and step 7 becomes local execution instead.

## Directory Structure

```
verifybot/
|-- scheduler.py               # Entry point -- run everything from here
|-- acceptance.py              # Acceptance test generation + pre/post state diffing
|-- core/                      # Infrastructure (things skills depend on)
|   |-- __init__.py            # Required for Python package imports
|   |-- selectors.py           # ChatGPT DOM selectors
|   +-- session.py             # Persistent browser session
|-- skills/                    # All skill modules
|   |-- __init__.py            # Required for Python package imports
|   |-- chatgpt_skill.py      # Browser automation, raw_md saving
|   |-- code_skill.py         # Code extraction, execution, verification
|   +-- ssh_skill.py          # SSH/SFTP to Raspberry Pi
|-- .env                       # Pi SSH credentials (gitignored)
|-- .gitignore
|-- LLM.md                     # This file
|-- raw_md/                    # Pipeline run transcripts
|-- programs/                  # Extracted code (cleared between runs)
+-- .browser_profile/          # Browser cookies (persistent login)

On the Raspberry Pi:
/home/scoobyxd/Documents/      # Default deploy target for Pi scripts
/home/scoobyxd/hw/             # Future: hardware project workspace
|-- pi/                        # Pi-side scripts
|-- stm32/                     # STM32 source + build artifacts
+-- logs/                      # UART captures, test logs
```

## Files

### core/ -- Infrastructure (things skills depend on)

| File | Purpose |
|---|---|
| `core/__init__.py` | Makes core/ a Python package (required for imports to work) |
| `core/selectors.py` | ChatGPT DOM selectors -- centralized so when ChatGPT changes its frontend, you update one file |
| `core/session.py` | Persistent browser session -- `prompt()`, `followup()`, `new_chat()` |

### skills/ -- All skill modules

| File | Purpose |
|---|---|
| `skills/__init__.py` | Makes skills/ a Python package (required for imports to work) |
| `skills/chatgpt_skill.py` | Browser automation: send prompts, save responses to raw_md/, append_to_log() |
| `skills/code_skill.py` | Code extraction from markdown, compilation, local execution, verification loop, feedback prompts |
| `skills/ssh_skill.py` | SSH/SFTP to Raspberry Pi via paramiko. ssh_run(), sftp_upload(), sftp_download(). Loads creds from .env |

### Root

| File | Purpose |
|---|---|
| `scheduler.py` | Entry point. prompt -> ChatGPT -> extract -> filter -> classify -> execute -> TEST. Browser visible and auto-retry on by default. |
| `acceptance.py` | Acceptance test generation and evaluation. Pre/post state diffing for kill-process, create-file, etc. No external deps. |
| `.env` | Pi SSH credentials (PI_USER, PI_HOST, PI_PASSWORD). Gitignored, never committed. |
| `.gitignore` | Keeps secrets, caches, and generated files out of git |
| `LLM.md` | This file |

### Why __init__.py?

Python needs a file called `__init__.py` inside a folder to treat it as an importable package. Without it, `from core.session import ChatGPTSession` fails with ModuleNotFoundError. They are one-line files with just a comment. You never edit them.

## Import Map

How modules depend on each other (arrows = "imports from"):

```
scheduler.py
  |-- core.session          (ChatGPTSession)
  |-- skills.code_skill     (extract, save, run, feedback)
  |-- skills.chatgpt_skill  (ensure_dirs)
  |-- skills.ssh_skill      (ssh_run, sftp_upload)
  +-- acceptance            (generate_acceptance_tests, capture_pre_state, run_acceptance_tests)

acceptance.py
  +-- skills.ssh_skill      (ssh_run, REMOTE_WORK_DIR -- conditional import)

skills/chatgpt_skill.py
  |-- core.selectors
  +-- core.session

skills/code_skill.py
  |-- skills.chatgpt_skill  (lazy import inside cmd_pipeline only)
  +-- core.session           (lazy import inside cmd_pipeline only)

core/session.py
  +-- core.selectors

core/selectors.py
  (no internal imports)

skills/ssh_skill.py
  (no internal imports, reads .env from project root)
```

## Target Classification

The scheduler auto-detects where code should run:

**From prompt keywords** (strongest signal):
- RASPI: "raspi", "raspberry pi", "pi5", "gpio", "i2c", "spi", "uart", "can bus",
  "sensor", "motor", "imu", "stm32", "embedded", "hardware", "remote"
- LOCAL: "local", "locally", "this machine", "my computer", "my laptop", "windows", "here"

**From code content** (after extraction):
- Pi patterns: `import RPi`, `import spidev`, GPIO references, /dev/tty*, HAL includes

**From filenames**: pi_*.py, stm32_*.c -> raspi

**Override**: `--target local` or `--target raspi` skips auto-detection.

## Junk Block Filtering

ChatGPT responses often include non-program code blocks:
- One-liner bash: `python3 script.py` (just showing how to run it)
- Example output: `Execution host: raspberrypi5` (showing what output looks like)
- yaml/txt blocks that are illustrations, not code

The scheduler filters these out before saving/executing. Only real programs with actual logic get kept. Safety net: if ALL blocks get filtered, keeps the largest one.

## Usage

```bash
# Typical -- just a prompt, everything else is automatic
# (browser visible, auto-retry on, target auto-detected)
python scheduler.py "make a random word generator that saves to a text file in raspi"
python scheduler.py "make a fizzbuzz program for local"

# Force target
python scheduler.py "write a sorting algorithm" --target local
python scheduler.py "blink an LED" --target raspi

# Hide browser (run unattended)
python scheduler.py "make a word counter for raspi" --headless

# Disable auto-retry
python scheduler.py "hello world for local" --no-verify

# More retries, longer timeout
python scheduler.py "stress test for raspi" --max-retries 5 --timeout 120

# Just extract code, don't run it
python scheduler.py "write a CAN bus listener for raspi" --no-run

# SSH skill standalone
python skills/ssh_skill.py --test
python skills/ssh_skill.py --run "ls -la ~/Documents"
python skills/ssh_skill.py --deploy programs/word_generator.py
```

## CLI Flags

| Flag | Default | What it does |
|------|---------|--------------|
| `"prompt"` | (required) | Your natural language prompt in quotes |
| `--target local/raspi` | auto-detect | Force where code runs |
| `--headless` | OFF (browser visible) | Hide the browser window |
| `--no-verify` | OFF (verify ON) | Disable auto-retry on failure |
| `--max-retries N` | 3 | How many times to retry on failure |
| `--no-run` | OFF (runs code) | Just extract code, don't execute |
| `--dest ./folder` | programs/ | Where to save extracted code locally |
| `--remote-dir /path` | ~/Documents | Where files go on the Pi |
| `--timeout N` | 30 | Seconds per file before killing execution |

## Dependencies

**Python packages** (install on your Windows laptop):
```
pip install playwright paramiko
playwright install chromium
```

| Package | Version | Purpose |
|---------|---------|---------|
| playwright | latest | Browser automation for ChatGPT |
| paramiko | latest | SSH/SFTP to Raspberry Pi (pure Python, Windows-compatible) |

**On the Raspberry Pi** (do once):
```bash
sudo apt install gcc-arm-none-eabi openocd can-utils
```

## Build Order

### Phase 0: SSH basics (COMPLETE)
1. ssh_skill.py with ssh_run(), sftp_upload(), sftp_download(), deploy_and_run()
2. .env for credentials (gitignored)
3. Uses paramiko (pure Python, works on Windows)

### Phase 0.5: Unified scheduler (COMPLETE)
1. scheduler.py -- single entry point for all pipelines
2. classify_target() -- auto-detects local vs raspi from prompt, code, filenames
3. Junk block filtering -- skips example output and run commands
4. Smart filenames -- derives from prompt instead of program_0.py
5. PipelineLogger -- terminal output mirrored in raw_md transcript
6. Folder reorganization: core/ for infrastructure, skills/ for all skills

### Phase 1: Testing and iteration (IN PROGRESS)
1. Test scheduler end-to-end with various prompts
2. Tune classify_target() and junk filter as edge cases appear
3. [DONE] Semantic post-condition verification for destructive prompts (kill, delete)
4. [DONE] Intent-contradiction detection (blocks that contradict user intent)
5. Extend classify_prompt_intent() for more intent types (create, configure, etc.)
6. Add post-condition checks for local target (not just raspi)

### Phase 2: Target classification for multi-file responses
3. Handle responses with both Pi and STM32 code blocks
4. Route different files to different targets from same prompt

### Phase 3: Cross-compile + flash
5. Pre-stage STM32 support files on Pi (linker script, startup, CMSIS headers)
6. Add compile + flash commands via ssh_run()
7. Test: send minimal STM32 C file, compile on Pi, flash, read UART output

### Phase 4: Full dual-target loop
8. Dual output capture (Pi stdout + STM32 UART)
9. Combined feedback prompt for retry loop
10. Test: full CAN roundtrip end-to-end

## Changelog

### 2026-02-14 v0.6 -- Acceptance testing (pre/post state diffing)

**Problem**: v0.5's post-condition check used a broad regex (`ps aux | grep python`)
that matched a system service (wayvnc-control.py, PID 1172, owned by root). The counter
was ACTUALLY killed on attempt 2, but the post-condition kept failing because wayvnc
was there. This caused 3 wasted retries where ChatGPT produced increasingly aggressive
scripts that eventually started DELETING files trying to satisfy an impossible check.

**Root cause**: The post-condition system had no concept of "what was already running
before we started." It couldn't distinguish target processes from system services.

**Solution: Pre/post state diffing with acceptance tests.**

New file: `acceptance.py` -- generates task-specific acceptance tests from the prompt
BEFORE ChatGPT is consulted. Tests define the success contract.

How it works:
1. BEFORE code runs: snapshot system state (process list, file sizes, etc.)
2. AFTER code runs: snapshot again
3. Compare the DIFF: only changes from pre-state matter
4. System processes in pre-state that DON'T match target patterns are ignored

For "kill process" prompts, two complementary tests are generated:
- **PID test**: Were the specific PIDs matching "counter"/"infinite" from pre-state
  removed in post-state? wayvnc doesn't match those keywords -> automatically excluded.
- **File stability test**: Sample output file size twice with 3s gap. If still growing,
  process is alive regardless of what the PID check says.

Feedback to ChatGPT now includes specific test failures with evidence:
- "FAILED TEST: Target processes killed -- 1/1 target PIDs still alive. Surviving: [2080]"
- Instead of generic "post-condition failed"

Also tells ChatGPT "Do NOT delete files or take unrelated actions" to prevent the
file-deletion behavior seen in attempt 4.

**What changed in scheduler.py**:
- Removed: `classify_prompt_intent()`, `run_postcondition_check()` (old regex system)
- Added: imports from `acceptance.py`
- New pipeline step: "Pre-State Snapshot" runs before ChatGPT is consulted
- New pipeline step: "Step 5: Acceptance Tests" replaces old "Step 5: Post-Condition Check"
- Feedback builder uses `format_test_failures_for_feedback()` for specific error messages

### 2026-02-14 v0.5 -- Semantic verification (three-layer fix)

**Problem**: Pipeline said "success" when it accidentally RESTARTED the process it was
supposed to kill. ChatGPT responded with shell tips instead of a script. The junk filter
let a 2-line "pro tip" bash block through (`python3 X.py & / echo $! > pid`), which got
uploaded and executed on the Pi -- relaunching the very process the user wanted dead.
Exit code 0 = "success" even though the task completely failed.

**Root cause**: The system only verified "did the code run without crashing?" not
"did the code accomplish what the user actually wanted?"

**Three layers of fixes**:

1. **Better junk filtering** (`is_junk_block` expanded):
   - Catches backgrounded process launches (`python3 X.py &`)
   - Catches nohup patterns, `fg`/`Ctrl+C` examples, `kill $(cat pid)` examples
   - Now checks bash blocks up to 5 lines (was 3) for these patterns

2. **Intent-contradiction detection** (`detect_intent_contradiction` -- new):
   - Classifies user prompt intent (destructive: kill/stop/delete vs constructive)
   - Checks each surviving code block for patterns that contradict the intent
   - Example: prompt says "kill process" but code launches a process -> BLOCKED
   - If all blocks contradict, auto-retries with explicit "give me a Python script" feedback

3. **Semantic post-condition verification** (`classify_prompt_intent` + `run_postcondition_check` -- new):
   - After code runs successfully, checks whether the intended effect actually happened
   - For "kill process" prompts: runs `pgrep` on Pi to verify process is actually dead
   - For "delete file" prompts: checks file no longer exists
   - If post-condition fails: sends targeted feedback to ChatGPT explaining
     "code ran but didn't accomplish the task"
   - Adds Step 5 to pipeline: post-condition check (only for non-generic intents)

**Also fixed**: `run_on_raspi()` now logs stdout/stderr for .sh files (was silent before,
making it impossible to diagnose what bash scripts actually did).

### 2026-02-13 v0.4 -- Folder reorganization
- Moved selectors.py and session.py into core/ (infrastructure)
- Moved chatgpt_skill.py, code_skill.py, ssh_skill.py into skills/
- scheduler.py stays at root as the entry point
- All imports updated, __init__.py files added
- All Path references use .resolve().parent.parent to find project root

### 2026-02-13 v0.3 -- Scheduler improvements
- Junk block filtering: filters out one-liner bash run commands and example output blocks
- Smart filenames: extracted files named from prompt context, not program_0.py
- Terminal-mirroring MD logs: raw_md is now an ordered transcript of the run
- PipelineLogger: dual-output logger (terminal + md file simultaneously)
- Flipped defaults: browser visible and auto-retry ON by default

### 2026-02-13 v0.2 -- Unified scheduler
- scheduler.py single entry point
- Auto-detects local vs raspi target
- Target-aware prompt prefixes and feedback prompts

### 2026-02-13 v0.1 -- SSH basics
- ssh_skill.py with paramiko
- .env for credentials
- deploy_and_run() for remote execution

## Notes

1. NO EMOJIS anywhere -- Linux terminal and STM32 UART cannot process/print Unicode emoji encodings. Use ASCII only in all generated code, output, and log files.
2. SSH credentials live in .env (gitignored). Never hardcode creds in scripts.
3. ssh_skill.py uses paramiko (pure Python) instead of sshpass -- works on Windows.
4. The junk block filter uses heuristics. If a real program gets filtered, the safety net keeps the largest block. May need tuning over time.
5. __init__.py files are required in core/ and skills/ for Python package imports. They are one-line comment files. Do not delete them.
6. Exit code 0 does NOT mean the task succeeded. The acceptance test system (acceptance.py) handles verification via pre/post state diffing. Tests are generated BEFORE ChatGPT runs, defining the success contract. Pre-state snapshots capture what's already running so system services are automatically excluded from kill checks.
7. ChatGPT "pro tips" and instructional examples are dangerous -- they can get extracted, pass filters, and get executed. The intent-contradiction detector is the safety net for this.
8. Never use broad regexes for post-condition checks. Always diff against pre-state. A system process running before the task started is not a failure.
