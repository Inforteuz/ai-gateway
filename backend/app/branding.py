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
