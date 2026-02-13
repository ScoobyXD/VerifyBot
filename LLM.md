# LLM.md — Browser-Automated LLM Skill System

## What This Is

A set of Python scripts that turn ChatGPT's browser UI into a programmable code generation, execution, and verification pipeline. No API keys. No credits. Just Playwright driving the same browser window you'd use manually.

The system sends prompts to ChatGPT, scrapes the response, extracts code blocks, saves them as real files, compiles/runs them, and — if they fail — sends the errors back in the same conversation for ChatGPT to fix. All from one command.

## Why

LLM APIs cost money. ChatGPT Plus is a flat-rate subscription. The web UI is the same model behind the same context window — just wrapped in a browser instead of a REST endpoint.

This project treats the browser UI as the endpoint. Everything a human does — type a prompt, wait for the response, copy the code, paste it into a file, run it — is automated with Playwright. The result is a zero-cost LLM integration layer that runs on your laptop.

The broader goal is a **skill-based automation system** where LLMs reason and generate, local scripts execute and build, and hardware measurements verify. No step trusts the AI's claims. Every result is checked against reality — exit codes, compiler output, actual behavior.

## How It Works

```
You run a command
        │
        ▼
  session.py opens persistent browser
        │
        ▼
  code_skill.py prepends platform context
  (tells ChatGPT: OS, available tools, constraints)
        │
        ▼
  chatgpt_skill.py sends prompt (reuses session)
        │
        ▼
  Response saved to raw_md/ (with prompt sent)
        │
        ▼
  code_skill.py extracts code blocks
  → extraction details appended to raw_md
        │
        ▼
  Code saved to programs/ (with filename hints)
  → saved file paths appended to raw_md
        │
        ▼
  Dependencies checked (missing imports detected)
  → dependency actions appended to raw_md
        │
        ▼ (if missing deps)
  User prompted: "Install numpy with pip? (y/n)"
  Installed without burning a retry
        │
        ▼
  Compiled (C/C++) or interpreted (Python/bash/JS)
  Long-running programs detected via static analysis
  → full stdout/stderr/exit codes appended to raw_md
        │
        ▼ (if timeout)
  Smart timeout: did it produce output?
    YES → SUCCESS (it was working, we just stopped it)
    NO  → FAILURE (hung or broken)
        │
        ▼ (if --verify and code failed)
  Errors sent back in SAME conversation as PLAIN TEXT
  (no markdown — prevents mangling in ChatGPT's input)
  → feedback prompt text appended to raw_md
  (NOTE: only errors go to ChatGPT, NOT the full md)
        │
        ▼
  New code extracted, compiled/run again
  → retry response + results appended to master raw_md
  (loops up to --max-retries times)
        │
        ▼
  Final status appended to raw_md
  Honest status: SUCCESS only if code actually ran correctly
```

## Directory Structure

| Directory | Contents |
|---|---|
| `raw_md/` | Full pipeline history as timestamped markdown — includes the prompt sent, ChatGPT's response, code extraction details, compilation/execution output, feedback prompts, and final status. Each attempt creates its own file; the first attempt's file also accumulates retry history for a complete audit trail. |
| `programs/` | Extracted code files ready to compile/run |
| `.browser_profile/` | Persistent Chromium login cookies |

## Files

| File | Purpose |
|---|---|
| `session.py` | `ChatGPTSession` — persistent browser wrapper. Open once, reuse for multiple prompts. Supports `prompt()` (new chat) and `followup()` (same conversation). Uses DOM injection for long prompts to prevent content mangling. |
| `chatgpt_skill.py` | Send prompts, extract responses, save to `raw_md/`. Provides `save_response()` for initial response capture and `append_to_log()` for incrementally recording pipeline activity (extraction, execution, feedback) into the same markdown file. CLI entry point for standalone use. |
| `code_skill.py` | Extract code from `raw_md/`, save to `programs/`, compile/run, dependency management, smart timeout handling, verify loop. Main pipeline orchestrator. |
| `selectors.py` | All ChatGPT DOM selectors (update here when UI changes). |

