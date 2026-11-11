"""Platform name and URLs. Set at install time via .env, never hardcoded."""
from __future__ import annotations

import os

BRAND_NAME = os.getenv("SF_BRAND_NAME", "AI Gateway").strip() or "AI Gateway"
# How the assistant introduces itself ("I am <ASSISTANT_NAME> ...")
ASSISTANT_NAME = os.getenv("SF_ASSISTANT_NAME", "").strip() or BRAND_NAME
SITE_URL = os.getenv("SF_SITE_URL", "").strip().rstrip("/")
API_URL = os.getenv("SF_API_URL", "").strip().rstrip("/")
# UI language used until the visitor picks one: uz | ru | en
DEFAULT_LANG = (os.getenv("SF_DEFAULT_LANG", "uz").strip().lower() or "uz")[:5]


def site_info() -> dict:
    return {"brand_name": BRAND_NAME, "assistant_name": ASSISTANT_NAME,
            "site_url": SITE_URL, "api_url": API_URL, "default_lang": DEFAULT_LANG}


def hostnames() -> str:
    """Hosts allowed for reCAPTCHA, derived from the site and API URLs."""
    from urllib.parse import urlparse
    hosts = [urlparse(u).hostname for u in (SITE_URL, API_URL) if u]
    return ",".join(dict.fromkeys(h for h in hosts if h))


# Local GPU models (Qwen) and the key pool (Gemini) introduce themselves by their real
# names unless told otherwise: "Mening ismim Qwen" in the playground, 2026-11-12. The
# browser cluster has its own persona; these two get this one line in front of the
# client's own system prompt. It only governs identity questions.
IDENTITY_NOTE = (
    f"You are {ASSISTANT_NAME}, the AI assistant of the {BRAND_NAME} platform. If asked who you are, "
    f"what model you are or who made you, say only that you are {ASSISTANT_NAME} from {BRAND_NAME}; "
    "never name the underlying model, its developer or other AI companies. Answer in the user's language."
)


def with_identity(messages: list) -> list:
    """Messages with IDENTITY_NOTE merged into the first system message (or added as one)."""
    if not isinstance(messages, list):
        return messages
    first = messages[0] if messages and isinstance(messages[0], dict) else None
    if first and first.get("role") in ("system", "developer"):
        content = first.get("content")
        if isinstance(content, str):
            return [{**first, "content": f"{IDENTITY_NOTE}\n\n{content}"}, *messages[1:]]
        if isinstance(content, list):
            return [{**first, "content": [{"type": "text", "text": IDENTITY_NOTE}, *content]}, *messages[1:]]
    return [{"role": "system", "content": IDENTITY_NOTE}, *messages]
