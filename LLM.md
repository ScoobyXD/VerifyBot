# LLM.md — AI-Driven Hardware Self-Verification System

## What This Is

A set of Python scripts that turn ChatGPT's browser UI into a programmable code generation, execution, and verification pipeline — extended to **deploy code to real hardware** (Raspberry Pi 5 + STM32L476RG) and verify it works.

The system sends a prompt to ChatGPT, scrapes the response, extracts code blocks, **identifies which code goes to which target** (Pi vs STM32), deploys each file to the correct machine's filesystem, executes/flashes it, captures terminal output from both targets, and — if anything fails — sends all the output back to ChatGPT in the same conversation for a fix. All from one command.

## Why

LLMs generate embedded code that compiles but doesn't work. The only way to know if it actually works is to run it on the hardware and observe what happens. This system closes that loop automatically.

## The Fundamental Problem

the fundamental task is from a LLM prompt and response, how to do then have it create distinct raspberry pi, or stm32 code, and those code will be properly copied and pasted onto the correct memory. For example, i say"spi to canbus between raspi and stm32" to chatgpt, it generates the code for it, gets put into md file, then extracted into distinct raspi and stm32 programs. Those files will be written to the correct raspi and stm32 file system, then have those respective machines execute, then maybe we conduct a test, send a raspi signal to stm32 and a stm32 signal to raspi, then we verify each result. Then we somehow get the terminal or error/success message from each raspi and stm32 and add to md history like we do now, then send it back to LLM

```
You say: "SPI to CAN bus between raspi and stm32"
                    │
                    ▼
        ChatGPT generates a response containing
        BOTH raspi code AND stm32 code
                    │
                    ▼
        Response saved to raw_md/ (same as before)
                    │
                    ▼
        Code blocks extracted (same as before)
                    │
                    ▼
   ┌────── NEW PROBLEM: Which code goes where? ──────┐
   │                                                   │
   │  Block 1: pi_can.py      → Raspberry Pi           │
   │  Block 2: stm32_main.c   → STM32 (via Pi)        │
   │  Block 3: stm32_can.h    → STM32 (via Pi)        │
   │                                                   │
   └───────────────────────────────────────────────────┘
                    │
          ┌────────┴────────┐
          ▼                 ▼
   Pi filesystem       Pi filesystem (staging)
   /home/pi/hw/pi/     /home/pi/hw/stm32/
   └─ pi_can.py        ├─ stm32_main.c
                        └─ stm32_can.h
          │                 │
          ▼                 ▼
   Pi executes         Pi cross-compiles
   python3 pi_can.py   arm-none-eabi-gcc → .elf
                              │
                              ▼
                        Pi flashes STM32
                        openocd → program .elf
          │                 │
          ▼                 ▼
   Pi stdout/stderr    STM32 UART output
   captured            captured by Pi
          │                 │
          └────────┬────────┘
                   ▼
        BOTH outputs appended to raw_md/
        (just like we do now for local execution)
                   │
                   ▼
        If failed → send BOTH outputs back to
        ChatGPT in same conversation → retry
```

## What's New vs. What Already Exists

**Already built (works today)**:
- Browser automation: `session.py`, `chatgpt_skill.py`, `selectors.py`
- Code extraction from responses: `code_skill.py`
- Save to `raw_md/`, extract to `programs/`
- Compile/run locally, capture stdout/stderr
- Verification loop: send errors back to ChatGPT, retry in same conversation
- Comprehensive pipeline history logging

**New problems to solve**:

| # | Problem | Solution |
|---|---------|----------|
| 1 | **Target identification** — which extracted code block goes to Pi vs STM32? | Filename hints + language heuristics (`.py` → Pi, `.c/.h` → STM32, or explicit filename like `pi_*.py`, `stm32_*.c`) |
| 2 | **Remote file deployment** — code needs to land on Pi's filesystem, not laptop's | SSH/SCP from laptop to Pi |
| 3 | **Cross-compilation** — STM32 C code must compile with `arm-none-eabi-gcc` on the Pi | Remote `ssh pi "cd /home/pi/hw/stm32 && make"` |
| 4 | **Flashing** — compiled .elf must get onto the STM32 | Remote `ssh pi "openocd ... -c 'program main.elf verify reset exit'"` |
| 5 | **Remote execution** — Pi-side scripts run on Pi, not laptop | Remote `ssh pi "cd /home/pi/hw/pi && python3 pi_can.py"` |
| 6 | **Dual output capture** — need stdout/stderr from Pi execution AND UART output from STM32 | Pi captures its own stdout + reads `/dev/ttyAMA0` for STM32 UART |
| 7 | **Combined feedback** — both outputs must go into raw_md and back to ChatGPT | Same `append_to_log()` and `build_feedback_prompt()`, just with two sources |