## Usage

```bash
# First time: log in manually (cookies persist)
python chatgpt_skill.py --login

# Single prompt → save response
python chatgpt_skill.py --prompt "write a linked list in C" --headed

# Interactive mode (multi-turn in same chat)
python chatgpt_skill.py --interactive

# Extract code from a saved response
python code_skill.py extract raw_md/response.md --run

# Full pipeline: prompt → extract → compile/run
python code_skill.py pipeline "generate a python fizzbuzz" --headed

# Pipeline with verification loop (multi-turn, same conversation):
python code_skill.py pipeline "write a CAN bus driver in C" --verify --headed

# Limit retries
python code_skill.py pipeline "make me a calculator in C" --verify --max-retries 5

# Longer timeout for programs that take a while
python code_skill.py pipeline "write a web scraper" --verify --timeout 120

# Review past pipeline runs in raw_md/
ls raw_md/
```

## Key Features

### Persistent Browser Session
The browser opens once and stays open. Multiple prompts reuse the same window. No more 10-15 seconds of overhead per prompt for launching/closing Chromium.

### Platform-Aware Prompting
The initial prompt is automatically prepended with platform context: OS type, available tools, and constraints. On Windows, ChatGPT is told not to generate bash scripts. On all platforms, it's told to prefer stdlib over third-party packages and to declare dependencies explicitly.

### Multi-Turn Verification
When `--verify` is enabled, the fix request is sent as a follow-up in the SAME ChatGPT conversation. ChatGPT already has full context of what it generated. The feedback prompt only includes the errors — not the entire previous response. This is faster and uses less of the context window.

### Smart Timeout Handling
Not all timeouts are failures. A random number generator writing CSV rows indefinitely is *working correctly* when killed at 30 seconds — it produced output, it just runs forever by design.

The system uses `subprocess.Popen` for streaming output capture and classifies timeouts by outcome:

- **Program produced stdout before timeout** → `SUCCESS` (it was working, we stopped it)
- **Program created or modified files before timeout** → `SUCCESS` (it was writing output to disk, even without stdout)
- **Program produced nothing and created no files** → `FAILURE` (hung or broken)
- **Program produced only stderr** → `FAILURE` (crashing slowly)

Additionally, `classify_program()` does static analysis to detect likely long-running patterns (`while True`, servers, polling loops, `input()` calls) and annotates the result so the verify loop doesn't waste retries trying to "fix" intentionally infinite programs.

Use `--timeout` to adjust the limit: `--timeout 120` for slow operations, `--timeout 10` for quick scripts.

### Plain-Text Feedback (No Markdown Mangling)
Feedback prompts use plain text only — no triple backticks, no bold, no headings. This prevents ChatGPT's contenteditable input from eating or mangling the error output. Previous versions sent markdown-formatted feedback that arrived empty or truncated.

### Dependency Detection & Installation
Before running Python files, `detect_missing_imports()` scans for imports that aren't installed. If missing packages are found, the pipeline prompts you for permission before installing with pip. Dependencies installed this way don't count as a retry — the code is re-run immediately after installation.

ChatGPT is also instructed to declare dependencies in a `DEPENDENCIES: pkg1, pkg2` line, which the pipeline detects and offers to install before execution.

### C/C++ Compilation
`run_file()` detects `.c` and `.cpp` files, compiles them with `gcc`/`g++`, and runs the resulting binary. Compiler errors (with line numbers, warnings, etc.) are captured and fed back to ChatGPT on failure.

### Windows Compatibility
Shell scripts (`.sh`) are handled properly on Windows: routed through WSL or Git Bash if available, or failed with a clear error message instead of the cryptic `/bin/bash: C:UsersJonat...` path mangling.

