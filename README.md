# VerifyBot

An LLM-driven hardware debug loop that turns natural language commands into deployed, executed code — on a Raspberry Pi or your local machine.

## What it does

You describe what you want in plain English. VerifyBot handles the rest:

1. **Probes** the target machine for system context
2. **Builds** a context-rich prompt and sends it to ChatGPT via browser
3. **Extracts** code blocks from the response
4. **Deploys and executes** the code on the target
5. **Retries** automatically if execution fails, feeding raw output back to the LLM
6. **Logs** everything to `raw_md/`

The LLM is the brain. VerifyBot is just hands on the keyboard.

## Usage

```bash
# Run a task on the default target (Raspberry Pi)
python main.py "make a random word generator that saves to a text file"

# Debug hardware
python main.py "why is my I2C sensor not responding"

# Kill a running script
python main.py "kill the infinite counter script"

# Run locally instead of on the Pi
python main.py "write a fizzbuzz" --target local
```

## Project Structure

```
VerifyBot/
├── main.py               # Entry point — the main debug loop
├── core/
│   ├── session.py        # ChatGPT browser session management
│   ├── setup.py          # First-run setup wizard
│   ├── artifact_sweep.py # Cleans up generated artifacts
│   └── selectors.py      # Browser DOM selectors
├── skills/
│   ├── chatgpt_skill.py  # Saving/logging LLM responses
│   ├── extract_skill.py  # Extracting code blocks from responses
│   └── ssh_skill.py      # SSH deploy & execute on remote targets
├── tests.py              # Test suite
├── context/              # System context snapshots
├── docs/                 # Documentation
├── outputs/              # Execution outputs
└── raw_md/               # Raw LLM response logs
```

## Requirements

- Python 3.8+
- A running ChatGPT session accessible via browser automation
- SSH access to a Raspberry Pi (for remote targets) or run with `--target local`

## First Run

On first run, VerifyBot will launch a setup wizard to configure your target and browser session. You can also trigger it manually:

```bash
python main.py --login
```

## How it works

VerifyBot uses browser automation to interact with ChatGPT, so no API key is required. It injects system context (OS info, running processes, recent errors) into each prompt to give the LLM accurate grounding before generating code.

Generated code is deployed over SSH (or run locally), stdout/stderr is captured, and on failure the raw output is fed back for a retry loop until the task succeeds or a retry limit is hit.