## How Target Identification Works

When ChatGPT responds to a hardware prompt, it typically labels its code blocks clearly:

```markdown
### Pi Side (`pi_can.py`)
   ```python
   import socket
   ...
   ```

### STM32 Side (`stm32_main.c`)
   ```c
   #include "stm32l4xx_hal.h"
   ...
   ```
```

**Classification rules** (in order of priority):

1. **Filename hint contains target prefix**: `pi_*.py` → Pi, `stm32_*.c` → STM32
2. **Filename extension**: `.py`, `.sh` → Pi; `.c`, `.h`, `.s`, `.ld` → STM32
3. **Content heuristics**: `import` at top → Pi; `#include "stm32` → STM32; `HAL_` functions → STM32
4. **Prompt augmentation**: We tell ChatGPT to name files with prefixes (see prompt template below)

The prompt template forces clear separation:

```
[HARDWARE CONTEXT]
You are generating code for TWO targets:

TARGET 1 — Raspberry Pi 5 (Linux, Python 3):
  - Name all Pi files starting with "pi_" (e.g. pi_can_send.py)
  - Available: socketCAN, spidev, RPi.GPIO, python3, bash
  - CAN interface: can0 (MCP2515 on SPI0, 500kbps)

TARGET 2 — STM32L476RG (bare-metal C with HAL):
  - Name all STM32 files starting with "stm32_" (e.g. stm32_main.c)
  - Compiler: arm-none-eabi-gcc
  - UART2 on PA2 TX at 115200 baud for debug output
  - MUST print status on UART at each stage (init, periph setup, data events)

Return COMPLETE files. Label each file clearly.

[TASK]
<user prompt here>
```

## How Remote Execution Works

Everything goes through SSH. The Pi is just a remote machine that runs commands.

```python
# remote_exec.py — the only new infrastructure file

class RemoteTarget:
    """SSH/SCP wrapper for the Raspberry Pi."""
    
    def __init__(self, host, user, key_path):
        # Uses subprocess calling ssh/scp (simplest, no paramiko needed)
    
    def upload(self, local_path, remote_path):
        """scp local_path user@host:remote_path"""
    
    def run(self, command, timeout=30) -> dict:
        """ssh user@host 'command' → {stdout, stderr, exit_code}"""
    
    def download(self, remote_path, local_path):
        """scp user@host:remote_path local_path"""
```

That's it. Three methods. Everything else is just calling `remote.run()` with the right command:

```python
# Deploy Pi code
remote.upload("programs/pi_can_send.py", "/home/pi/hw/pi/pi_can_send.py")

# Deploy STM32 code  
remote.upload("programs/stm32_main.c", "/home/pi/hw/stm32/stm32_main.c")

# Cross-compile STM32 code (on Pi)
result = remote.run("cd /home/pi/hw/stm32 && arm-none-eabi-gcc -mcpu=cortex-m4 -mthumb -o main.elf stm32_main.c startup.s -T stm32l476.ld -lnosys")

# Flash STM32 (from Pi)
result = remote.run("openocd -f interface/raspberrypi-native.cfg -f target/stm32l4x.cfg -c 'program /home/pi/hw/stm32/main.elf verify reset exit'")

# Run Pi-side script
result = remote.run("cd /home/pi/hw/pi && python3 pi_can_send.py")

# Capture STM32 UART output
result = remote.run("timeout 10 cat /dev/ttyAMA0")
```

## How Dual Output Capture Works

After deployment, we need output from two places:

**Pi execution output**: stdout/stderr from running `pi_can_send.py` — captured directly by `remote.run()`.

**STM32 output**: Whatever the STM32 prints on UART — captured by Pi reading `/dev/ttyAMA0`.

The sequence for a CAN roundtrip test:

```python
# 1. Start UART listener in background on Pi
remote.run("timeout 15 cat /dev/ttyAMA0 > /tmp/stm32_uart.log 2>&1 &")

# 2. Small delay for STM32 to boot and print init messages
time.sleep(2)

# 3. Run Pi-side script (sends CAN frame, maybe also listens)
pi_result = remote.run("cd /home/pi/hw/pi && python3 pi_can_send.py", timeout=20)

# 4. Grab the STM32 UART log
remote.download("/tmp/stm32_uart.log", "programs/stm32_uart.log")
stm32_output = Path("programs/stm32_uart.log").read_text()

# Now we have:
#   pi_result['stdout']  — what the Pi script printed
#   pi_result['stderr']  — any Pi-side errors
#   stm32_output         — what the STM32 printed on UART
```

