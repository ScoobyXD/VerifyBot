# LLM.md — Browser-Automated LLM Skill System

## What This Is

A set of Python scripts that turn ChatGPT's browser UI into a programmable code generation, execution, and verification pipeline. No API keys. No credits. Just Playwright driving the same browser window you'd use manually.

The system sends prompts to ChatGPT, scrapes the response, extracts code blocks, saves them as real files, runs them, and — if they fail — packages the original response and error output together and sends it back to ChatGPT for a fix. All from one command.

## Why

LLM APIs cost money. ChatGPT Plus and Claude Pro are flat-rate subscriptions you're already paying for. The web UI is functionally the same model behind the same context window — it's just wrapped in a browser instead of a REST endpoint.

This project treats the browser UI as the endpoint. Everything a human does — type a prompt, wait for the response, copy the code, paste it into a file, run it — is automated with Playwright. The result is a zero-cost LLM integration layer that runs entirely on your laptop.

The broader goal is a **skill-based automation system** where LLMs reason and generate, local scripts execute and build, and hardware measurements verify. No step trusts the AI's claims. Every result is checked against reality — exit codes, bus traffic, logs, actual outputs. This browser automation layer is the first piece: giving scripts access to LLM reasoning without paying per-token.

## How It Works

```
You run a command
        │
        ▼
  chatgpt_skill.py
  (Playwright opens browser, types prompt, waits for response)
        │
        ▼
  Response saved to raw_md/
  (with code fences reconstructed from DOM)
        │
        ▼
  code_skill.py
  (extracts code blocks, detects language, finds filename hints)
        │
        ▼
  Code saved to generated/
        │
        ▼
  Executed, output captured
        │
        ▼ (if --verify and code failed)
  Package raw_md + execution output
        │
        ▼
  Send back to ChatGPT for fix
  (loops up to --max-retries times)
```

## Directory Structure

| Directory | Contents |
|---|---|
| `raw_md/` | ChatGPT responses saved as timestamped markdown (the "raw" LLM output) |
| `generated/` | Extracted code files ready to run |
| `.browser_profile/` | Persistent Chromium login cookies |

## Files

| File | Purpose |
|---|---|
| `chatgpt_skill.py` | Browser automation — send prompts, extract responses, save to `raw_md/` |
| `code_skill.py` | Parse code blocks from `raw_md/`, save to `generated/`, run, and verify |
| `selectors.py` | All ChatGPT DOM selectors in one place (update here when UI changes) |

## Usage

```bash
# First time: log in manually (cookies persist)
python chatgpt_skill.py --login

# Send a prompt and save the response to raw_md/
python chatgpt_skill.py --prompt "write a linked list in C" --headed

# Extract code from a raw_md response into generated/
python code_skill.py extract raw_md/response.md

# Extract and run
python code_skill.py extract raw_md/response.md --run

# Full pipeline: prompt → extract → save to generated/ → run
python code_skill.py pipeline "generate a python fizzbuzz" --headed

# Full pipeline WITH verification loop:
# If code fails, sends raw_md + error back to ChatGPT for a fix
python code_skill.py pipeline "generate a python fizzbuzz" --verify --headed

# Limit retries
python code_skill.py pipeline "generate a python fizzbuzz" --verify --max-retries 5
```

## Verification Loop

The `--verify` flag enables the feedback loop. When generated code fails:

1. The system reads the original `raw_md/` file (ChatGPT's full response)
2. Reads each generated code file
3. Captures stdout, stderr, and exit code from execution
4. Packages all of it into a structured follow-up prompt
5. Sends it back to ChatGPT: "here's what you gave me, here's the error, fix it"
6. Extracts the new code, runs it again
7. Repeats up to `--max-retries` times (default: 3)

This is the error-driven retry loop — the system doesn't trust ChatGPT's claims, it verifies against reality and feeds errors back until the code actually works.

## Design Decisions

**Persistent browser profile.** You log in once. Cookies are saved to `.browser_profile/`. Every subsequent run reuses the session. No re-authentication.

**raw_md as the source of truth.** ChatGPT's full response is always saved to `raw_md/` before any extraction happens. This is the unmodified LLM output. Code extraction from `raw_md/` into `generated/` is a separate step, so you always have the original to reference or re-extract from.

**DOM-level code extraction.** ChatGPT's `inner_text()` strips backtick fences. The scraper reads `<pre><code>` elements directly, pulls the language from CSS classes like `language-python`, and reconstructs proper fenced markdown. This means saved responses have clean code blocks that parse reliably.

**Fallback regex for raw scrapes.** Older responses (or edge cases where DOM extraction misses) are handled by a loose regex that matches `language\nCopy code\n...` patterns. Both extraction paths feed into the same `CodeBlock` pipeline.

**Filename hints.** The extractor scans for phrases like "save as calculator.html" in the response text and uses them as the output filename. Falls back to `block_0.py` etc.

**Selectors are centralized.** ChatGPT changes its frontend constantly. All DOM selectors live in `selectors.py`. When it breaks, update one file.

## What This Doesn't Do (Yet)

- **File upload to ChatGPT** — sending code/images as context for the prompt
- **Multi-turn conversations** — continuing a thread instead of starting fresh each time
- **Hardware integration** — flashing MCUs, sniffing CAN buses, verifying against real measurements
- **Claude browser support** — same approach, different selectors

These are all future skills that compose on top of what's here.

## Dependencies

```
pip install playwright
playwright install chromium
```

That's it. No frameworks. No agents. No LangChain. Just scripts.
