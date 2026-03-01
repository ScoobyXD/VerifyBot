"""
selectors.py -- ChatGPT DOM selectors (centralized for easy updates)

ChatGPT frequently changes its frontend. When automation breaks,
update selectors HERE only. Everything else stays the same.

Last verified: Feb 2026
"""

# --- Navigation ---
CHATGPT_URL = "https://chat.openai.com"
NEW_CHAT_URL = "https://chat.openai.com/?model=auto"

# --- Prompt Input ---
PROMPT_TEXTAREA = "#prompt-textarea"

# --- Send Button (fallback chain) ---
SEND_BUTTON_SELECTORS = [
    'button[data-testid="send-button"]',
    'button[aria-label="Send prompt"]',
    'button[aria-label="Send"]',
    'form button[type="submit"]',
]

# --- Response Detection ---
ASSISTANT_MESSAGE_SELECTORS = [
    '[data-message-author-role="assistant"]',
    'div.agent-turn',
]

STOP_GENERATING_SELECTORS = [
    'button[aria-label="Stop generating"]',
    'button[data-testid="stop-button"]',
]

RESPONSE_COMPLETE_INDICATORS = [
    'button[aria-label="Regenerate"]',
    'button[data-testid="regenerate-button"]',
    'button[aria-label="Copy"]',
]

# --- Timeouts ---
NAVIGATION_TIMEOUT = 30      # seconds to wait for page load
RESPONSE_TIMEOUT = 180       # max wait for response streaming
TYPING_DELAY_MS = 30         # ms between keystrokes
POST_SEND_DELAY = 2          # seconds after send before polling