Both get appended to the raw_md file:

```python
append_to_log(raw_md_path, "Pi Execution Output", f"""
stdout:
{pi_result['stdout']}

stderr:
{pi_result['stderr']}

exit code: {pi_result['exit_code']}
""")

append_to_log(raw_md_path, "STM32 UART Output", f"""
{stm32_output}
""")
```

## How Feedback Works (Same as Before, Just Two Sources)

The feedback prompt sent back to ChatGPT now includes output from both targets:

```python
def build_hardware_feedback(pi_results, stm32_uart, compile_result, flash_result):
    lines = []
    lines.append("The code you generated has issues. Here are the results from BOTH targets:")
    lines.append("")
    
    # Compilation result (if it failed, this is all we need)
    if not compile_result['success']:
        lines.append("--- STM32 COMPILATION FAILED ---")
        lines.append(f"Compiler output:")
        lines.append(compile_result['stderr'])
        lines.append("")
        lines.append("Fix the STM32 code. Return ALL complete files.")
        return "\n".join(lines)
    
    # Flash result
    if not flash_result['success']:
        lines.append("--- STM32 FLASH FAILED ---")
        lines.append(f"OpenOCD output:")
        lines.append(flash_result['stderr'])
        lines.append("")
        lines.append("Fix the issue. Return ALL complete files.")
        return "\n".join(lines)
    
    # Both sides ran — report what happened
    lines.append("--- Pi execution (pi_can_send.py) ---")
    if pi_results.get('stderr'):
        lines.append(f"STDERR: {pi_results['stderr'][:1500]}")
    if pi_results.get('stdout'):
        lines.append(f"STDOUT: {pi_results['stdout'][:1500]}")
    lines.append(f"Exit code: {pi_results.get('exit_code', '?')}")
    lines.append("")
    
    lines.append("--- STM32 UART output (captured from /dev/ttyAMA0) ---")
    if stm32_uart:
        lines.append(stm32_uart[:1500])
    else:
        lines.append("(no UART output received — STM32 may not be printing, or UART is misconfigured)")
    lines.append("")
    
    lines.append("Fix the code for both targets. Return ALL complete files.")
    return "\n".join(lines)
```

This goes straight into `session.followup()` — same multi-turn conversation, same as the existing verify loop.

## Modified Pipeline Flow

The existing `code_skill.py` pipeline becomes:

```
Step 1: Prompt ChatGPT (same as before)
Step 2: Extract code blocks (same as before)
Step 3: Classify blocks → Pi files vs STM32 files (NEW)
Step 4: Deploy
         ├─ Pi files:   SCP to Pi, execute, capture stdout/stderr (NEW)
         └─ STM32 files: SCP to Pi, cross-compile, flash (NEW)
Step 5: Capture UART from STM32 (NEW)
Step 6: Append ALL outputs to raw_md (same pattern, new sources)
Step 7: If failed → build feedback from both targets → followup (same pattern, new content)
```

Steps 1, 2, 6, 7 are the existing pipeline with minor modifications. Steps 3-5 are new.

## Files

### Existing (unchanged)

| File | Purpose |
|---|---|
| `session.py` | Persistent browser wrapper — `prompt()`, `followup()`, `new_chat()` |
| `chatgpt_skill.py` | Send prompts, extract responses, save to `raw_md/`, `append_to_log()` |
| `selectors.py` | ChatGPT DOM selectors |

### Existing (modified)

| File | Change |
|---|---|
| `code_skill.py` | Add `classify_target()` function. Add `--hardware` flag to pipeline command. When hardware mode is on, use `remote_exec` instead of local `run_file()`. |

### New

| File | Purpose |
|---|---|
| `ssh_skill.py` | SSH/SFTP wrapper using paramiko (Windows-compatible). `ssh_run()`, `sftp_upload()`, `sftp_download()`, `deploy_and_run()`. Loads creds from `.env`. |
| `remote_exec.py` | `RemoteTarget` class -- `upload()`, `run()`, `download()`. ~100 lines. Builds on `ssh_skill.py`. |
| `.env` | Pi SSH credentials (PI_USER, PI_HOST, PI_PASSWORD). **Gitignored, never committed.** |

