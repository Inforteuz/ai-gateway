"""Configuration. Everything comes from SF_-prefixed environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import branding


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    # PostgreSQL: the URL carries no password (safe even if logged); the password is a separate variable.
    # Example: postgresql://ai_gateway_app@127.0.0.1:5433/ai_gateway
    database_url: str = field(default_factory=lambda: os.getenv("SF_DATABASE_URL", "postgresql:///ai_gateway"))
    database_password: str | None = field(default_factory=lambda: os.getenv("SF_DATABASE_PASSWORD") or None, repr=False)
    db_pool_max: int = field(default_factory=lambda: int(os.getenv("SF_DB_POOL_MAX", "10")))
    # Dev/test only: the app role creates the schema itself. 0 in prod — the schema comes from `app.dbadmin migrate`.
    db_auto_migrate: bool = field(default_factory=lambda: _bool("SF_DB_AUTO_MIGRATE", False))
    db_app_role: str | None = field(default_factory=lambda: os.getenv("SF_DB_APP_ROLE") or None)
    upstream_url: str = field(default_factory=lambda: os.getenv("SF_UPSTREAM_URL", "http://127.0.0.1:11434").rstrip("/"))
    cookie_secure: bool = field(default_factory=lambda: _bool("SF_COOKIE_SECURE", True))
    session_days: int = field(default_factory=lambda: int(os.getenv("SF_SESSION_DAYS", "14")))
    # Concurrent requests to the local model (sized for a single GPU).
    max_concurrent: int = field(default_factory=lambda: int(os.getenv("SF_MAX_CONCURRENT", "2")))
    queue_wait_seconds: int = field(default_factory=lambda: int(os.getenv("SF_QUEUE_WAIT", "90")))
    per_key_concurrent: int = field(default_factory=lambda: int(os.getenv("SF_PER_KEY_CONCURRENT", "3")))
    upstream_read_timeout: int = field(default_factory=lambda: int(os.getenv("SF_UPSTREAM_TIMEOUT", "300")))   # non-streaming request: time to wait for the answer
    stream_idle_timeout: int = field(default_factory=lambda: int(os.getenv("SF_STREAM_IDLE", "90")))        # stream: aborted after this many seconds without data
    max_stream_seconds: int = field(default_factory=lambda: int(os.getenv("SF_MAX_STREAM", "600")))        # overall stream duration limit
    max_body_bytes: int = field(default_factory=lambda: int(os.getenv("SF_MAX_BODY", str(2 * 1024 * 1024))))
    # Higher limit for requests with image/file attachments: base64 encoding
    # grows the file ~1.37x, so the normal limit is not enough.
    # Applies only to requests with multimodal parts (plain text keeps
    # max_body_bytes) — so large bodies cannot be used for DoS.
    max_multimodal_bytes: int = field(default_factory=lambda: int(os.getenv("SF_MAX_MULTIMODAL_BODY", str(24 * 1024 * 1024))))
    static_dir: str | None = field(default_factory=lambda: os.getenv("SF_STATIC_DIR") or None)
    monitor_enabled: bool = field(default_factory=lambda: _bool("SF_MONITOR", True))
    probe_interval: int = field(default_factory=lambda: int(os.getenv("SF_PROBE_INTERVAL", "30")))
    ext_concurrent: int = field(default_factory=lambda: int(os.getenv("SF_EXT_CONCURRENT", "6")))
    cluster_concurrent: int = field(default_factory=lambda: int(os.getenv("SF_CLUSTER_CONCURRENT", "8")))
    # Google reCAPTCHA v3 (captcha). Keys live here (/etc/ai-gateway.env) or in admin
    # settings; the environment wins. SF_CAPTCHA=0 is the emergency switch, so that admins can
    # still log in if Google is down (change it and restart the service).
    captcha_enabled: bool = field(default_factory=lambda: _bool("SF_CAPTCHA", True))
    recaptcha_site_key: str = field(default_factory=lambda: os.getenv("SF_RECAPTCHA_SITE_KEY", "").strip())
    recaptcha_secret: str = field(default_factory=lambda: os.getenv("SF_RECAPTCHA_SECRET", "").strip(), repr=False)
    # Admin panel VNC: the backend itself connects to x11vnc (127.0.0.1 only) and enters the
    # VNC password ITSELF — the password is never sent to the browser. Empty password = VNC off.
    vnc_host: str = field(default_factory=lambda: os.getenv("SF_VNC_HOST", "127.0.0.1"))
    vnc_port: int = field(default_factory=lambda: int(os.getenv("SF_VNC_PORT", "5900")))
    vnc_password: str = field(default_factory=lambda: os.getenv("SF_VNC_PASSWORD", ""), repr=False)


# Defaults for the `settings` table in the database (changed from the admin panel).
DEFAULT_SETTINGS: dict[str, str] = {
    "registration_open": "0",       # 1 = anyone can sign up
    "signup_bonus_micro": "0",      # signup bonus for new users (micro-UZS)
    "default_rpm": "30",            # requests per minute per key
    # Daily token allowance (Tashkent time) on free models for users without a deposit (0 = unlimited).
    # Comma-separated models; their usage counts against one shared limit.
    "free_daily_token_limit": "250000",
    "free_daily_limit_models": "",
    # --- Abuse limits (changed from the admin panel) ---
    "max_accounts_per_ip": "4",      # accounts per IP (0 = unlimited)
    "bonus_once_per_ip": "1",        # signup bonus once per IP
    "ip_rpm": "90",                  # requests per minute per IP
    "key_max_ips_per_hour": "6",     # a key used from this many distinct IPs counts as shared
    "shared_key_rpm": "12",          # reduced rate for a shared key
    # Temporarily unreliable models: they stay listed but are shown to users
    # as "under maintenance". Comma-separated IDs.
    "maintenance_models": "",
    # Fallback model inside the provider (currently Qwen): when the primary model says "high demand",
    # the request runs on the fallback and the response carries gateway.fallback_model. "1" = enabled.
    "cluster_fallback_enabled": "0",
    # Fast path: trivial messages to web-chat models are answered by the fast GPU model (app/fastpath.py)
    "fastpath_enabled": "1",
    "fastpath_model": "qwen3:14b",
    # When legacy model names (model-1.0 … 4.0) are retired (YYYY-MM-DD). Empty = no date announced:
    # responses carry only X-SF-Replacement, no Deprecation/Sunset (app/directions.py).
    "legacy_models_sunset": "",
    # The public status page counts from this date (YYYY-MM-DD, Tashkent): outages during the
    # pre-launch build/test period are not part of the service history.
    "status_public_since": "2026-10-20",
    "default_max_tokens": "4096",   # when max_tokens is not given
    "max_max_tokens": "16384",      # upper bound for max_tokens
    "min_topup_micro": "10000000000",  # 10 000 UZS
    "default_reasoning_effort": "low",   # when neither the request nor the key sets it: none|low|medium|high; "" = the model decides
    "external_base_url": "",  # external provider (OpenAI-compatible)
    "external_api_key": "",         # external provider key (write-only, never returned)
    "refiner_model": "",  # verifier model (name at the external provider)
    "refine_timeout": "15",         # max verification time (seconds); the draft is returned after that
    "free_mode": "0",               # 1 = free mode: nothing is charged, balance/pricing sections are hidden
    "payment_info": "",             # payment instructions shown to users (text)
    "alert_telegram_token": "",     # Telegram bot token (write-only, never returned)
    "alert_telegram_chat_id": "",   # chat that receives alerts
    "last_daily_report": "",        # date of the last daily report (YYYY-MM-DD)
    "cluster_base_url": "http://127.0.0.1:8800/v1",  # AI cluster gateway (browser-based providers)
    "cluster_api_key": "",          # Bearer token for the cluster (write-only, never returned)
    # --- Google and Telegram login ---
    # Public site URL: the redirect_uri sent to Google is built from it, so it
    # must match the URL in the Google console exactly. When empty it is taken
    # from the request itself (unreliable behind nginx, so setting it
    # explicitly is recommended).
    "site_base_url": os.getenv("SF_API_URL", "").rstrip("/").removesuffix("/v1"),
    "google_client_id": "",         # empty = no Google button
    "google_client_secret": "",     # write-only, never returned
    # The Telegram button is off ON PURPOSE: for the widget to work the domain MUST be
    # linked to the bot via BotFather's `/setdomain`, which code cannot verify.
    # If it is not linked, the button shows "Bot domain invalid" instead —
    # so the admin enables it manually.
    "telegram_login_enabled": "0",
    "telegram_login_bot": "",       # bot username; resolved via getMe when empty
    # A SEPARATE bot token for login. It need not be the alert bot, and better
    # is not: the Telegram login dialog shows the bot's NAME to the user, and a
    # personal ops bot looks odd for a product. Falls back to the alert token when empty.
    "telegram_login_token": "",     # write-only, never returned
    # --- Captcha (Google reCAPTCHA v3): login, signup, password reset ---
    # Works only when both keys are set (SF_RECAPTCHA_* in the environment wins). No keys = no captcha.
    "captcha_enabled": "1",         # 0 = off even when keys are set
    "recaptcha_site_key": "",       # public key (sent to the browser)
    "recaptcha_secret": "",         # write-only, never returned
    "captcha_min_score": "0.5",     # reject when Google's score is below this (0.0 bot … 1.0 human)
    # Accept tokens only from these domains (comma-separated; empty = not checked)
    "captcha_hostnames": branding.hostnames(),
    # Captcha on login: 0 = always; N = after N failed attempts per IP or email within
    # 15 minutes. Signup and password reset always require it.
    "captcha_login_after_fails": "0",
    # --- Referral program (referral.py) ---
    # The referrer gets this percentage of every deposit by an invited user (decimals allowed: "2.5").
    "referral_enabled": "1",        # 0 = program paused: no new referral links and no rewards
    "referral_percent": "5",
    # --- Top-up requests in Telegram (topup_telegram.py) ---
    # Messages go through the login bot (telegram_login_token); button presses arrive via webhook.
    "topup_tg_enabled": "0",
    "topup_tg_chat_id": "",          # empty = alert_telegram_chat_id
    "topup_tg_admin_ids": "",        # Telegram user_ids allowed to press the buttons (comma-separated); empty = the private chat owner
    "topup_tg_webhook_secret": "",   # X-Telegram-Bot-Api-Secret-Token; generated when the webhook is set, never returned
}
