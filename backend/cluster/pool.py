"""Discover browser tabs (workers), track their state and send requests to them."""
from __future__ import annotations

import asyncio
import json
import os
import random
import shutil
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional

import websockets
import websockets.exceptions

from . import cdp, logs
from . import runmeta
from . import settings as cfg
from .adapters import get_adapter
from .adapters.base_adapter import BaseAIAdapter
from .circuit import exponential_cooldown
from .compose import OVERFLOW_FILE_NAME, fit_prompt, split_for_file

log = logs.get("pool")

# Size limit for a CDP WebSocket message.
# The `websockets` default of 1 MB is NOT ENOUGH for images: a generated image
# is several MB as base64 and the connection DROPS with `1009 (message too big)`,
# which made image generation fail with `ConnectionClosedError`.
CDP_MAX_MESSAGE = int(os.getenv("SF_CLUSTER_CDP_MAX_MESSAGE", str(48 * 1024 * 1024)))


# The hourly request count is kept per ACCOUNT, not per tab.
# Reason: one browser instance may have several tabs of the same provider open
# (e.g. if someone opens a new tab). Counting per tab would give each tab its
# own limit and weaken account protection by the number of tabs, which is
# exactly what we want to prevent.
_ACCOUNT_HOURLY: Dict[tuple, List[float]] = {}
_ACCOUNT_DISPATCH: Dict[tuple, asyncio.Lock] = {}
_ACCOUNT_BLOCKED: Dict[tuple, tuple[float, str]] = {}
# One request at a time per account, and one shared pacing clock for it.
# Several tabs of one account exist for double buffering (one answers while
# the other prepares a clean chat), never to send in parallel.
_ACCOUNT_LOCKS: Dict[tuple, asyncio.Lock] = {}
_ACCOUNT_IMAGE_SETUP: Dict[tuple, asyncio.Lock] = {}
_ACCOUNT_FINISHED: Dict[tuple, float] = {}
# Parallel tabs (cfg.PARALLEL_TAB_MODELS): which tabs of an account are answering
# right now, when the account last sent, and until when it is back to one request
# at a time after a captcha.
_ACCOUNT_INFLIGHT: Dict[tuple, set] = {}
_ACCOUNT_LAST_SEND: Dict[tuple, float] = {}
_ACCOUNT_SERIAL_UNTIL: Dict[tuple, float] = {}
_TAB_OPENED_AT: Dict[int, float] = {}


def parallel_allowed(account_key: tuple) -> bool:
    """May the tabs of this account answer at the same time?"""
    return (account_key[0] in cfg.PARALLEL_TAB_MODELS and cfg.PARALLEL_TABS > 1
            and time.time() >= _ACCOUNT_SERIAL_UNTIL.get(account_key, 0.0))


def serialize_account(account_key: tuple, why: str) -> None:
    """Back to one request at a time on this account (see PARALLEL_BACKOFF)."""
    if account_key[0] not in cfg.PARALLEL_TAB_MODELS:
        return
    _ACCOUNT_SERIAL_UNTIL[account_key] = time.time() + cfg.PARALLEL_BACKOFF
    log.warning("parallel tabs off model=%s port=%d for %ds (%s)",
                account_key[0], account_key[1], cfg.PARALLEL_BACKOFF, why)


# Response classes, to tell rate-limit/quota/login states apart
CLASS_OK = "ok"
CLASS_RATE_LIMIT = "rate_limit"
CLASS_QUOTA = "quota"
CLASS_LOGIN = "login"
CLASS_PROVIDER_ERROR = "provider_error"


SERVICE_MSG_MAX_CHARS = 400


def classify_response(text: str, prompt_text: str = "") -> tuple[str, str]:
    """Decide whether a response is a real answer or a service message.

    Returns (class, matched keyword).

    Order matters: quota and login are checked before rate-limit, because
    their texts sometimes contain the word "limit" too, yet they need a much
    longer cooldown.
    """
    # Service messages (limit, login, "high demand" banner) are SHORT and contain no code.
    # Checking answers of any length would reject code or prose that merely mentions
    # "rate limit"/"please wait"/"captcha"/"puzzle" as a provider error and put the
    # worker into cooldown (for an hour in the quota case).
    stripped = (text or "").strip()
    if len(stripped) > SERVICE_MSG_MAX_CHARS or "```" in stripped:
        return CLASS_OK, ""
    low = stripped.lower()
    # A topic the user asked about ("what is a rate limit?") naturally shows up in the answer
    asked = (prompt_text or "").lower()
    for kind, words in ((CLASS_QUOTA, cfg.QUOTA_KEYWORDS), (CLASS_LOGIN, cfg.LOGIN_KEYWORDS),
                        (CLASS_RATE_LIMIT, cfg.RATE_LIMIT_KEYWORDS),
                        (CLASS_PROVIDER_ERROR, cfg.PROVIDER_ERROR_KEYWORDS)):
        for kw in words:
            if kw in low and kw not in asked:
                return kind, kw
    return CLASS_OK, ""


def is_degenerate_response(text: str) -> bool:
    """Detect a non-answer (CAPTCHA, empty, punctuation only).

    Short answers (e.g. "4", "ha", "yes") are VALID; only empty answers or
    meaningless punctuation are rejected.
    """
    stripped = (text or "").strip()
    if not stripped:
        return True
    # A single repeated symbol (e.g. "....", "____") is a placeholder. Repeated digits or
    # letters ("3333", "1111", "aaa") may be a real answer and are not rejected.
    if len(stripped) >= 3 and len(set(stripped)) == 1 and not stripped[0].isalnum():
        return True
    # Only meaningless punctuation
    if all(ch in ".,;:!?-_ \t\n…" for ch in stripped):
        return True
    return False


_URL_TO_MODEL = {
    "chatgpt.com": "chatgpt",
    "deepseek.com": "deepseek",
    "chat.qwen.ai": "qwen",
    "genspark.ai": "genspark",
    "deepai.org": "deepai",
    "chat.z.ai": "zai",
    "grok.com": "grok",
    "chatbotappai.ai": "chatbotapp",
    "chatbotchatapp.com": "chatbotchatapp",
    "gemini.google.com": "gemini",
    "venice.ai": "venice",
}


def detect_model_from_url(url: str) -> Optional[str]:
    """Detect the model from the HOST part of a URL.

    Only the hostname is checked, so OAuth redirects such as
    `?continue=https://chat.z.ai/...` in the query/path are not misdetected.
    """
    from urllib.parse import urlparse
    try:
        host = (urlparse(url or "").hostname or "").lower()
    except Exception:
        return None
    if not host:
        return None
    for domain, model in _URL_TO_MODEL.items():
        # Exact match or subdomain (e.g. "accounts.chatgpt.com")
        if host == domain or host.endswith("." + domain):
            return model
    return None


