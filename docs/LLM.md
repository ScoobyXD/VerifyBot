# Agent v2 -- LLM-Driven Software/Hardware Loop

## What This Is

Automates the LLM-to-hardware debugging loop: sends prompts to ChatGPT via browser automation, extracts code, executes on local machine or Raspberry Pi over SSH, feeds stdout/stderr back for retry. The LLM is the brain; Agent is just hands on the keyboard.

## Quick Start

1. Python 3.10+, a ChatGPT account (browser-based, no API key needed), and optionally a Raspberry Pi.
2. Run `python main.py "hello"` -- first run triggers a setup wizard (deps, Chromium, Pi SSH creds, ChatGPT login, directories).
3. After setup: `python main.py "write a fizzbuzz"` or `python main.py "blink LED" --target raspi`.
4. Re-login: `python main.py --login`. Re-setup: delete `core/.setup_complete`. Tests: `python tests.py`.
5. Dotfiles lose leading dots when downloaded from Claude.ai -- rename `gitignore` to `.gitignore`, `env` to `.env`.

## Pipeline

1. **Probe** target (SSH diagnostics or local context) -> `context/raspi.md` or `context/local.md`
2. **Prompt** ChatGPT with system context + user task via Playwright browser automation
3. **Extract** fenced code blocks (```python, ```bash) from response using regex + DOM parsing
4. **Execute** scripts on target (SFTP upload + SSH for Pi, subprocess for local)
5. **Verify** by sending stdout/stderr back to ChatGPT; LLM decides PASS/FAIL/REVISE
6. **Retry** in same conversation until success or max retries hit

## Directory Structure

```
verifybot/
├── main.py                 # Entry point + full pipeline
├── tests.py                # E2E test suite (parallel streams, LLM assertion)
├── core/
│   ├── selectors.py        # ChatGPT DOM selectors (update when UI changes)
│   ├── session.py          # Persistent browser session (prompt/followup/new_chat)
│   ├── setup.py            # First-time setup wizard
│   ├── ui.py               # Flask+SocketIO workbench UI (multi-agent panels)
│   ├── artifact_sweep.py   # Post-execution cleanup (moves outputs to outputs/)
│   └── .setup_complete     # Marker file (gitignored)
├── skills/
│   ├── chatgpt_skill.py    # Browser wrapper, raw_md saving, login mode
│   ├── ssh_skill.py        # SSH/SFTP to Pi via paramiko
│   └── extract_skill.py    # Code block extraction + classification
├── context/                # Auto-generated target snapshots
├── programs/               # Extracted scripts (versioned: slug_1.py, slug_2.py)
├── outputs/                # Execution results (versioned: slug_1.txt, slug_2.txt)
├── uploads/                # User-uploaded files for agent context
├── raw_md/                 # Full pipeline transcripts (timestamped .md logs)
├── .env                    # Pi SSH creds (PI_USER, PI_HOST, PI_PASSWORD)
├── .browser_profile/       # Chromium cookies (persistent ChatGPT login)
└── .gitignore
```

## Import Map

```
main.py -> core.setup, core.session, core.artifact_sweep,
           skills.chatgpt_skill, skills.ssh_skill, skills.extract_skill
skills/chatgpt_skill.py -> core.selectors, core.session
core/session.py -> core.selectors
core/ui.py -> main (run_pipeline, run_followup_pipeline), core.session, skills.chatgpt_skill
core/setup.py, core/artifact_sweep.py, skills/ssh_skill.py, skills/extract_skill.py, core/selectors.py -- no internal imports
tests.py -> main (run_pipeline), core.session (for LLM assertion)
```

## Key Design Decisions

- **LLM is the brain**: Agent doesn't diagnose errors or pick strategies. It captures output and sends it back. As LLMs improve, the tool improves for free.
- **LLM verifies, not exit codes**: Full stdout/stderr goes to ChatGPT which responds PASS/FAIL/REVISE. Even exit-0 with wrong output gets caught.
- **Versioned files, never overwrite**: Programs and outputs use `_1`, `_2`, `_3` suffixes. Duplicates auto-detected and skipped.
- **Local = Python only**: Local target forces Python (uses subprocess/os/pathlib for everything). Eliminates bash-on-Windows issues. Pi target supports bash/Python/C/C++.
- **Browser-based, not API**: Playwright automates ChatGPT's UI. No API keys, no per-token costs. Response detection uses stability checks (content stops growing for 5s).
- **Prompt engineering**: LLM told to put ALL code in one fenced block, no text after closing fence. Also supports `TIMEOUT: N` hints for long-running scripts.
- **Context injection**: Before first prompt, probes target (hostname, Python version, pip list, GPIO/I2C state for Pi) and injects as system context.
- **Model escalation**: Defaults to Instant (fast). After 3 failures, auto-escalates to Thinking with the FULL raw_md transcript as context. Thinking sees every failed attempt and is told to try a different approach. Centralized model config in selectors.py so model URL params update in one place.
- **Live streaming**: Pi execution streams stdout/stderr in real-time via `ssh_run_live()`. Local shows source + command + output + exit status.
- **CRLF normalization**: Strips `\r` before saving to `programs/` to prevent bash errors on Pi.
- **Artifact sweep**: After local execution, diffs filesystem for new non-code files, moves them to `outputs/`. Code files stay in place.