That's it -- one new file of substance (`remote_exec.py`) and a config file.

## Directory Structure

```
verifybot/
├── chatgpt_skill.py          # Browser automation (existing)
├── session.py                 # Persistent browser session (existing)
├── selectors.py               # DOM selectors (existing)
├── code_skill.py              # Code extraction + pipeline (existing, extended)
├── ssh_skill.py               # [NEW] SSH/SFTP via paramiko, deploy_and_run()
├── remote_exec.py             # [TODO] RemoteTarget class, builds on ssh_skill
├── .env                       # [NEW] Pi creds (gitignored, NEVER committed)
├── raw_md/                    # Pipeline history (existing)
├── programs/                  # Extracted code (existing)
└── .browser_profile/          # Browser cookies (existing)

On the Raspberry Pi:
/home/pi/hw/
├── pi/                        # Pi-side scripts land here
├── stm32/                     # STM32 source + build artifacts
│   ├── startup.s              # Pre-staged startup assembly
│   ├── stm32l476.ld           # Pre-staged linker script
│   ├── Makefile               # Pre-staged Makefile template
│   └── cmsis/                 # Pre-staged HAL/CMSIS headers
└── logs/                      # UART captures, test logs
```

## Usage

```bash
# Existing commands still work for local-only tasks:
python code_skill.py pipeline "write a fizzbuzz in C" --verify

# NEW: Hardware pipeline
python code_skill.py pipeline "SPI to CAN bus between raspi and stm32" --hardware --verify

# NEW: Hardware pipeline with more retries
python code_skill.py pipeline "STM32 reads IMU over I2C and prints to UART" --hardware --verify --max-retries 5
```

## Build Order

### Phase 0: SSH basics (COMPLETE)
1. `ssh_skill.py` with `ssh_run()`, `sftp_upload()`, `sftp_download()`, `deploy_and_run()`
2. `.env` for credentials (gitignored)
3. Tested: `--test` creates file on Pi, `--deploy` uploads + runs script + script saves own results on Pi
4. Uses paramiko (pure Python, works on Windows)

### Phase 1: Remote execution basics
1. Write `remote_exec.py` with `upload()`, `run()`, `download()` -- wraps ssh_skill functions into RemoteTarget class
2. Test: upload a "hello world" Python script to Pi, run it, get "hello" back

### Phase 2: Target classification
4. Add `classify_target()` to `code_skill.py` — takes a `CodeBlock` + filename hint, returns `"pi"` or `"stm32"` or `"local"`
5. Test: extract blocks from a mock two-target response, verify correct classification

### Phase 3: Cross-compile + flash
6. Pre-stage STM32 support files on Pi (linker script, startup, CMSIS headers, Makefile)
7. Add compile + flash commands to the pipeline via `remote.run()`
8. Test: send a minimal STM32 C file, compile on Pi, flash, read UART "hello" back

### Phase 4: Full loop
9. Add `--hardware` flag to `cmd_pipeline()`
10. Add dual output capture (Pi stdout + STM32 UART)
11. Add `build_hardware_feedback()` for the retry loop
12. Test: run the full CAN roundtrip end-to-end

## Prerequisites

**On your laptop** (already have most of this):
```
pip install playwright
playwright install chromium
```

**On the Raspberry Pi** (do once):
```bash
sudo apt install gcc-arm-none-eabi openocd can-utils
# Set up SSH key auth from laptop
# MCP2515 overlay in /boot/firmware/config.txt
# Enable UART
# Create /home/pi/hw/stm32/ with support files
```

**Physical wiring** (do once, verify manually before automating):
- MCP2515 #1 ↔ Pi SPI0, MCP2515 #2 ↔ STM32 SPI1, CAN_H↔CAN_H, CAN_L↔CAN_L
- STM32 PA2 (UART TX) → Pi UART RX
- STM32 SWD ↔ Pi GPIOs (or ST-Link on Pi USB)
- 120Ω termination resistors on CAN bus

---

No frameworks. No agents. No LangChain. Same scripts as before, plus SSH.


### Notes
1. NO EMOJIS anywhere -- Linux terminal and STM32 UART cannot process/print Unicode emoji encodings. Use ASCII only in all generated code, output, and log files.
2. SSH credentials live in `.env` (gitignored). Never hardcode creds in scripts.
3. `ssh_skill.py` uses paramiko (pure Python) instead of sshpass -- works on Windows.