class WorkerTab:
    """Controller for a single Chromium tab. Lock + cooldown + statistics."""

    def __init__(self, browser_port: int, tab_id: str, model_name: str, ws_url: str, adapter: BaseAIAdapter):
        self.browser_port = browser_port
        self.tab_id = tab_id
        self.model_name = model_name
        self.ws_url = ws_url
        self.adapter = adapter
        self.lock = asyncio.Lock()
        self.cooldown_until = 0.0
        self.cooldown_reason = ""
        self.default_cooldown = cfg.MODEL_COOLDOWNS.get(model_name, 30)
        self.total_requests = 0
        self.failed_requests = 0
        self.last_used = 0.0
        self.last_error: Optional[str] = None
        # Consecutive failures; reset to 0 on success (for the exponential cooldown)
        self.consecutive_failures = 0
        # Outcomes of the last 20 attempts (True=OK, False=error), for the stability score
        self._recent: list[bool] = []
        # State needing operator attention: None | "quota" | "login"
        self.needs_attention: Optional[str] = None
        # Provider-side fallback model: until this time requests go to the fallback (if runmeta allows)
        self.prefer_fallback_until = 0.0
        self.warm_since = 0.0   # when a pre-opened clean chat was prepared (0 = none)
        self._last_variant: Optional[str] = None
        # Anti-bot: when the last request finished. The hourly count is kept
        # per account (`_ACCOUNT_HOURLY`), since one account may have several
        # tabs.
        self.last_finished = 0.0
        # Last session check (a probe that sends no prompt):
        # {"ts", "ok", "reason"}. The admin panel status is derived from it,
        # to tell "login required" apart from "page crashed".
        self.last_probe: Optional[Dict[str, Any]] = None

    @property
    def short_tab(self) -> str:
        return self.tab_id[:8]

    @property
    def lane_key(self) -> tuple:
        """What sends one request at a time: the account, or this one tab of it
        when the account may answer in parallel (cfg.PARALLEL_TAB_MODELS)."""
        if parallel_allowed(self.account_key):
            return (*self.account_key, self.tab_id)
        return self.account_key

    @property
    def dispatch_lock(self) -> asyncio.Lock:
        return _ACCOUNT_DISPATCH.setdefault(self.lane_key, asyncio.Lock())

    @property
    def cooldown_until(self) -> float:
        # Quota, login, hourly cap and captcha apply to every tab of an account.
        shared = _ACCOUNT_BLOCKED.get(self.account_key, (0.0, ""))[0]
        return max(getattr(self, "_cooldown_until", 0.0), shared)

    @cooldown_until.setter
    def cooldown_until(self, value: float) -> None:
        self._cooldown_until = value
        if value == 0:
            _ACCOUNT_BLOCKED.pop(self.account_key, None)

    def sibling_blocked(self) -> bool:
        """Another tab of this account is answering and this one may not join it yet.

        Only for parallel-tab models; the others are already kept to one request
        per account by the account-wide locks. Checked when a tab is picked, so the
        balancer waits for an allowed tab instead of sleeping inside one.
        """
        if self.account_key[0] not in cfg.PARALLEL_TAB_MODELS:
            return False
        others = _ACCOUNT_INFLIGHT.get(self.account_key, set()) - {self.tab_id}
        if not others:
            return False
        if not parallel_allowed(self.account_key):
            return True
        return time.time() - _ACCOUNT_LAST_SEND.get(self.account_key, 0.0) < cfg.PARALLEL_SEND_GAP

    def claim(self) -> None:
        """Mark this tab as answering. Called by the balancer in the same step that
        picked it (no await in between), so two requests cannot both pass the
        sibling check before either is marked."""
        _ACCOUNT_INFLIGHT.setdefault(self.account_key, set()).add(self.tab_id)
        _ACCOUNT_LAST_SEND[self.account_key] = time.time()

    def release_claim(self) -> None:
        _ACCOUNT_INFLIGHT.get(self.account_key, set()).discard(self.tab_id)

    def is_available(self) -> bool:
        return (time.time() >= self.cooldown_until and not self.lock.locked()
                and not self.dispatch_lock.locked() and not self.sibling_blocked())

    def state(self, now: Optional[float] = None) -> str:
        """One-word state for monitoring: ACTIVE (free and healthy), BUSY (serving a request),
        COOLDOWN (resting after an error/rate limit, or waiting for the operator: see
        needs_attention) or EXHAUSTED (provider quota used up, rests until it renews)."""
        now = time.time() if now is None else now
        if self.lock.locked():
            return "BUSY"
        if now < self.cooldown_until:
            return "EXHAUSTED" if self.needs_attention == "quota" else "COOLDOWN"
        return "ACTIVE"

    def set_cooldown(self, seconds: Optional[int] = None, reason: str = "") -> None:
        sec = seconds if seconds is not None else self.default_cooldown
        self.cooldown_until = time.time() + sec
        if self.browser_port > 0 and reason.startswith(("captcha", "probe:captcha", "quota", "login", "probe:login", "hourly_cap", "rate_limit")):
            old = _ACCOUNT_BLOCKED.get(self.account_key, (0.0, ""))[0]
            _ACCOUNT_BLOCKED[self.account_key] = (max(old, self.cooldown_until), reason)
        self.cooldown_reason = reason or "manual"
        log.warning(
            "cooldown model=%s port=%d tab=%s seconds=%d reason=%s",
            self.model_name, self.browser_port, self.short_tab, sec, reason or "manual",
        )

    def apply_probe(self, healthy: bool, reason: str, *, reloaded: bool = False) -> None:
        """Apply a session check result to the worker state.

        Both the background watchdog and the admin panel's "check session"
        button go through here, so they follow the same rules.
        """
        self.last_probe = {"ts": int(time.time()), "ok": healthy, "reason": reason}
        if healthy:
            # A DOM probe proves the page/session is available, not that a
            # timed-out generation recovered. Only an explicit successful
            # reload may clear a stale transient request error.
            if reloaded and self.last_error in ("timeout", "empty", "send_failed"):
                self.last_error = None
                self.consecutive_failures = 0
                if self.cooldown_reason.startswith(("timeout", "streak=")):
                    # Keep a quota/rate-limit block imposed by another tab.
                    self._cooldown_until = 0.0
                    self.cooldown_reason = ""
            blocked = _ACCOUNT_BLOCKED.get(self.account_key)
            if blocked and blocked[1].startswith(("captcha", "probe:captcha", "login", "probe:login")):
                _ACCOUNT_BLOCKED.pop(self.account_key, None)
            if self.needs_attention:
                # Session restored: clear both the flag and the cooldown the probe
                # set. Otherwise a healthy worker would keep showing "login
                # required" in the admin panel until the next check (minutes).
                log.info("session restored model=%s port=%d (previous state: %s)",
                         self.model_name, self.browser_port, self.needs_attention)
                self.needs_attention = None
                self.cooldown_until = 0.0
                self.last_error = None
        elif reason == "captcha":
            self.needs_attention = "captcha"
            self.set_cooldown(cfg.CHALLENGE_COOLDOWN, reason="probe:captcha")
            serialize_account(self.account_key, "probe:captcha")
            log.warning("bot check model=%s port=%d, operator needed", self.model_name, self.browser_port)
        elif reason in ("login_required", "no_chat_input"):
            self.needs_attention = "login"
            self.set_cooldown(cfg.LOGIN_COOLDOWN, reason=f"probe:{reason}")
            log.warning("session dead model=%s port=%d reason=%s",
                        self.model_name, self.browser_port, reason)

    def _record_outcome(self, ok: bool, transient: bool = False) -> None:
        """Record the outcome of an attempt.

        `transient=True` means the failure is the infrastructure's fault, not the
        provider's (browser tab reloaded, CDP connection dropped). The exponential
        cooldown is NOT applied then; otherwise one random disconnect would take
        the worker out for minutes and the model would look "gone".
        """
        self._recent.append(ok)
        if len(self._recent) > 20:
            self._recent.pop(0)
        if ok:
            self.consecutive_failures = 0
            self.last_error = None
            self.needs_attention = None
            return
        self.failed_requests += 1
        if transient:
            self.set_cooldown(cfg.TRANSPORT_ERROR_COOLDOWN, reason="transport")
            return
        self.consecutive_failures += 1
        # Exponential cooldown: rest longer after each failed attempt
        adaptive = exponential_cooldown(self.consecutive_failures)
        if adaptive > 0:
            self.set_cooldown(int(adaptive), reason=f"streak={self.consecutive_failures}")

    @property
    def health_score(self) -> float:
        """0..1: success ratio of the last 20 attempts. 1.0 for a new worker."""
        if not self._recent:
            return 1.0
        return sum(1 for x in self._recent if x) / len(self._recent)

    def _lookup_ws_url(self) -> Optional[str]:
        """Re-read this tab's current WebSocket URL from CDP `/json`."""
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.browser_port}/json", timeout=2.0) as resp:
                tabs = json.loads(resp.read().decode())
        except Exception:
            return None
        for tab in tabs:
            if tab.get("id") == self.tab_id:
                return tab.get("webSocketDebuggerUrl")
        # The tab was recreated (ID changed): look for a tab of the same model
        # on this port and "move" to it.
        for tab in tabs:
            if tab.get("type") != "page":
                continue
            if detect_model_from_url(tab.get("url", "")) != self.model_name:
                continue
            ws_url = tab.get("webSocketDebuggerUrl")
            if ws_url:
                self.tab_id = tab.get("id", self.tab_id)
                return ws_url
        return None

    async def connect(self, open_timeout: float = 10.0):
        """Open a CDP connection to the tab; on failure refresh `ws_url` and retry.

        When Chrome recreates a tab (navigation, crash, manual close) the old
        `webSocketDebuggerUrl` goes dead and connecting fails with `InvalidStatus`
        or `ConnectionClosed`. A fresh URL is then read from `/json`, which
        restores operation without waiting for a full rescan.
        """
        try:
            return await websockets.connect(self.ws_url, ping_interval=None,
                                            open_timeout=open_timeout,
                                            max_size=CDP_MAX_MESSAGE)
        except Exception:
            fresh = await asyncio.get_event_loop().run_in_executor(None, self._lookup_ws_url)
            if not fresh or fresh == self.ws_url:
                raise
            log.info("ws_url refreshed model=%s port=%d", self.model_name, self.browser_port)
            self.ws_url = fresh
            return await websockets.connect(fresh, ping_interval=None,
                                            open_timeout=open_timeout,
                                            max_size=CDP_MAX_MESSAGE)

    async def _reset_phase(self, for_images: bool = False) -> None:
        """New-chat phase, on its own CDP connection.

        Why separate: some sites (ChatGPT) fully reload the page when switching
        to a new chat and recreate the CDP target. Doing reset and prompt on one
        connection would drop it right there and lose the request. With reset
        split out, the disconnect is an expected condition.
        """
        try:
            async with await self.connect() as ws:
                if for_images:
                    # Image mode menus can require a foreground tab. Serialize
                    # just setup within an account so parallel image requests
                    # cannot steal focus midway through each other's menu.
                    key = (getattr(self, "model_name", ""), getattr(self, "browser_port", 0))
                    async with _ACCOUNT_IMAGE_SETUP.setdefault(key, asyncio.Lock()):
                        await self.adapter.reset_chat_for_images(ws)
                else:
                    await self.adapter.reset_chat(ws)
        except websockets.exceptions.WebSocketException:
            # Navigation dropped the connection: normal. The tab URL gets refreshed.
            log.debug("reset: connection dropped during navigation model=%s", self.model_name)
        except Exception as e:
            # Sending after a failed reset can disclose a previous user's
            # conversation. Let dispatch select another worker instead.
            log.warning("reset_chat failed: %s", type(e).__name__)
            raise

    @property
    def account_key(self) -> tuple:
        """Key identifying the account: one provider in one browser = one account."""
        return (self.model_name, self.browser_port)

    @property
    def account_lock(self) -> asyncio.Lock:
        """Shared by every tab of this account: they never send at the same time —
        unless the account may answer in parallel, then it is per tab (lane_key)."""
        return _ACCOUNT_LOCKS.setdefault(self.lane_key, asyncio.Lock())

    def hourly_count(self) -> int:
        """Number of requests sent from this ACCOUNT in the last hour."""
        cutoff = time.time() - 3600
        hits = [t for t in _ACCOUNT_HOURLY.get(self.account_key, []) if t > cutoff]
        _ACCOUNT_HOURLY[self.account_key] = hits
        return len(hits)

    def _mark_request(self) -> None:
        _ACCOUNT_HOURLY.setdefault(self.account_key, []).append(time.time())

    async def _pace(self) -> None:
        """Natural pacing to protect the account.

        Does two things:
          1) a minimum gap between requests (with random jitter), since
             back-to-back bursts look robotic;
          2) an hourly cap, so the account is not used like a "farm".

        IMPORTANT: no wait is added under normal load. If the previous request
        finished more than MIN_REQUEST_GAP ago, this returns immediately, with
        no effect on quality or latency.
        """
        if cfg.MAX_REQUESTS_PER_HOUR > 0 and self.hourly_count() >= cfg.MAX_REQUESTS_PER_HOUR:
            # Rest until the oldest request leaves the window
            hits = _ACCOUNT_HOURLY.get(self.account_key) or [time.time()]
            oldest = min(hits)
            rest = max(60, int(oldest + 3600 - time.time()) + 1)
            self.set_cooldown(rest, reason=f"hourly_cap={cfg.MAX_REQUESTS_PER_HOUR}")
            raise RuntimeError(
                f"{self.model_name}: hourly cap ({cfg.MAX_REQUESTS_PER_HOUR}) reached, "
                f"use another worker")

        gap = cfg.MIN_REQUEST_GAP + random.random() * cfg.REQUEST_JITTER
        # The gap belongs to the ACCOUNT: with several tabs open the provider
        # would otherwise see one message per tab back to back.
        last = max(self.last_finished, _ACCOUNT_FINISHED.get(self.account_key, 0.0))
        waited = time.time() - last
        if last and waited < gap:
            await asyncio.sleep(gap - waited)

    async def prewarm(self) -> None:
        """After an answer: if the worker is idle, pre-open a clean chat for the next request.

        Skipped when a request is waiting in the queue (so as not to get ahead of it).
        The next request still verifies the page is clean with `is_fresh_chat`.
        """
        if not cfg.PREWARM_ENABLED or self.lock.locked() or getattr(self.lock, "_waiters", None):
            return
        if time.time() < self.cooldown_until or self.needs_attention:
            return
        async with self.lock:
            try:
                if await self._reuse_clean_chat():
                    self.warm_since = time.time()
                    return
                await self._reset_phase()
                async with await self.connect(open_timeout=5) as ws:
                    await self.adapter.wait_for_input(ws, timeout=15)
                    if await self.adapter.is_fresh_chat(ws):
                        self.warm_since = time.time()
            except Exception as e:
                self.warm_since = 0.0
                log.debug("prewarm skipped model=%s: %s", self.model_name, e)

    async def _reuse_clean_chat(self, for_images: bool = False) -> bool:
        """DOM state, not the age of our hint, decides whether a chat is empty.

        A ten-minute-old blank temporary chat was being navigated again on
        every low-traffic request. Never reuse a conversation or image chat;
        each adapter must affirm its strict fresh-chat contract right now.
        """
        if for_images or not cfg.PREWARM_ENABLED:
            return False
        try:
            async with await self.connect(open_timeout=5) as ws:
                return await self.adapter.is_fresh_chat(ws) is True
        except Exception:
            return False

    def _on_challenge(self, sign: str) -> None:
        """Captcha: long cooldown + operator flag (cluster_watch notifies via Telegram)."""
        self.last_error = "captcha"
        self.needs_attention = "captcha"
        self._record_outcome(False)
        self.set_cooldown(cfg.CHALLENGE_COOLDOWN, reason=f"captcha:{sign[:24]}")
        serialize_account(self.account_key, f"captcha:{sign[:24]}")
        log.warning("bot check model=%s port=%d sign=%s, %ds cooldown, operator needed",
                    self.model_name, self.browser_port, sign, cfg.CHALLENGE_COOLDOWN)

    async def _challenge_now(self) -> bool:
        """Is there a bot check on the page right now (checked after an error/no answer)?"""
        try:
            async with await self.connect(open_timeout=5) as ws:
                sign = await self.adapter.detect_challenge(ws)
        except Exception:
            return False
        if sign:
            self._on_challenge(sign)
            return True
        return False

    async def _preflight_session(self) -> None:
        """Check the session WITHOUT a prompt before sending a request to a worker that is
        unchecked since restart or flagged "operator needed".

        `needs_attention` and cooldowns live only in memory, so after a cluster restart a
        worker with a blocked account looks "healthy" again, and a request arriving before
        the next background check would hang on it (76 s, then 502). With this check a
        healthy page passes instantly; a dead session is detected in ~1-15 s and the
        request moves to another worker or fails right away.
        """
        if self.last_probe is not None and not self.needs_attention:
            return
        try:
            async with await self.connect(open_timeout=8) as ws:
                ok, reason = await self.adapter.check_session(ws, timeout=8, recover=False)
                if not ok and reason not in ("captcha", "login_required"):
                    # The page may just be loading: try one recovery
                    ok, reason = await self.adapter.check_session(ws, timeout=12, recover=True)
        except Exception as e:
            ok, reason = False, f"ws:{type(e).__name__}"
        self.apply_probe(ok, reason)
        if not ok:
            raise RuntimeError(f"{self.model_name}: session not working ({reason}), request not sent")

    async def _quota_now(self, prompt_text: str = "") -> Optional[str]:
        """Is there a daily-limit notice on the page? Checked after an empty answer.

        MEASURED (Qwen): "You've reached today's chat limit" appears as a separate block
        on the page, not as a chat reply. The adapter does not read it, so every request
        waited ~80 s and ended with "empty answer" while the quota went undetected.
        """
        js = "(() => ((document.querySelector('main') || document.body).innerText || '').slice(-1500))()"
        try:
            async with await self.connect(open_timeout=5) as ws:
                tail = await cdp.evaluate(ws, js, timeout=5)
        except Exception:
            return None
        # The page tail also contains the user's question, so instead of the short-text
        # classifier only exact provider phrases NOT present in the question are used.
        low = (tail if isinstance(tail, str) else "").lower()
        asked = (prompt_text or "").lower()
        for kw in cfg.QUOTA_PAGE_KEYWORDS:
            if kw in low and kw not in asked:
                return kw
        return None

    def _choose_variant(self, meta: dict) -> Optional[str]:
        """Provider-side model for this attempt: forced (measurement), fallback or primary."""
        if meta.get("force_variant"):
            return meta["force_variant"]
        if (meta.get("allow_fallback") and self.adapter.has_variant("fallback")
                and time.time() < self.prefer_fallback_until):
            return "fallback"
        return None

    async def _run_once(self, prompt_text: str, timeout: int,
                        attachment_paths: Optional[List[str]] = None,
                        images_only: bool = False,
                        want_images: bool = False) -> tuple[Optional[str], List[tuple[str, str]]]:
        """Open a new chat, attach files, send the prompt and read the answer.

        Returns (text, images), where images are (mime, base64) pairs.
        """
        # Phase timings (for speed measurement): logged by the server as `phases=`
        phases = runmeta.get().setdefault("phases", {})
        t_mark = [time.monotonic()]

        def mark(name: str) -> None:
            now = time.monotonic()
            phases[name] = round(phases.get(name, 0.0) + now - t_mark[0], 2)
            t_mark[0] = now

        # Phase 1: a new chat before every request, so users never get mixed.
        runmeta.get()["image_requested"] = want_images
        # A pre-opened page is used only if the adapter STRICTLY confirms it is clean.
        # Image requests do not use the pre-opened (temporary) chat, because
        # ChatGPT does not draw images in a temporary chat.
        warm = await self._reuse_clean_chat(for_images=want_images)
        runmeta.get()["chat_reused"] = warm
        self.warm_since = 0.0
        if not warm:
            await self._reset_phase(for_images=want_images)
        mark("new_chat")

        # The tab ID may have changed if the page reloaded
        fresh = await asyncio.get_event_loop().run_in_executor(None, self._lookup_ws_url)
        if fresh and fresh != self.ws_url:
            self.ws_url = fresh

        # Phase 2: prompt + answer, on a fresh, stable connection
        async with await self.connect() as ws:
            # The page may still be loading: wait for the input field
            await self.adapter.wait_for_input(ws, timeout=15)
            if not warm and not want_images:
                verify_reset = getattr(self.adapter, "verify_clean_chat", None)
                if verify_reset is not None and await verify_reset(ws) is False:
                    raise RuntimeError(f"{self.model_name}: new chat isolation check failed")
            mark("page")

            # Bot check: pause + reload ONCE without cache (owner's decision).
            # If it persists, the request is not sent and the worker rests. Sliders are never solved automatically.
            challenge = await self.adapter.detect_challenge(ws)
            if challenge:
                # Do not spend the request's latency budget reloading a bot check.
                # Quarantine this account immediately; recovery belongs to the watchdog.
                raise ChallengeDetected(challenge)

            # Provider-side model: primary or (if allowed) fallback. Checked on every
            # request so an earlier fallback choice does not stick on the account.
            meta = runmeta.get()
            variant = self._choose_variant(meta)
            self._last_variant = variant
            active = await self.adapter.select_variant(ws, variant)
            if active:
                meta["upstream_model"] = active
            # For image requests reasoning only slows the image down: when the client
            # sets no level, the fastest one (IMAGE_EFFORT) is used.
            want_effort = meta.get("effort") or (cfg.IMAGE_EFFORT if want_images else None)
            effort = await self.adapter.set_effort(ws, want_effort)
            if effort:
                meta["upstream_effort"] = effort
            # A client that omits effort still gets the adapter's default.
            # Poll deadlines must use that effective effort, not mistake it
            # for an unspecified/high request and wait the full minute.
            meta["effective_effort"] = effort or want_effort
            meta["fallback"] = variant == "fallback"

            # Clear the composer before EVERY request. If a previous request was
            # never sent, its file and TEXT stay there and would get merged
            # into another user's request. A leftover modal may cover the
            # whole page and swallow clicks, so dismiss that first.
            await self.adapter.dismiss_overlays(ws)
            await self.adapter.clear_attachments(ws)
            await self.adapter.clear_composer_text(ws)

            if attachment_paths:
                ok = await self.adapter.attach_files(ws, attachment_paths, images_only=images_only)
                if not ok:
                    # Without the file the model cannot see it and the answer would be
                    # bogus, so the request does not continue.
                    self.last_error = "attach_failed"
                    self._record_outcome(False, transient=True)
                    raise RuntimeError(f"{self.model_name}: could not attach file")

            mark("prepare")
            # Provider input limit (e.g. Gemini 32,000 chars): make sure the tail is not cut
            limit = getattr(self.adapter, "MAX_INPUT_CHARS", 0)
            if limit and len(prompt_text) > limit:
                log.info("%s: request %d chars > %d, trimming old history",
                         self.model_name, len(prompt_text), limit)
                prompt_text = fit_prompt(prompt_text, limit)
            sent = await self.adapter.send_prompt(ws, prompt_text)
            mark("send")
            if not sent:
                # After a reset the page may not have finished reloading, so the
                # input field is not ready yet. Wait and retry once
                # (measured: after a new chat ChatGPT sometimes stays
                # non-writable for up to 10 seconds).
                await asyncio.sleep(3.0)
                await self.adapter.wait_for_input(ws, timeout=15)
                sent = await self.adapter.send_prompt(ws, prompt_text)
            if not sent:
                self.last_error = "send_failed"
                self._record_outcome(False)
                raise RuntimeError(f"{self.model_name}: could not type into the input field")

            images: List[tuple[str, str]] = []

            if want_images:
                # Image requested: the answer is mostly an IMAGE with little text.
                # Waiting long for text is pointless, so watch for the image
                # first. (Measured: ChatGPT draws the image in 5-6 seconds, but
                # code waiting for text burned the whole 180 seconds and then
                # returned an empty answer.)
                urls = await self._wait_for_images(ws, timeout=timeout)
                mark("image_gen")
                # Image endpoints discard the text, so waiting for it is wasted time
                text = "" if meta.get("image_only") else await self.adapter.read_response(
                    ws, timeout=cfg.IMAGE_TEXT_TIMEOUT)
                mark("text")
                if urls:
                    images = await self.adapter.download_images(ws, urls)
                mark("download")
                await self.adapter.after_image_response(ws)
                mark("registry")
            else:
                read_started = time.monotonic()
                timed_out = None
                try:
                    text = await self.adapter.read_response(ws, timeout=timeout)
                except asyncio.TimeoutError as exc:
                    timed_out = exc
                    text = None
                # A bounded retry on the provider's existing answer. Never replay
                # after live bytes were committed or when verification is required.
                meta = runmeta.get()
                # Retry the existing response once, without retyping the whole
                # conversation. Keep it inside the original read budget.
                retry_budget = min(12, timeout - (time.monotonic() - read_started))
                technical_error = classify_response(text or "", prompt_text)[0] == CLASS_PROVIDER_ERROR
                if ((timed_out or is_degenerate_response(text) or technical_error)
                        and retry_budget >= 2 and not meta.get("stream_committed")
                        and not meta.get("regenerated")
                        and hasattr(self.adapter, "regenerate_response")):
                    if await asyncio.wait_for(self.adapter.regenerate_response(ws), timeout=retry_budget):
                        meta["regenerated"] = True
                        retry_budget = min(12, timeout - (time.monotonic() - read_started))
                        if retry_budget <= 0:
                            raise asyncio.TimeoutError("Response retry budget exhausted")
                        text = await self.adapter.read_response(ws, timeout=retry_budget)
                        timed_out = None
                if timed_out:
                    raise timed_out
                mark("answer")

            self.last_finished = time.time()
            _ACCOUNT_FINISHED[self.account_key] = self.last_finished
            return text, images

    async def _wait_for_images(self, ws, timeout: int) -> List[str]:
        """Wait for images to appear in the answer and return once they are stable.

        Stability matters: images are drawn progressively and several may
        appear one after another. When the count stays unchanged across
        consecutive polls, generation is considered done.
        """
        start = time.monotonic()
        deadline = start + timeout
        prev: List[str] = []
        stable = 0
        idle = 0
        urls: List[str] = []
        while time.monotonic() < deadline:
            await asyncio.sleep(cfg.IMAGE_POLL_INTERVAL)
            try:
                found = await self.adapter.read_images(ws)
            except Exception:
                continue
            if not found:
                # If the model finished with a text answer (a refusal), waiting is pointless
                if time.monotonic() - start >= cfg.IMAGE_IDLE_GRACE:
                    idle = idle + 1 if await self.adapter.finished_without_image(ws) else 0
                    if idle >= 3:
                        return []
                continue
            urls = found
            if found == prev:
                stable += 1
                # The list is unchanged and the model is done: return at once;
                # if completion cannot be detected, wait for three stable polls (3 s).
                done = await self.adapter.generation_done(ws)
                if ((stable >= 1 and done) or (stable >= 3
                        and not getattr(self.adapter, "IMAGE_DONE_RELIABLE", False))):
                    return urls
            else:
                stable = 0
                prev = list(found)
        # A completed-looking preview during active generation is not a final
        # edited image. Fail over rather than bill/return an unfinished result.
        if urls and getattr(self.adapter, "IMAGE_DONE_RELIABLE", False):
            if not await self.adapter.generation_done(ws):
                raise asyncio.TimeoutError("Image generation did not finish")
        return urls

    def _overflow_to_file(self, prompt_text: str, attachment_paths: Optional[List[str]],
                          images_only: bool, want_images: bool) -> tuple[str, Optional[str]]:
        """If the prompt is longer than the provider's input field and the provider takes files,
        writes instructions + history to a private temp context.txt. Returns (text to send,
        temp dir to delete afterwards or None)."""
        limit = getattr(self.adapter, "MAX_INPUT_CHARS", 0)
        if not (cfg.OVERFLOW_TO_FILE and limit and len(prompt_text) > limit) or images_only or want_images:
            return prompt_text, None
        if not getattr(getattr(self.adapter, "caps", None), "files", False):
            return prompt_text, None
        if len(attachment_paths or []) >= cfg.MAX_ATTACHMENTS:
            return prompt_text, None
        split = split_for_file(prompt_text, limit)
        if split is None:
            return prompt_text, None
        text, content = split
        tmpdir = tempfile.mkdtemp(prefix="ai-gateway-context-", dir=cfg.ATTACHMENT_DIR or None)
        os.chmod(tmpdir, 0o700)
        path = os.path.join(tmpdir, OVERFLOW_FILE_NAME)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(path, 0o600)
        log.info("%s: request %d chars > %d, instructions + history (%d chars) sent as %s",
                 self.model_name, len(prompt_text), limit, len(content), OVERFLOW_FILE_NAME)
        return text, tmpdir

    async def execute_prompt(self, prompt_text: str, timeout: int = 45,
                             attachment_paths: Optional[List[str]] = None,
                             images_only: bool = False,
                             want_images: bool = False) -> tuple[Optional[str], List[tuple[str, str]]]:
        """Execute the request. Returns (answer text, images).

        An oversized prompt is not cut when the provider takes files: its long part goes as
        an attached context.txt (see compose.split_for_file); the file is removed afterwards.
        """
        send_text, tmpdir = self._overflow_to_file(prompt_text, attachment_paths, images_only, want_images)
        paths = attachment_paths
        if tmpdir:
            paths = [*(attachment_paths or []), os.path.join(tmpdir, OVERFLOW_FILE_NAME)]
        try:
            async with self.account_lock:
                return await self._execute_prompt(prompt_text, send_text, timeout, paths,
                                                  images_only, want_images)
        finally:
            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)

    async def _execute_prompt(self, prompt_text: str, send_text: str, timeout: int,
                              attachment_paths: Optional[List[str]], images_only: bool,
                              want_images: bool) -> tuple[Optional[str], List[tuple[str, str]]]:
        """`prompt_text` is the full request (used to classify the answer), `send_text` what is typed."""
        # Keep a human-like gap between two requests on the same account and
        # honour the hourly cap. Raises when the cap is reached, and the balancer
        # then moves the request to another worker.
        phases = runmeta.get().setdefault("phases", {})
        t_phase = time.monotonic()
        await self._pace()
        phases["pace"] = round(phases.get("pace", 0) + time.monotonic() - t_phase, 3)
        t_phase = time.monotonic()
        await self._preflight_session()
        phases["session"] = round(phases.get("session", 0) + time.monotonic() - t_phase, 3)
        if self.account_key[0] in cfg.PARALLEL_TAB_MODELS:
            # The gap a parallel sibling must keep is measured from the real send
            _ACCOUNT_LAST_SEND[self.account_key] = time.time()
        self.total_requests += 1
        self.last_used = time.time()
        kw = dict(attachment_paths=attachment_paths, images_only=images_only,
                  want_images=want_images)
        try:
            raw, images = await self._run_once(send_text, timeout, **kw)
        except websockets.exceptions.WebSocketException as e:
            # The tab may have been recreated by navigation, reload or crash. While
            # the page reloads it is normal for the tab to vanish from `/json` for
            # a few seconds, so try several times.
            fresh = None
            for _ in range(6):
                await asyncio.sleep(2.0)
                fresh = await asyncio.get_event_loop().run_in_executor(None, self._lookup_ws_url)
                if fresh:
                    break
            if not fresh:
                self.last_error = f"ws:{type(e).__name__}"
                self._record_outcome(False, transient=True)
                raise RuntimeError(
                    f"{self.model_name}: lost connection to browser ({type(e).__name__})") from e
            log.info("connection lost, tab refreshed model=%s port=%d, retrying",
                     self.model_name, self.browser_port)
            self.ws_url = fresh
            try:
                raw, images = await self._run_once(send_text, timeout, **kw)
            except websockets.exceptions.WebSocketException as e2:
                self.last_error = f"ws:{type(e2).__name__}"
                self._record_outcome(False, transient=True)
                raise RuntimeError(
                    f"{self.model_name}: lost connection to browser ({type(e2).__name__})") from e2
        except ChallengeDetected as e:
            self._on_challenge(str(e))
            raise RuntimeError(f"{self.model_name}: provayder bot tekshiruvini ko'rsatdi — operator kerak") from e
        except asyncio.TimeoutError:
            self.last_error = "timeout"
            self._record_outcome(False)
            # A bot check may be why no answer came
            if await self._challenge_now():
                raise RuntimeError(f"{self.model_name}: provayder bot tekshiruvini ko'rsatdi — operator kerak")
            raise

        if not raw:
            # For image generation, empty text with images is normal
            if images:
                self._record_outcome(True)
                return "", images
            if await self._challenge_now():
                raise RuntimeError(f"{self.model_name}: provayder bot tekshiruvini ko'rsatdi — operator kerak")
            kw = await self._quota_now(prompt_text)
            if kw:
                self.last_error = CLASS_QUOTA
                self._record_outcome(False)
                self.set_cooldown(cfg.QUOTA_COOLDOWN, reason=f"quota:{kw[:24]}")
                self.needs_attention = "quota"
                raise RuntimeError(
                    f"{self.model_name}: daily quota exhausted, retrying in {cfg.QUOTA_COOLDOWN // 60} minutes")
            self.last_error = "empty"
            self._record_outcome(False)
            return None, []

        # 1. Is this a real answer or a service message (quota/login/rate-limit)?
        kind, kw = classify_response(raw, prompt_text)
        meta = runmeta.get()
        if (kind == CLASS_RATE_LIMIT and "high demand" in raw.lower()
                and meta.get("allow_fallback") and not meta.get("force_variant")
                and self._last_variant is None and not meta.get("_fallback_tried")
                and self.adapter.has_variant("fallback")):
            # The primary model is busy ("high demand") and the admin allows fallback: this
            # request is run once on the fallback model, and following requests go there for
            # a short while too. The response flags it via `gateway.fallback_model` and a header.
            meta["_fallback_tried"] = True
            self.prefer_fallback_until = time.time() + cfg.FALLBACK_STICKY_SECONDS
            log.warning("%s: primary model busy (high demand), retrying on fallback model", self.model_name)
            # Same prepared text and attachments (incl. a context.txt, still on disk until execute_prompt ends)
            return await self._execute_prompt(prompt_text, send_text, timeout, attachment_paths,
                                              images_only, want_images)

        if kind != CLASS_OK:
            self.last_error = kind
            self._record_outcome(False)
            if kind == CLASS_QUOTA:
                # Daily quota exhausted: long cooldown, recovers without operator intervention
                self.set_cooldown(cfg.QUOTA_COOLDOWN, reason=f"quota:{kw[:24]}")
                self.needs_attention = "quota"
                raise RuntimeError(
                    f"{self.model_name}: daily quota exhausted, retrying in {cfg.QUOTA_COOLDOWN // 60} minutes")
            if kind == CLASS_LOGIN:
                # Session expired: the operator must log in manually
                self.set_cooldown(cfg.LOGIN_COOLDOWN, reason=f"login:{kw[:24]}")
                self.needs_attention = "login"
                raise RuntimeError(
                    f"{self.model_name}: session expired, re-login required (via VNC)")
            if kind == CLASS_PROVIDER_ERROR:
                # Transient failure on the provider server: short cooldown, quick recovery
                self.set_cooldown(cfg.PROVIDER_ERROR_COOLDOWN, reason=f"provider:{kw[:24]}")
                raise RuntimeError(
                    f"{self.model_name}: provider temporarily not answering, try again")
            # Rate limit: the worker rests for RATE_LIMIT_COOLDOWN, healthy workers take the traffic
            if kind == CLASS_RATE_LIMIT:
                self.set_cooldown(cfg.RATE_LIMIT_COOLDOWN, reason=f"rate_limit:{kw[:20]}")
                raise RuntimeError(f"{self.model_name}: rate limited by the provider, resting "
                                   f"{cfg.RATE_LIMIT_COOLDOWN // 60} min")
            # Other temporary block (anti-bot keywords)
            self.set_cooldown(self.default_cooldown * 2, reason=f"kw:{kw[:20]}")
            raise RuntimeError(f"{self.model_name}: temporary block ({raw[:60]})")

        # 2. Quality check: degenerate answer (empty, one repeated symbol, punctuation only)
        if is_degenerate_response(raw):
            self.last_error = "degenerate_response"
            self._record_outcome(False)
            raise RuntimeError(f"{self.model_name}: degenerate answer ({raw[:30]!r})")

        self._record_outcome(True)
        return raw, images


