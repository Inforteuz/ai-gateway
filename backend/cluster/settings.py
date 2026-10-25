"""Cluster settings. All values come from SF_CLUSTER_* environment variables.

When integrated with the gateway backend, the connection is configured through the
`cluster_base_url` and `cluster_api_key` keys of the backend's `settings` table.
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
# Proxy list file. The code directory is read-only (the service runs as a
# different user), so it lives in the writable state directory; otherwise the
# list would not persist and would be re-downloaded on every restart.
PROXIES_FILE = Path(os.getenv("SF_CLUSTER_PROXIES_FILE",
                              "/var/lib/ai-gateway/cluster/proxies.txt"))
PROFILES_DIR = BASE_DIR.parent / "data" / "browser_profiles"

# HTTP gateway
HOST = os.getenv("SF_CLUSTER_HOST", "127.0.0.1")
PORT = int(os.getenv("SF_CLUSTER_PORT", "8800"))

# Bearer token (empty disables auth; local dev only)
API_KEY = os.getenv("SF_CLUSTER_API_KEY", "")

# CDP ports (running Chromium instances)
CDP_PORTS = [int(p) for p in os.getenv("SF_CLUSTER_CDP_PORTS", "9222,9223,9224,9225").split(",") if p.strip()]

# Logging
LOG_LEVEL = os.getenv("SF_CLUSTER_LOG_LEVEL", "INFO").upper()
LOG_JSON = os.getenv("SF_CLUSTER_LOG_JSON", "0").strip().lower() in {"1", "true", "yes"}

# Per-request timeout (seconds)
REQUEST_TIMEOUT = int(os.getenv("SF_CLUSTER_REQUEST_TIMEOUT", "60"))

# A tab that shows neither answer text nor its stop button this long after the
# prompt went out has not started, and will not: the message was not accepted, a
# dialog is in the way, the page hung. Measured 2026-10-25: such tabs held a request
# for the full REQUEST_TIMEOUT (one Gemini tab 65 s, two Qwen tabs 60 s each)
# while another tab of the same model answered in 7 s. Past this point the
# balancer moves on to the next tab. A model that is thinking shows its stop
# button, so long reasoning is not cut. 0 disables the check.
ANSWER_START_TIMEOUT = float(os.getenv("SF_CLUSTER_ANSWER_START_TIMEOUT", "30"))

# An answer that is still GROWING is not cut at REQUEST_TIMEOUT: each new piece of
# text moves the tab's deadline ANSWER_GROWTH_GRACE seconds on, up to
# ANSWER_MAX_SECONDS in all. Measured 2026-10-25: qwen writing a whole HTML file
# for a coding agent needed over 60 s; the tab was killed mid-answer, the second
# tab started from zero and was killed too, and the request failed after 126 s
# with the model working the whole time. A tab that shows nothing is still given
# up early (ANSWER_START_TIMEOUT); one whose text stops changing, at the old limit.
ANSWER_GROWTH_GRACE = float(os.getenv("SF_CLUSTER_ANSWER_GROWTH_GRACE", "30"))
ANSWER_MAX_SECONDS = float(os.getenv("SF_CLUSTER_ANSWER_MAX_SECONDS", "170"))

# Context and history
# Context window. Web-chat providers accept long text, but the whole
# conversation goes into ONE text field, so the limit is set on our side. It
# must be generous: coding agents carry several files and tool results in the
# context.
MAX_HISTORY_TURNS = int(os.getenv("SF_CLUSTER_MAX_HISTORY", "24"))
MAX_TURN_CHARS = int(os.getenv("SF_CLUSTER_MAX_TURN_CHARS", "12000"))

# Upper limit for the whole prompt (characters). Beyond it the OLDEST turns are
# dropped; the last question and recent context are always kept.
# 320,000 characters ~ 128,000 tokens.
MAX_PROMPT_CHARS = int(os.getenv("SF_CLUSTER_MAX_PROMPT_CHARS", "320000"))

# Minimum size of the current question (last message) that is never truncated,
# so the user's actual request stays intact even when the context is full.
MIN_CURRENT_CHARS = int(os.getenv("SF_CLUSTER_MIN_CURRENT_CHARS", "48000"))

# Queue: when the only instance is busy, a request waits this many seconds.
# If more than the limit are already waiting, a new one gets 503 queue_full at once.
QUEUE_WAIT_SECONDS = int(os.getenv("SF_CLUSTER_QUEUE_WAIT", "45"))
QUEUE_MAX_WAITING = int(os.getenv("SF_CLUSTER_QUEUE_MAX", "4"))

# Overall deadline of one request (queue + recovery + retries).
# MUST be lower than the gateway's non-streaming wait limit (SF_UPSTREAM_TIMEOUT,
# 300s); otherwise the gateway cuts first and the client gets a generic 504
# instead of the actual reason.
REQUEST_DEADLINE = int(os.getenv("SF_CLUSTER_REQUEST_DEADLINE", "200"))

# For streaming requests, wait this long before sending HTTP headers.
# Errors detected within this window (queue full, unsupported, no model) return
# the proper HTTP status; later ones become an SSE `error` event.
STREAM_PREFLIGHT_SECONDS = float(os.getenv("SF_CLUSTER_STREAM_PREFLIGHT", "12"))
TOOL_PREFLIGHT_SECONDS = max(0.0, float(os.getenv("SF_CLUSTER_TOOL_PREFLIGHT", "1")))

# How long to hold a request while an instance is recovering (seconds).
# If it recovers in time the request is resent, otherwise it fails.
HEAL_WAIT_SECONDS = int(os.getenv("SF_CLUSTER_HEAL_WAIT_SECONDS", "150"))

# Interval for auto-recovering a frozen Chrome instance (seconds).
# 0 = disabled. Frequent checks are cheap: a single HTTP request.
SELF_HEAL_INTERVAL = int(os.getenv("SF_CLUSTER_SELF_HEAL_INTERVAL", "45"))

# Proxy health check interval (seconds). 0 = disabled.
# When a proxy plan expires, proxies stop working all at once; the watchdog
# swaps them for working ones from the list without stopping Chrome.
PROXY_HEALTH_INTERVAL = int(os.getenv("SF_CLUSTER_PROXY_HEALTH_INTERVAL", "300"))

# Rescan when the last scan is older than this
SCAN_REFRESH_INTERVAL = int(os.getenv("SF_CLUSTER_SCAN_INTERVAL", "30"))

# DeepSeek direct browserless API token (.env: DEEPSEEK_USER_TOKEN)
DEEPSEEK_USER_TOKEN = os.getenv("DEEPSEEK_USER_TOKEN", "").strip()
# Automatically clean up temporary chat sessions after request
DEEPSEEK_AUTO_CLEANUP = os.getenv("SF_DEEPSEEK_AUTO_CLEANUP", "1").strip().lower() in ("1", "true", "yes")

# DeepSeek model aliases
DEEPSEEK_MODEL_ALIASES = {
    "deepseek",
    "deepseek-chat",
    "deepseek-v3",
    "deepseek-reasoner",
    "deepseek-r1",
}

# Per-model cooldown times (seconds)
MODEL_COOLDOWNS = {
    "deepseek": 30,
    "qwen": 30,
    "chatgpt": 45,
    "genspark": 30,
    "deepai": 15,
    "zai": 45,
    "grok": 60,
    "chatbotapp": 30,
    "chatbotchatapp": 15,
}

# Name the model introduces itself with (.env: SF_ASSISTANT_NAME or SF_BRAND_NAME)
ASSISTANT_NAME = (os.getenv("SF_ASSISTANT_NAME", "").strip()
                  or os.getenv("SF_BRAND_NAME", "").strip() or "AI Gateway")

# Priority order for `auto` requests
DEFAULT_PRIORITY_ORDER = [
    "deepseek", "chatgpt", "qwen",
    # Free, login-less providers: tried last for `auto`
    "deepai", "venice", "chatbotchatapp", "chatbotapp",
    "genspark", "zai", "grok",
    # Gemini: still being evaluated, hidden in the catalog
    "gemini",
]

# --- Keywords that classify a response as "not a real answer" -----------------
# Three categories, each with a different cooldown length.

# 1) Temporary rate limit / anti-bot wall: short cooldown (default x2)
RATE_LIMIT_KEYWORDS = [
    "высокая нагрузка", "сильно загружен",
    "too many requests", "rate limit", "rate-limit",
    "please wait", "try again later", "slow down",
    "connect im", "drag the slider",
    "security verification", "puzzle", "captcha",
    "unusual activity", "verify you are human",
]

# 2) Daily/monthly quota exhausted: LONG cooldown (QUOTA_COOLDOWN).
# IMPORTANT: such text is NEVER returned as an answer; otherwise an error
# message would be sold as an "answer" and billed.
QUOTA_KEYWORDS = [
    "used all your free",
    "free chats for today",
    "daily limit", "daily quota",
    "upgrade to a paid plan", "upgrade your plan",
    "you've reached your limit", "you have reached your limit",
    "limit reached", "quota exceeded", "out of credits",
    "no credits remaining", "insufficient credits",
    "достигнут лимит", "лимит исчерпан",
    # Qwen: the limit notice is not a chat reply, it appears separately on the page
    "reached today's chat limit", "today's chat limit", "upgrade your membership",
]

# Quota phrases searched in the PAGE text after an empty answer: only exact provider
# banners (the page also contains the user's question, so generic words are excluded).
QUOTA_PAGE_KEYWORDS = [
    "reached today's chat limit", "today's chat limit", "upgrade your membership",
    "used all your free", "free chats for today",
    "you've reached your limit", "you have reached your limit",
]

# 2b) Transient provider-side failure (server error, disconnect).
# Not a block: it usually clears up within seconds, so it gets the shortest
# cooldown and the worker returns to service quickly.
PROVIDER_ERROR_KEYWORDS = [
    "there was an issue connecting",
    # Grok answers like this on a free account under load
    "under heavy usage", "high demand",
    "something went wrong",
    "an error occurred while processing",
    "network error", "service unavailable",
    "internal server error", "please try again",
    "произошла ошибка", "сервис недоступен",
]

# 3) Session expired / login required: operator intervention needed
LOGIN_KEYWORDS = [
    "sign in to continue", "please sign in", "please log in",
    "log in to continue", "session expired", "you are logged out",
    "create an account to", "sign up to continue",
    "войдите в аккаунт", "сессия истекла",
]

# Worker cooldown after quota exhaustion (default 1 hour).
QUOTA_COOLDOWN = int(os.getenv("SF_CLUSTER_QUOTA_COOLDOWN", "3600"))
# The provider answered "too many requests" / rate limited: this worker rests for this long
# (other healthy workers take the traffic meanwhile). Keeps the load on the provider low.
RATE_LIMIT_COOLDOWN = int(os.getenv("SF_CLUSTER_RATE_LIMIT_COOLDOWN", "900"))

# When login is required (operator must sign in manually): default 10 minutes
LOGIN_COOLDOWN = int(os.getenv("SF_CLUSTER_LOGIN_COOLDOWN", "600"))

# Short cooldown on a transient provider failure: default 20 seconds
PROVIDER_ERROR_COOLDOWN = int(os.getenv("SF_CLUSTER_PROVIDER_ERROR_COOLDOWN", "20"))

# Lost connection to the browser (tab reloaded/crashed): not the provider's
# fault but an infrastructure failure. Very short cooldown, then retry at once.
TRANSPORT_ERROR_COOLDOWN = int(os.getenv("SF_CLUSTER_TRANSPORT_ERROR_COOLDOWN", "5"))

# When all matching workers are cooling down, the balancer waits up to this
# many seconds (instead of returning 503). Default 25 seconds.
MAX_COOLDOWN_WAIT = float(os.getenv("SF_CLUSTER_MAX_COOLDOWN_WAIT", "25"))

# How often the session probe (watchdog) runs (seconds)
SESSION_PROBE_INTERVAL = int(os.getenv("SF_CLUSTER_SESSION_PROBE", "600"))

# Approximate tokenization (not real, but matches the backend's CHARS_PER_TOKEN_EST=2.5)
CHARS_PER_TOKEN = 2.5

# --- proxy pool ---------------------------------------------------------------
# Download the proxy list from a URL (e.g. a webshare.io direct download link)
PROXY_LIST_URL = os.getenv("SF_CLUSTER_PROXY_URL", "")

# Periodic refresh (seconds); 0 = off
PROXY_REFRESH_INTERVAL = int(os.getenv("SF_CLUSTER_PROXY_REFRESH", "3600"))

# How many Chromium instances run without a proxy (with the host's own IP).
# The remaining instances use the proxy list.
PROXY_DIRECT_SLOTS = int(os.getenv("SF_CLUSTER_PROXY_DIRECT_SLOTS", "1"))

# --- Attachments (image / file upload) ----------------------------------------

# Temporary file directory. Empty means the system /tmp.
# IMPORTANT: Chrome must be able to see this directory (same host).
ATTACHMENT_DIR = os.getenv("SF_CLUSTER_ATTACHMENT_DIR", "") or None

# Maximum size of a single file (default 12 MB)
MAX_ATTACHMENT_BYTES = int(os.getenv("SF_CLUSTER_MAX_ATTACHMENT_BYTES", str(12 * 1024 * 1024)))

# Total limit for all files in one request (default 30 MB)
MAX_ATTACHMENTS_TOTAL_BYTES = int(
    os.getenv("SF_CLUSTER_MAX_ATTACHMENTS_TOTAL", str(30 * 1024 * 1024)))

# Maximum number of files per request
MAX_ATTACHMENTS = int(os.getenv("SF_CLUSTER_MAX_ATTACHMENTS", "6"))

# A request longer than the provider's input field: send instructions + history as an
# attached context.txt instead of cutting them (providers that accept files). "0" = always cut.
OVERFLOW_TO_FILE = os.getenv("SF_CLUSTER_OVERFLOW_FILE", "1") not in ("0", "false", "no")

# Timeout for downloading a file from an external URL
ATTACHMENT_FETCH_TIMEOUT = float(os.getenv("SF_CLUSTER_ATTACHMENT_TIMEOUT", "20"))

# Allowed MIME types. An empty set allows every type (not recommended).
ALLOWED_ATTACHMENT_MIMES = {
    m.strip().lower()
    for m in os.getenv(
        "SF_CLUSTER_ALLOWED_MIMES",
        "image/png,image/jpeg,image/jpg,image/webp,image/gif,image/bmp,"
        "application/pdf,text/plain,text/csv,text/markdown,application/json,"
        "application/msword,"
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document,"
        "application/vnd.ms-excel,"
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    ).split(",")
    if m.strip()
}

# Requests with attachments wait longer for the answer, since the model has to
# read the file (added on top of REQUEST_TIMEOUT).
ATTACHMENT_EXTRA_TIMEOUT = int(os.getenv("SF_CLUSTER_ATTACHMENT_EXTRA_TIMEOUT", "40"))

# --- Chat history cleanup -----------------------------------------------------

# Whether chats are deleted automatically. If the provider keeps chats, the
# list grows without bound and stored conversations feed the provider's
# "memory", which raises the risk of mixing users' contexts.
CHAT_CLEANUP_ENABLED = os.getenv("SF_CLUSTER_CHAT_CLEANUP", "1") not in ("0", "false", "no")

# How often cleanup runs (seconds). Default 30 minutes.
CHAT_CLEANUP_INTERVAL = int(os.getenv("SF_CLUSTER_CHAT_CLEANUP_INTERVAL", "1800"))

# At most this many chats are deleted per cleanup run, to avoid straining the
# site's UI and raising anti-bot suspicion.
CHAT_CLEANUP_BATCH = int(os.getenv("SF_CLUSTER_CHAT_CLEANUP_BATCH", "10"))

# Number of newest chats kept in the sidebar (0 deletes all).
CHAT_CLEANUP_KEEP = int(os.getenv("SF_CLUSTER_CHAT_CLEANUP_KEEP", "3"))

# --- Renderer memory guard (see memory_guard.py) ------------------------------

# Background check interval (seconds); 0 disables auto-recovery (the manual button still works).
MEM_GUARD_INTERVAL = int(os.getenv("SF_CLUSTER_MEM_GUARD_INTERVAL", "120"))

# RSS+swap limit for a site's renderer (MB). When the ChatGPT renderer grew past
# ~1 GB, swap filled up and ~44% of requests timed out.
MEM_GUARD_THRESHOLD_MB = int(os.getenv("SF_CLUSTER_MEM_GUARD_MB", "900"))

# Minimum time between auto-recoveries of one instance (seconds) and the daily cap.
MEM_GUARD_MIN_GAP = int(os.getenv("SF_CLUSTER_MEM_GUARD_MIN_GAP", "1800"))
MEM_GUARD_MAX_PER_DAY = int(os.getenv("SF_CLUSTER_MEM_GUARD_MAX_PER_DAY", "6"))

# How long to wait for the old renderer process to exit after the site tab closes (seconds).
MEM_GUARD_PID_WAIT = float(os.getenv("SF_CLUSTER_MEM_GUARD_PID_WAIT", "20"))

# Instances whose recovery restarts the Chrome unit (comma-separated). Only applies when the
# instance hosts a provider from `memory_guard.RESTART_MODELS`; default is #9 (Gemini).
# Other instances hold the owner's personal logins; do not add them here.
MEM_GUARD_RESTART_INSTANCES = frozenset(
    int(x) for x in os.getenv("SF_CLUSTER_MEM_GUARD_RESTART", "9").split(",") if x.strip().isdigit())

# State file (interval, daily count, block); survives cluster restarts. Empty = in memory only.
MEM_GUARD_STATE_FILE = os.getenv("SF_CLUSTER_MEM_GUARD_STATE",
                                 "/var/lib/ai-gateway/cluster/memory_guard.json")

# --- Image generation ---------------------------------------------------------

# Maximum images returned per request
MAX_IMAGES_PER_REQUEST = int(os.getenv("SF_CLUSTER_MAX_IMAGES", "4"))

# Image generation takes much longer than text: separate, longer timeout
IMAGE_REQUEST_TIMEOUT = int(os.getenv("SF_CLUSTER_IMAGE_TIMEOUT", "180"))


# How long to wait for text in an image response. Short, because the answer is
# mostly the image and a long wait slows the whole request down.
# Measured: once the image arrives the model is done, and any text is read at once;
# with a 12 s limit an image-only answer wasted 13 s waiting.
IMAGE_TEXT_TIMEOUT = int(os.getenv("SF_CLUSTER_IMAGE_TEXT_TIMEOUT", "3"))
# If the model finished with a text answer while an image was expected (a refusal),
# stop waiting after this many seconds. Covers the pause before generation starts.
IMAGE_IDLE_GRACE = int(os.getenv("SF_CLUSTER_IMAGE_IDLE_GRACE", "40"))
# Cost of one generated/edited image in output tokens (a medium-quality 1024x1024 image
# is ~1000 tokens at OpenAI). The base64 length is not billed.
IMAGE_OUTPUT_TOKENS = int(os.getenv("SF_CLUSTER_IMAGE_OUTPUT_TOKENS", "1000"))
# Reasoning effort used for image requests (when the client sets none): a separate
# tool draws the image anyway, so reasoning only lengthens the wait.
IMAGE_EFFORT = os.getenv("SF_CLUSTER_IMAGE_EFFORT", "none")

# --- Response cache -----------------------------------------------------------

# Whether the cache is enabled. Each request takes 8-40 seconds in a real browser,
# so serving repeats from the cache noticeably saves server load and the
# provider's daily quota.
CACHE_ENABLED = os.getenv("SF_CLUSTER_CACHE", "1") not in ("0", "false", "no")

# Entry lifetime (seconds). Default 1 hour: AI answers do not go stale quickly,
# but questions like "what is today's date" should not be kept long.
CACHE_TTL = float(os.getenv("SF_CLUSTER_CACHE_TTL", "3600"))

# Maximum number of cache entries (evicted LRU)
CACHE_MAX_ENTRIES = int(os.getenv("SF_CLUSTER_CACHE_MAX", "500"))

# Maximum size of one entry, so base64 images do not flood the cache
CACHE_MAX_ENTRY_BYTES = int(os.getenv("SF_CLUSTER_CACHE_MAX_ENTRY", str(2 * 1024 * 1024)))

# --- Account protection (anti-bot) -------------------------------------------
#
# PRINCIPLE: do not affect quality. These limits only kick in when requests come
# VERY fast; under normal load no delay is added.

# Minimum gap between requests of one worker (account), in seconds.
# Softens robot-like back-to-back bursts. If the previous request finished
# longer ago than this, there is NO wait.
MIN_REQUEST_GAP = float(os.getenv("SF_CLUSTER_MIN_REQUEST_GAP", "3.0"))

# Random extra added to the gap (0..JITTER seconds); a perfectly constant
# interval is itself a machine fingerprint.
REQUEST_JITTER = float(os.getenv("SF_CLUSTER_REQUEST_JITTER", "1.5"))

# Maximum requests per hour from one account. When reached, the worker rests
# and the balancer moves to another, so the account does not look like a
# "farm". Deliberately high so it does not get in the way of normal use.
MAX_REQUESTS_PER_HOUR = int(os.getenv("SF_CLUSTER_MAX_RPH", "150"))

# --- Structured reasoning (YAML) ----------------------------------------------

# For hard questions, ask the model for a short plan first, then the answer.
# Only the answer goes to the user (the plan stays internal).
REASONING_YAML = os.getenv("SF_CLUSTER_REASONING_YAML", "1") not in ("0", "false", "no")

# Applied to questions longer than this even without complexity hints
REASONING_MIN_CHARS = int(os.getenv("SF_CLUSTER_REASONING_MIN_CHARS", "180"))

# --- Answer polling speed -----------------------------------------------------

# How often the DOM is read. Measurements showed 81-96% of request time goes to
# this loop, so the interval was cut down from 1 second.
# Too small a value overloads the browser; 0.3-0.5 is a good range.
POLL_INTERVAL = float(os.getenv("SF_CLUSTER_POLL_INTERVAL", "0.35"))
# First look at the answer this soon after sending (live streaming shows the first words
# early). The "answer finished" decision still waits for the adapter's initial delay, so a
# placeholder shown before the provider's stop button appears is never taken as the answer.
POLL_FIRST_DELAY = float(os.getenv("SF_CLUSTER_POLL_FIRST_DELAY", "0.3"))

# Live streaming: for stream=true requests without tools / JSON format / images the text is
# sent to the client while the provider is still writing it (instead of all at the end).
# Such requests skip the structured-reasoning template (its answer part cannot be streamed).
LIVE_STREAM = os.getenv("SF_CLUSTER_LIVE_STREAM", "1") not in ("0", "false", "no")
# Nothing is sent until the text is this long and is not a service message (quota, login...)
LIVE_STREAM_HOLD_CHARS = int(os.getenv("SF_CLUSTER_LIVE_STREAM_HOLD", "80"))
# The newest characters are held back: the page may still re-render them (markdown, code labels)
LIVE_STREAM_TAIL_CHARS = int(os.getenv("SF_CLUSTER_LIVE_STREAM_TAIL", "60"))

# --- Per-port model map -------------------------------------------------------

# Normally the model is detected from the domain in the URL. But one domain can
# run in two modes: chatgpt.com opens both logged in and anonymous, with
# completely different UIs. In that case the model is set explicitly per port;
# otherwise the logged-in adapter would be applied to the anonymous tab and the
# request would fail.
#
# Format: "9225:chatgptanon,9226:other"
# NOTE: `chatgptanon` is currently OFF (empty default).
# Logged-out chatgpt.com technically answers text, but its page has no stable
# markers: class names are obfuscated, messages lack the
# `data-message-author-role` attribute, and the login prompt and cookie banner
# keep getting in the way. In measurements the answer could not be read
# reliably, so it was not shipped to production.
# To enable: SF_CLUSTER_PORT_MODELS=9225:chatgptanon
PORT_MODEL_OVERRIDE: dict[int, str] = {}
for _pair in os.getenv("SF_CLUSTER_PORT_MODELS", "").split(","):
    _pair = _pair.strip()
    if ":" not in _pair:
        continue
    _port, _name = _pair.split(":", 1)
    try:
        PORT_MODEL_OVERRIDE[int(_port.strip())] = _name.strip()
    except ValueError:
        pass

# On Qwen every request runs in a temporary chat (`?temporary-chat=true`), so
# chats are not saved to history. "0" = old mode (plain new chat + background cleanup).
QWEN_TEMPORARY_CHAT = os.getenv("SF_CLUSTER_QWEN_TEMPORARY", "1") not in ("0", "false", "no")

# Switch to a fallback model when Qwen reports "high demand". On/off is an admin
# setting (`cluster_fallback_enabled`); the gateway sends it with every request as
# `sf_allow_fallback`. Only the model names and the "sticky" period live here.
QWEN_PRIMARY_MODEL = os.getenv("SF_CLUSTER_QWEN_MODEL", "Qwen3.7-Plus")
QWEN_FALLBACK_MODEL = os.getenv("SF_CLUSTER_QWEN_FALLBACK_MODEL", "Qwen3.8-Max")

# Gemini's mode picker, the same idea. The name is the one the menu shows on its first
# line ("3.6 Flash", "3.1 Pro", "4 Argon"); the line under it is translated, the name is
# not. Empty (the default) leaves whatever the account already has selected, which is
# what an account without a paid plan needs - it is only offered "Auto" and a couple of
# models, so asking for one it does not have sends the request to another instance.
GEMINI_MODEL = os.getenv("SF_CLUSTER_GEMINI_MODEL", "").strip()
GEMINI_FALLBACK_MODEL = os.getenv("SF_CLUSTER_GEMINI_FALLBACK_MODEL", "").strip()
# After the primary model reports "high demand", requests go straight to the fallback for this many seconds
FALLBACK_STICKY_SECONDS = int(os.getenv("SF_CLUSTER_FALLBACK_STICKY", "60"))

# ChatGPT reasoning effort (a 0..2 slider on Plus). The level is stored per ACCOUNT,
# so a request without an explicit level is always reset to this default; otherwise one
# client's "high" would carry over to the next client. "" = leave the slider alone.
CHATGPT_DEFAULT_EFFORT = os.getenv("SF_CLUSTER_CHATGPT_EFFORT", "medium")

# When a provider bot check (captcha/slider) appears, the worker rests this many seconds
# and the operator is notified. The check is DELIBERATELY not solved automatically or
# "made to disappear" by reloading; that would be circumventing the provider's protection.
CHALLENGE_COOLDOWN = int(os.getenv("SF_CLUSTER_CHALLENGE_COOLDOWN", "1200"))
# When a captcha appears: pause this many seconds, then reload ONCE without cache (owner's
# decision). If the captcha persists, there is no reload loop: the worker rests and the
# operator is notified. Sliders are never solved automatically.
CHALLENGE_RELOAD_PAUSE = float(os.getenv("SF_CLUSTER_CHALLENGE_RELOAD_PAUSE", "20"))

# SPEED: when a worker is idle after an answer, a clean chat is pre-opened for the next request
# (saves Qwen ~6 s, ChatGPT ~2.5 s, DeepSeek ~1.3 s). The next request uses the page only if the
# adapter STRICTLY confirms it with `is_fresh_chat`; otherwise it opens a new chat as usual.
PREWARM_ENABLED = os.getenv("SF_CLUSTER_PREWARM", "1") not in ("0", "false", "no")
PREWARM_MAX_AGE = int(os.getenv("SF_CLUSTER_PREWARM_MAX_AGE", "600"))