### Filename Extraction
ChatGPT responses are scanned for filename hints — bold names like **CAN.c**, backtick names like `main.c`, or heading names like `### CAN.h`. Multiple hints are matched to code blocks by extension, so multi-file projects get saved with correct names.

### Honest Status Reporting
The pipeline reports SUCCESS only when code actually ran and exited cleanly (or produced output before a timeout). Skipped files (unknown extensions, unrunnable formats) no longer silently count as "passed." If max retries are exhausted, the pipeline says FAILED — not "complete."

### Comprehensive raw_md Pipeline History
Each pipeline run produces a rich markdown file in `raw_md/` that captures the entire lifecycle in human-readable form:

1. **Prompt sent** — the full text sent to ChatGPT (including platform context)
2. **ChatGPT response** — the complete response as received
3. **Code extraction** — which blocks were found, languages, sizes
4. **Saved files** — where code was written to disk
5. **Execution results** — stdout, stderr, exit codes, timeout behavior
6. **Feedback prompts** — the exact error text sent back to ChatGPT (on retry)
7. **Retry responses** — subsequent ChatGPT responses appended to the master file
8. **Final status** — SUCCESS or FAILED with reason
9. **Run Record (JSON)** — structured machine-readable summary embedded at the end, containing: attempt count, file paths, execution result summaries (truncated stdout/stderr), dependencies installed, timing, and final status

The first attempt's file serves as the "master log" for the entire pipeline run — all retry activity is appended to it, including the final JSON run record. Individual retry attempts also get their own standalone files.

**Important:** Only error messages are sent to ChatGPT as feedback. The raw_md file captures everything for your reference, but the ChatGPT conversation stays lean to preserve context window.

## Architecture: session.py

```python
from session import ChatGPTSession

with ChatGPTSession(headed=True) as s:
    # First prompt starts a new chat
    r1 = s.prompt("Write me a fizzbuzz in Python")

    # Follow-up in the same conversation (multi-turn)
    r2 = s.followup("Now make it count by 3s instead")

    # Explicitly start a new chat
    s.new_chat()
    r3 = s.prompt("Write a linked list in C")
```

The session wraps Playwright lifecycle, handles navigation, typing, send button detection, stream waiting, and response extraction — all in a single reusable object. Long prompts (>200 chars) are injected via JavaScript DOM manipulation to prevent Playwright's `fill()`/`type()` from stripping newlines in the contenteditable div.

## Dependencies

```
pip install playwright
playwright install chromium
```

No frameworks. No agents. No LangChain. Just scripts.

---

## Roadmap

### The North Star

A skill-based automation system where LLMs reason and generate, local scripts execute and build, and hardware measurements verify. Nothing trusts the AI's claims. Every result is checked against reality.

The concrete endgame for embedded work:

- Prompt ChatGPT: "Write me an STM32 CAN bus driver in C"
- VerifyBot extracts the code, saves it as `CAN.c`
- VerifyBot compiles it with `arm-none-eabi-gcc`
- VerifyBot flashes it to the STM32 (or at minimum, verifies it compiles)
- If it fails, VerifyBot sends the compiler errors back to ChatGPT
- ChatGPT fixes it, VerifyBot tries again
- Eventually: flash → read serial output → verify behavior matches spec

### What's Been Built

| Feature | Status |
|---------|--------|
| Single-shot prompting, code extraction, execution | **Done** |
| Verification loop (re-prompt on failure) | **Done** |
| Persistent browser session (`session.py`) | **Done** |
| Multi-turn conversation (follow-ups in same chat) | **Done** |
| C/C++ compilation support | **Done** |
| Multi-file filename extraction | **Done** |
| Comprehensive raw_md pipeline history logging | **Done** |
| Plain-text feedback (fix markdown mangling bug) | **Done** |
| Dependency detection & install with permission | **Done** |
| Platform-aware prompting (Windows/Linux context) | **Done** |
| Windows .sh handling (WSL/Git Bash routing) | **Done** |
| Honest success/failure reporting | **Done** |
| Smart timeout handling (streaming output capture) | **Done** |
| File-creation-aware timeout (detect disk writes without stdout) | **Done** |
| Static analysis for long-running program detection | **Done** |
| Rename: `programs/` dir, `program_N` filenames | **Done** |
| Comprehensive raw_md pipeline history logging | **Done** |

