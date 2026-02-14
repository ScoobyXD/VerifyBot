"""
selectors.py — ChatGPT DOM selectors (centralized for easy updates)

ChatGPT frequently changes its frontend. When automation breaks,
update selectors HERE only. Everything else stays the same.

Last verified: Feb 2025
"""

# --- Navigation ---
CHATGPT_URL = "https://chat.openai.com"
NEW_CHAT_URL = "https://chat.openai.com/?model=auto"

# --- Prompt Input ---
# The main textarea where you type prompts.
# ChatGPT uses a contenteditable div with id="prompt-textarea"
PROMPT_TEXTAREA = "#prompt-textarea"

# Send button — the arrow/submit button
# Fallback chain: try data-testid first, then aria-label, then structural
SEND_BUTTON_SELECTORS = [
    'button[data-testid="send-button"]',
    'button[aria-label="Send prompt"]',
    'button[aria-label="Send"]',
    # Structural fallback: button inside the prompt form area
    'form button[type="submit"]',
]

# --- Response Detection ---
# ChatGPT renders assistant messages in these containers.
# We look for the LAST assistant message after sending.
ASSISTANT_MESSAGE_SELECTORS = [
    '[data-message-author-role="assistant"]',
    'div.agent-turn',  # older layout
]

# The "stop generating" button appears while streaming
STOP_GENERATING_SELECTORS = [
    'button[aria-label="Stop generating"]',
    'button[data-testid="stop-button"]',
]

# --- Streaming Detection ---
# When ChatGPT is done streaming, the stop button disappears
# and a "regenerate" or copy button appears.
RESPONSE_COMPLETE_INDICATORS = [
    'button[aria-label="Regenerate"]',
    'button[data-testid="regenerate-button"]',
    # The copy button on the last message
    'button[aria-label="Copy"]',
]

# --- File Upload ---
FILE_INPUT = 'input[type="file"]'

# --- Model Selector (optional) ---
MODEL_SELECTOR_BUTTON = 'button[aria-haspopup="menu"]'

# --- Timeouts (seconds) ---
NAVIGATION_TIMEOUT = 30
RESPONSE_TIMEOUT = 120  # max wait for response to finish streaming
TYPING_DELAY_MS = 30    # ms between keystrokes (appear human)
POST_SEND_DELAY = 2     # seconds after clicking send before polling
