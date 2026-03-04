"""
selectors.py -- ChatGPT DOM selectors (centralized for easy updates)

ChatGPT frequently changes its frontend. When automation breaks,
update selectors HERE only. Everything else stays the same.

Last verified: Mar 2026
"""

# --- Navigation ---
CHATGPT_URL = "https://chat.openai.com"
NEW_CHAT_URL = "https://chat.openai.com/?model=auto"

# --- Model Configuration ---
# Maps logical model names to ChatGPT URL model parameters.
# The URL parameter ?model=<value> selects the model for a new chat.
# Update these when ChatGPT changes model names.
MODELS = {
    "instant":    "gpt-5.3-instant",    # Default: fast, cheap
    "thinking":   "gpt-5.2-thinking",   # Escalation: slower, smarter
    "auto":       "auto",               # Let ChatGPT decide
}

DEFAULT_MODEL = "instant"
ESCALATION_MODEL = "thinking"

# How to build a new-chat URL for a specific model
def model_url(model_key: str) -> str:
    """Return the new-chat URL for the given model key."""
    param = MODELS.get(model_key, MODELS[DEFAULT_MODEL])
    return f"{CHATGPT_URL}/?model={param}"

# --- Model Picker DOM Selectors ---
# Used as a fallback if URL-based model selection doesn't work.
# ChatGPT's model picker is a dropdown button at the top of the page.
MODEL_PICKER_BUTTON_SELECTORS = [
    'button[data-testid="model-selector"]',
    'button:has-text("Instant")',
    'button:has-text("Thinking")',
    'button:has-text("GPT")',
]

# After clicking the picker, menu items appear. These selectors find them.
MODEL_MENU_ITEM_SELECTORS = {
    "instant": [
        'div[role="menuitem"]:has-text("Instant 5.3")',
        'div[role="menuitem"]:has-text("Instant")',
        'div[role="option"]:has-text("Instant 5.3")',
        'div[role="option"]:has-text("Instant")',
    ],
    "thinking": [
        'div[role="menuitem"]:has-text("Thinking 5.2")',
        'div[role="menuitem"]:has-text("Thinking")',
        'div[role="option"]:has-text("Thinking 5.2")',
        'div[role="option"]:has-text("Thinking")',
    ],
}

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

# --- File Upload ---
# ChatGPT uses a hidden <input type="file"> that we can set directly
# via Playwright's set_input_files(). These selectors find it.
FILE_INPUT_SELECTORS = [
    'input[type="file"]',
    'input[data-testid="file-upload"]',
]

# After uploading, ChatGPT shows a preview/chip. Wait for this before sending.
FILE_UPLOAD_COMPLETE_SELECTORS = [
    '[data-testid="file-thumbnail"]',
    '.text-token-text-secondary',           # file name chip
    'button[aria-label*="Remove"]',          # remove-file button = upload done
    'img[alt="Uploaded image"]',             # image preview
]

# How long to wait for upload processing (large files, images)
FILE_UPLOAD_TIMEOUT = 30     # seconds