### Next: Output Validation
> *Exit code 0 is not enough — verify the output makes sense*

The rocket sim problem: code runs, exits 0, prints `Apogee: 0.0 m` — clearly wrong, but the pipeline calls it "success." Exit codes only catch crashes, not logical errors.

Planned:

- **Output assertions**: Task files specify expected patterns (`output_contains: "Apogee"`, `output_not_contains: "0.0 m"`)
- **Sanity checks**: Detect obviously-wrong outputs (all zeros, empty output, NaN/Inf values)
- **ChatGPT-as-judge**: Send the output back to ChatGPT: "does this look correct for a rocket simulation?"
- **Test harness**: If ChatGPT generates tests alongside implementation, run them as the success criterion

### Future: Cross-Compilation & Hardware
> *Compile for ARM, flash MCUs, read serial output*

- Configurable compiler (`arm-none-eabi-gcc` for STM32)
- Makefile detection — if a Makefile is among extracted blocks, use `make`
- Flash integration via OpenOCD/STM32CubeProgrammer
- Serial output capture and verification
- CAN bus message validation

### Future: Task Recipes
> *Define reusable pipelines as YAML files*

```yaml
name: STM32 CAN Bus Driver
prompt: |
  Write a CAN bus driver for STM32L476RG using HAL.
  Must support standard 11-bit IDs, 500kbps baud rate.
compiler: arm-none-eabi-gcc
flags: [-mcpu=cortex-m4, -mthumb, -Wall]
success_criteria:
  - compiles_clean: true
  - output_contains: "CAN loopback test PASSED"
max_retries: 5
timeout: 60
```

### Future: Multi-LLM Support
> *Same pipeline, different brains*

- Claude browser support (different selectors, same `session.py` pattern)
- Local LLM support (Ollama, llama.cpp) via HTTP endpoint
- Model comparison: run same task through multiple LLMs, compare results

---

## Known Issues & Lessons Learned

### Playwright + ChatGPT Contenteditable Div
Playwright's `fill()` and `type()` methods strip newlines and mangle multi-line text when used on ChatGPT's `contenteditable` prompt textarea. Solution: for prompts >200 chars, inject content via JavaScript DOM manipulation (`createElement('p')` per line) to preserve structure.

### Markdown in Feedback Prompts
ChatGPT's input field interprets markdown formatting. Triple backticks, `###` headings, and `**bold**` in feedback prompts get rendered/eaten instead of passed through as text. Solution: all feedback prompts use plain text only.

### Windows Path Mangling
`bash C:\path\to\file.sh` on Windows produces `/bin/bash: C:pathtofile.sh` because backslash path separators get stripped. Solution: detect OS, route through WSL/Git Bash, or skip with a clear error.

### Exit Code 0 ≠ Correct
A program that runs without crashing isn't necessarily correct. A rocket simulator outputting `Apogee: 0.0 m` exits cleanly but is obviously wrong. Output validation (Next phase) will address this.

### Timeout ≠ Failure
A continuous data generator killed at 30 seconds isn't broken — it was doing its job. The smart timeout system now captures streaming output via `Popen` and classifies the outcome based on whether the program was actually producing useful output before being killed. It also snapshots the working directory before and after execution to detect programs that write to files (e.g. CSV output) even when stdout is empty or buffered. Long-running patterns (`while True`, servers, polling loops) are detected via static analysis.

### Dependency Hell
ChatGPT loves generating code with `numpy`, `matplotlib`, `requests`, etc. without checking if they're installed. Pre-execution import scanning catches this before wasting a retry cycle on a `ModuleNotFoundError`.