class DeepSeekDirectWorker(WorkerTab):
    """Direct, browserless worker for DeepSeek using HTTP API and PoW challenge solver."""

    def __init__(self, token: Optional[str] = None):
        adapter = get_adapter("deepseek")
        super().__init__(
            browser_port=0,
            tab_id="direct",
            model_name="deepseek",
            ws_url="",
            adapter=adapter,
        )
        self.token = (token or cfg.DEEPSEEK_USER_TOKEN).strip()
        self._client: Optional[Any] = None
        self._stream_future: Optional[asyncio.Future] = None

    @property
    def client(self):
        if self._client is None:
            from .deepseek_direct import DeepSeekDirectClient
            self._client = DeepSeekDirectClient(self.token)
        return self._client

    async def prewarm(self) -> None:
        """Prewarm: no-op for direct worker."""
        return None

    @property
    def dispatch_lock(self) -> asyncio.Lock:
        return _ACCOUNT_DISPATCH.setdefault(self.account_key, asyncio.Lock())

    @property
    def cooldown_until(self) -> float:
        # Quota, login, hourly cap and captcha apply to every tab of an account.
        shared = _ACCOUNT_BLOCKED.get(self.account_key, (0.0, ""))[0]
        return max(getattr(self, "_cooldown_until", 0.0), shared)

    @cooldown_until.setter
    def cooldown_until(self, value: float) -> None:
        self._cooldown_until = value

    def is_available(self) -> bool:
        return (time.time() >= self.cooldown_until and not self.lock.locked()
                and not self.dispatch_lock.locked() and not self.sibling_blocked())

    def sibling_blocked(self) -> bool:
        # Cancelling an asyncio caller does not stop its synchronous HTTP read.
        # Keep this client out of dispatch until that read and cleanup finish.
        return bool(self._stream_future and not self._stream_future.done())

    def state(self, now: Optional[float] = None) -> str:
        return "BUSY" if self.sibling_blocked() else super().state(now)

    async def probe_session(self) -> tuple[bool, str]:
        if self.sibling_blocked():
            return True, "busy"
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, self.client.get_user_info)
            return True, "ok"
        except Exception as e:
            return False, f"auth:{e}"

    async def execute_prompt(
        self,
        prompt_text: str,
        timeout: int = 40,
        attachment_paths: Optional[List[str]] = None,
        images_only: bool = False,
        want_images: bool = False,
    ) -> Tuple[str, List[tuple[str, str]]]:
        if self.sibling_blocked():
            raise RuntimeError("The previous direct request is still stopping")
        send_text, tmpdir = self._overflow_to_file(
            prompt_text, attachment_paths, images_only, want_images)
        paths = attachment_paths
        if tmpdir:
            paths = [*(attachment_paths or []), os.path.join(tmpdir, OVERFLOW_FILE_NAME)]
        try:
            return await self._execute_direct(send_text, timeout, paths)
        finally:
            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)

    async def _execute_direct(self, prompt_text: str, timeout: int,
                              attachment_paths: Optional[List[str]]) -> Tuple[str, List[tuple[str, str]]]:
        self.total_requests += 1
        self.last_used = time.time()
        self._mark_request()
        meta = runmeta.get()
        phases = meta.setdefault("phases", {})
        t_mark = [time.monotonic()]

        def mark(name: str) -> None:
            now = time.monotonic()
            phases[name] = round(phases.get(name, 0.0) + now - t_mark[0], 2)
            t_mark[0] = now

        req_model = (meta.get("upstream_model") or "").lower()
        effort = (meta.get("effort") or "").lower()
        thinking = (
            "reasoner" in req_model
            or "r1" in req_model
            or effort in ("low", "medium", "high")
        )

        loop = asyncio.get_event_loop()

        # 1. Upload attachments if any
        ref_file_ids: List[str] = []
        if attachment_paths:
            for p in attachment_paths:
                fid = await loop.run_in_executor(None, self.client.upload_file, p)
                ref_file_ids.append(fid)
            mark("upload_files")

        # 2. Create isolated session
        session_id = await loop.run_in_executor(None, self.client.create_chat_session)
        mark("new_chat")

        on_progress = meta.get("on_progress")
        on_reasoning = meta.get("on_reasoning")
        meta["reasoning_content"] = ""
        accumulated_text = ""
        stop = threading.Event()
        client = self.client
        cleanup_seconds = [0.0]

        try:
            import queue
            q: queue.Queue = queue.Queue(maxsize=64)
            DONE = object()

            def enqueue(item):
                while not stop.is_set():
                    try:
                        q.put(item, timeout=.1)
                        return
                    except queue.Full:
                        pass

            def produce():
                stream = None
                try:
                    stream = client.stream_chat(
                        prompt=prompt_text,
                        ref_file_ids=ref_file_ids,
                        thinking=thinking,
                        session_id=session_id,
                    )
                    for chunk in stream:
                        if stop.is_set():
                            break
                        enqueue(chunk)
                except Exception as ex:
                    enqueue(ex)
                finally:
                    # The generator owns the response. Close it from its own
                    # thread before reusing the client or deleting its session.
                    if hasattr(stream, "close"):
                        try:
                            stream.close()
                        except Exception as ex:
                            enqueue(ex)
                    if cfg.DEEPSEEK_AUTO_CLEANUP:
                        cleanup_started = time.monotonic()
                        try:
                            client.delete_chat_session(session_id)
                        except Exception as ex:
                            log.warning("deepseek_direct cleanup failed: %s", type(ex).__name__)
                        cleanup_seconds[0] = time.monotonic() - cleanup_started
                    enqueue(DONE)

            self._stream_future = loop.run_in_executor(None, produce)

            while True:
                try:
                    item = await loop.run_in_executor(None, q.get, True, .1)
                except queue.Empty:
                    continue
                if item is DONE:
                    break
                if isinstance(item, Exception):
                    raise item
                if isinstance(item, dict):
                    if item.get("type") == "thinking":
                        meta["reasoning_content"] += item.get("text", "")
                        if on_reasoning is not None:
                            on_reasoning(meta["reasoning_content"])
                    if item.get("type") == "content":
                        accumulated_text += item.get("text", "")
                        if on_progress is not None:
                            on_progress(accumulated_text)

            await asyncio.shield(self._stream_future)
            mark("answer")
            if cleanup_seconds[0]:
                phases["answer"] = round(max(0, phases["answer"] - cleanup_seconds[0]), 2)
                phases["cleanup"] = round(cleanup_seconds[0], 2)
            self._record_outcome(True)
            # No account clock here: this worker has no browser tab to share an
            # account with, so there is nothing to pace against.
            self.last_finished = time.time()
            return accumulated_text, []
        except Exception as e:
            self._record_outcome(False)
            self.last_error = str(e)
            message = str(e).lower()
            if any(term in message for term in ("rate limit", "too many requests", "frequency", "429", "请求过于频繁")):
                self.set_cooldown(cfg.RATE_LIMIT_COOLDOWN, reason="rate_limit")
            raise
        finally:
            stop.set()


