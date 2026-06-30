"""
intent_router.py — Fast keyword-based pre-check before Claude Agent.

No LLM call. Returns True only when the message is a pure greeting/menu
trigger with NO actionable intent embedded in it.
"""

GREETING_PATTERNS = [
    "hi", "hello", "hey", "vanakkam", "வணக்கம்", "menu",
    "start", "good morning", "good evening", "good afternoon",
    "hai", "helo", "main menu", "back", "home",
]

INTENT_KEYWORDS = [
    "appointment", "book", "cancel", "queue", "doctor", "token",
    "slot", "visit", "question", "ask", "timing", "hour", "open",
    "naalaikku", "indru", "tomorrow", "today", "appointment",
    "schedule", "status", "wait", "confirm", "change",
]


def is_greeting_or_menu_trigger(text: str) -> bool:
    """Fast keyword check — no LLM call.
    Returns True only if message has NO actionable intent.
    Numeric single-digit replies are also menu triggers (1-6).
    """
    cleaned = text.strip().lower()

    # Empty message
    if not cleaned:
        return True

    # Single digit (menu number selection)
    if cleaned in {"1", "2", "3", "4", "5", "6"}:
        return True

    # Exact match against greeting list
    if cleaned in GREETING_PATTERNS:
        return True

    # Short messages (≤3 words) that start with a greeting but have no intent
    words = cleaned.split()
    if len(words) <= 3 and any(g in cleaned for g in GREETING_PATTERNS):
        if not any(k in cleaned for k in INTENT_KEYWORDS):
            return True

    return False


def has_actionable_intent(text: str) -> bool:
    """Returns True if message likely needs Claude Agent reasoning."""
    return not is_greeting_or_menu_trigger(text)