## Workbench UI

`core/ui.py` serves a Flask+SocketIO web UI at `http://127.0.0.1:5000`:
- Infinite canvas with draggable, resizable agent panels (drag header, resize from any edge/corner)
- Each agent panel shows split-view: live terminal output (left) + live ChatGPT browser screenshot (right)
- Follow-up prompts within each panel (multi-turn in same ChatGPT conversation)
- File upload via drag-drop, paste, or attach button (uploaded to `uploads/`, text previews injected into prompt)
- Toolbar dropdowns for History (raw_md), Programs, Outputs, Uploads -- each with Clear All button
- Run Tests button spawns agent panels per test stream with live output and browser view
- Each agent gets its own cloned browser profile (avoids Playwright lock conflicts)
- Profiles cleaned up on agent close; `.browser_profiles/` dir purged after tests

## CLI Flags

| Flag | Default | What it does |
|------|---------|-------------|
| `"prompt"` | required | Natural language task |
| `--target` | auto | Force `local` or `raspi` |
| `--headless` | OFF | Hide browser |
| `--max-retries N` | 3 | Retry attempts |
| `--timeout N` | 30 | Execution timeout (LLM can override via TIMEOUT: hint) |
| `--remote-dir` | ~/Documents | Pi working directory |
| `--login` | -- | Manual ChatGPT login |
| `--model` | instant | ChatGPT model: `instant`, `thinking`, or `auto` |
| `--no-escalate` | OFF | Disable automatic escalation to Thinking on failure |

## Model Escalation (Instant -> Thinking)

Agent defaults to ChatGPT 5.3 Instant for speed. If Instant fails after max retries (default 3), Agent automatically escalates to GPT-5.2 Thinking:

1. **Instant runs first** (fast, 3 retries). If it passes, done.
2. **On failure**, Agent reads the ENTIRE raw_md transcript from the Instant run.
3. **Opens a new ChatGPT session** using the Thinking model (via `?model=gpt-5.2-thinking` URL parameter).
4. **Injects the full transcript** as context -- every prompt, response, code, output, and error. Thinking sees exactly what Instant tried and what went wrong.
5. **Thinking gets 2 attempts** (configurable). The prompt explicitly tells it NOT to repeat the same mistakes and to try a different approach.
6. **All attempts logged** to the same raw_md file under "Escalation" headers.

Escalation scripts use offset filenames (e.g. `slug_101.py`) so they don't collide with Instant's versions in `programs/`.

Model selection is centralized in `core/selectors.py` (the `MODELS` dict and `model_url()` function). When ChatGPT updates model names, change them there only.

### Override Examples

```bash
# Default: Instant with auto-escalation
python main.py "write fizzbuzz"

# Skip Instant, go straight to Thinking
python main.py "complex task" --model thinking

# Instant only, no escalation
python main.py "simple task" --no-escalate

# Force Thinking, no escalation (Thinking only)
python main.py "hard problem" --model thinking --no-escalate
```

## Dependencies

Auto-installed by setup wizard. Manual: `pip install playwright paramiko flask flask-socketio && playwright install chromium`.

## Rules

1. ASCII only in generated code/output. No emojis or Unicode symbols.
2. SSH creds in `.env` (gitignored). Never hardcode.
3. Skill files use `_skill` suffix. `__init__.py` required in `core/` and `skills/`.
4. `context/` is auto-generated, don't manually edit. `programs/` and `outputs/` keep ALL versions.
5. `raw_md/` transcripts embed scripts + outputs inline chronologically.
6. `.setup_complete` marker = setup done. Delete to re-run. Don't commit.
7. `.browser_profile/` = saved cookies. `--login` to re-auth. Don't delete unless re-logging in.
8. `main.py` auto-cleans `__pycache__/` on startup.

## Test System

- Tests in `TESTS` list in `tests.py`, grouped by `stream` field. Each stream = own thread + own browser + own agent panel.
- Dependent tests share a stream (`depends_on: N`). Independent tests get separate streams for parallelism.
- Each stream clones `.browser_profile/` into `.browser_profile_X/` (Playwright needs exclusive user_data_dir). Cloned profiles cleaned after tests.
- UI test runner (`Run Tests` button) creates agent panels per stream showing live terminal + browser screenshots.
- All pages force-closed on stream completion to prevent orphaned chrome tabs. Profile dirs purged after all streams finish.
- `raw_md/test.md` records all results. After streams finish, summary table + LLM assertion verdict appended.
- Results sorted by test number. Error messages truncated to first line in test.md.
