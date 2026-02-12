# ChatGPT Browser Skill — Proof of Concept

## Purpose
Send prompts to ChatGPT via browser automation (no API), read responses, and save outputs locally.

## Setup

```bash
# 1. Install dependencies
pip install playwright
playwright install chromium

# 2. First run — log in manually
python chatgpt_skill.py --login
# This opens a browser. Log into ChatGPT manually.
# Close the browser when done. Cookies are saved.

# 3. Automated run
python chatgpt_skill.py --prompt "Write a Python hello world"

# 4. Interactive mode (send multiple prompts)
python chatgpt_skill.py --interactive
```

## How It Works
1. Launches Chromium with a **persistent profile** (saved cookies = no re-login)
2. Navigates to `chat.openai.com`
3. Locates the prompt textarea
4. Types the prompt, sends it
5. Waits for response to finish streaming
6. Extracts the response text
7. Saves to `outputs/` as timestamped markdown files

## Known Limitations
- ChatGPT DOM selectors change frequently — may need updates
- Cloudflare challenges can block headless mode (use `--headed`)
- Long responses need longer timeouts
- File upload/download skills are separate (see roadmap)

## Files
- `chatgpt_skill.py` — main automation script
- `selectors.py` — centralized DOM selectors (easy to update when ChatGPT changes)
- `outputs/` — saved responses