class ChallengeDetected(RuntimeError):
    """The provider showed a bot check (captcha); the request is not sent."""


class ClusterPool:
    """Discovers and manages matching tabs across all Chromium ports."""

    def __init__(self, ports: Optional[List[int]] = None):
        self.ports = ports or cfg.CDP_PORTS
        self.workers: List[WorkerTab] = []
        self._last_scan = 0.0
        self._scan_lock = asyncio.Lock()
        self.scan_errors: Dict[int, str] = {}

    async def scan(self, force: bool = False) -> None:
        """Initial/forced scans wait; refreshes use existing workers immediately."""
        fresh = self.workers and time.time() - self._last_scan < cfg.SCAN_REFRESH_INTERVAL
        if not force and fresh:
            return
        if not force and self.workers:
            task = getattr(self, "_scan_task", None)
            if task is None or task.done():
                self._scan_task = asyncio.create_task(self._refresh_scan())
            return
        await self._refresh_scan()

    async def _refresh_scan(self) -> None:
        async with self._scan_lock:
            try:
                await asyncio.get_running_loop().run_in_executor(None, self._scan_sync)
                self._last_scan = time.time()
            except Exception:
                log.exception("worker discovery refresh failed")


    def _scan_sync(self) -> None:
        discovered: List[WorkerTab] = []
        self.scan_errors.clear()

        for port in self.ports:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2.0) as resp:
                    tabs = json.loads(resp.read().decode())
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                self.scan_errors[port] = f"{type(e).__name__}: {e}"
                log.debug("scan port=%d down: %s", port, e)
                continue
            except Exception as e:
                self.scan_errors[port] = f"{type(e).__name__}: {e}"
                log.warning("scan port=%d unexpected error: %s", port, e)
                continue

            self._ensure_parallel_tabs(port, tabs)

            for tab in tabs:
                if tab.get("type") != "page":
                    continue
                # An explicit model for the port (e.g. anonymous chatgpt) takes
                # precedence over URL-based detection.
                model = cfg.PORT_MODEL_OVERRIDE.get(port) or detect_model_from_url(tab.get("url", ""))
                if not model:
                    continue
                # If direct API token is configured, skip Chrome deepseek tab in favor of direct worker
                if model == "deepseek" and cfg.DEEPSEEK_USER_TOKEN:
                    continue
                adapter = get_adapter(model)
                if not adapter:
                    continue
                ws_url = tab.get("webSocketDebuggerUrl")
                if not ws_url:
                    continue
                tab_id = tab.get("id", "")

                existing = next((w for w in self.workers if w.tab_id == tab_id and w.browser_port == port), None)
                if existing:
                    existing.ws_url = ws_url
                    discovered.append(existing)
                else:
                    discovered.append(WorkerTab(port, tab_id, model, ws_url, adapter))

        # Direct browserless DeepSeek integration
        if cfg.DEEPSEEK_USER_TOKEN:
            existing_direct = next((w for w in self.workers if isinstance(w, DeepSeekDirectWorker)), None)
            if existing_direct:
                discovered.append(existing_direct)
            else:
                discovered.append(DeepSeekDirectWorker(cfg.DEEPSEEK_USER_TOKEN))

        before, self.workers = len(self.workers), discovered
        log.info("scan complete workers=%d ports_up=%d/%d (was %d)",
                 len(discovered), len(self.ports) - len(self.scan_errors), len(self.ports), before)

    def _ensure_parallel_tabs(self, port: int, tabs: List[dict]) -> None:
        """Open the missing tab of a parallel-tab account (cfg.PARALLEL_TAB_MODELS).

        A new tab, not a new browser: same profile, same login, Chrome is not
        restarted. It shows up as a worker on the next scan. At most one attempt per
        browser every PARALLEL_TAB_OPEN_INTERVAL, so a failure cannot open tabs in a loop.
        """
        pages = [t for t in tabs if t.get("type") == "page"]
        by_model: Dict[str, List[dict]] = {}
        for t in pages:
            m = cfg.PORT_MODEL_OVERRIDE.get(port) or detect_model_from_url(t.get("url", ""))
            if m:
                by_model.setdefault(m, []).append(t)
        for model, mine in by_model.items():
            if (model not in cfg.PARALLEL_TAB_MODELS or len(mine) >= cfg.PARALLEL_TABS
                    or not parallel_allowed((model, port))):
                continue
            if time.time() - _TAB_OPENED_AT.get(port, 0.0) < cfg.PARALLEL_TAB_OPEN_INTERVAL:
                continue
            _TAB_OPENED_AT[port] = time.time()
            adapter = get_adapter(model)
            url = getattr(adapter, "TEMP_URL", None) or mine[0].get("url", "")
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/json/new?{urllib.parse.quote(url, safe=':/?=&')}", method="PUT")
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    tab_id = json.loads(resp.read().decode()).get("id", "")
                log.info("parallel tab opened model=%s port=%d tab=%s", model, port, tab_id[:8])
            except Exception as e:
                log.warning("parallel tab not opened model=%s port=%d: %s", model, port, e)

    def workers_for_model(self, model_name: str) -> List[WorkerTab]:
        m = (model_name or "").lower()
        if m in cfg.DEEPSEEK_MODEL_ALIASES:
            m = "deepseek"
        return [w for w in self.workers if w.model_name == m]

    def summary(self) -> List[Dict[str, Any]]:
        now = time.time()
        return [
            {
                "model": w.model_name,
                "instance": (w.browser_port - 9222) if w.browser_port else 0,
                "state": w.state(now),
                "cooldown_reason": w.cooldown_reason if now < w.cooldown_until else None,
                "browser_port": w.browser_port,
                "tab_id": w.short_tab,
                "available": w.is_available(),
                "locked": w.lock.locked(),
                "cooldown_left": max(0, int(w.cooldown_until - now)),
                "total_requests": w.total_requests,
                "failed_requests": w.failed_requests,
                "consecutive_failures": w.consecutive_failures,
                "health_score": round(w.health_score, 2),
                "requests_last_hour": w.hourly_count(),
                "hourly_cap": cfg.MAX_REQUESTS_PER_HOUR,
                "needs_attention": w.needs_attention,
                "last_used": int(w.last_used) if w.last_used else None,
                "last_error": w.last_error,
            }
            for w in self.workers
        ]

    async def probe_sessions(self, force: bool = False) -> Dict[str, str]:
        """Check the session of every idle worker (without sending a prompt).

        Workers that lost their login are flagged `needs_attention="login"` and
        put in cooldown, so real requests do not land on them and users do not wait.

        Returns {worker_key: reason} for problematic workers only.
        """
        problems: Dict[str, str] = {}
        for w in list(self.workers):
            if w.lock.locked():
                continue
            if not force and w.needs_attention == "captcha" and time.time() < w.cooldown_until:
                continue
            # Workers in cooldown are normally left alone. Exception: a worker
            # flagged for attention (login/quota); checking it is cheap (no
            # prompt is sent) and if it has actually recovered, it returns to
            # service without waiting out the cooldown.
            if time.time() < w.cooldown_until and not w.needs_attention:
                continue
            key = f"{w.model_name}:{w.browser_port}"

            if isinstance(w, DeepSeekDirectWorker):
                async with w.lock:
                    healthy, reason = await w.probe_session()
                w.apply_probe(healthy, reason)
                if not healthy:
                    problems[key] = reason
                continue

            # IMPORTANT: take the lock; otherwise the probe connects to the same tab
            # as a real request and the CDP replies get mixed up.
            async with w.lock:
                try:
                    async with await w.connect(open_timeout=5) as ws:
                        healthy, reason = await w.adapter.check_session(ws)
                except Exception as e:
                    healthy, reason = False, f"ws:{type(e).__name__}"
            # Short pause so Chrome can finish closing the CDP connection
            await asyncio.sleep(0.3)

            w.apply_probe(healthy, reason)
            if not healthy:
                problems[key] = reason
        return problems

    def health(self) -> Dict[str, Any]:
        total = len(self.workers)
        available = sum(1 for w in self.workers if w.is_available())
        by_model: Dict[str, Dict[str, int]] = {}
        for w in self.workers:
            m = by_model.setdefault(w.model_name, {"total": 0, "available": 0})
            m["total"] += 1
            if w.is_available():
                m["available"] += 1
        needs_attention = [
            {"model": w.model_name, "port": w.browser_port, "reason": w.needs_attention}
            for w in self.workers if w.needs_attention
        ]
        return {
            "accounts_total": len({w.account_key for w in self.workers}),
            "accounts_available": len({w.account_key for w in self.workers if w.is_available()}),
            "total_workers": total,
            "available_workers": available,
            "needs_attention": needs_attention,
            "by_model": by_model,
            "ports_up": [p for p in self.ports if p not in self.scan_errors],
            "ports_down": self.scan_errors,
            "last_scan": int(self._last_scan),
        }
