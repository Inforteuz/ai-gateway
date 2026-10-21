"""Captcha (Google reCAPTCHA v3): protects login, signup and password reset from bots.

v3 is INVISIBLE: the user is shown nothing. The browser gets a token from Google
when submitting the form; the server checks it via `siteverify` and compares
Google's score (0.0 = bot, 1.0 = human) with a threshold. The browser's own
claim that it "passed" is never trusted.

RULES (each one deliberate):

1. Without keys captcha is OFF: that is how local development and tests run.
   Keys live in `/etc/ai-gateway.env` (SF_RECAPTCHA_*) or in admin settings;
   the environment wins. The secret never reaches logs or responses.

2. If verification fails to run (Google did not answer, timeout) the request is
   REJECTED. Otherwise blocking access to Google would become a way to bypass
   the captcha. For emergencies there is SF_CAPTCHA=0.

3. The token is bound to the `action` and the site domain (`hostname`): a token
   obtained on the login page cannot be used for signup, and one obtained on a
   foreign site is not accepted.

4. Verification runs BEFORE checking whether the email exists — a captcha
   error does not reveal whether an account exists.
"""
from __future__ import annotations

import logging

import httpx
from fastapi import Request

from .common import ApiError, client_ip
from .db import Database

log = logging.getLogger("captcha")

VERIFY_URL = "https://www.google.com/recaptcha/api/siteverify"
# Google usually answers in 100-300 ms. Waiting longer leaves the user hanging,
# a shorter timeout rejects needlessly on slow networks.
TIMEOUT = 5.0
TOKEN_MAX = 4096          # a v3 token is usually ~500-2000 characters
DEFAULT_MIN_SCORE = 0.5   # Google's recommendation
from .branding import hostnames as _hosts
DEFAULT_HOSTNAMES = _hosts()

MSG_REQUIRED = "Davom etish uchun xavfsizlik tekshiruvidan (captcha) o'ting. Sahifani yangilab, qayta urinib ko'ring."
MSG_FAILED = "Xavfsizlik tekshiruvi o'tmadi yoki eskirdi. Qaytadan urinib ko'ring."
MSG_LOW_SCORE = ("Xavfsizlik tekshiruvi so'rovni shubhali deb topdi. Birozdan keyin qayta urinib ko'ring; "
                 "VPN yoki anonimayzer yoqilgan bo'lsa, o'chirib ko'ring.")
MSG_UNAVAILABLE = "Xavfsizlik tekshiruvi vaqtincha ishlamayapti. Birozdan keyin qayta urinib ko'ring."


def _resolve(db: Database, cfg) -> tuple[str, str, dict[str, str]] | None:
    """(site_key, secret, settings) — or None when captcha is off."""
    if not cfg.captcha_enabled:
        return None
    s = db.get_settings()
    if s.get("captcha_enabled", "1") != "1":
        return None
    site = cfg.recaptcha_site_key or (s.get("recaptcha_site_key") or "").strip()
    secret = cfg.recaptcha_secret or (s.get("recaptcha_secret") or "").strip()
    if not site or not secret:
        return None
    return site, secret, s


def settings(db: Database, cfg) -> dict | None:
    """Public settings for the browser; None when captcha is off."""
    got = _resolve(db, cfg)
    if got is None:
        return None
    site, _, s = got
    return {"site_key": site, "login_after_fails": login_after_fails(s)}


def login_after_fails(s: dict[str, str]) -> int:
    try:
        return max(0, int(s.get("captcha_login_after_fails") or 0))
    except ValueError:
        return 0


def min_score(s: dict[str, str]) -> float:
    try:
        v = float(s.get("captcha_min_score") or DEFAULT_MIN_SCORE)
    except ValueError:
        return DEFAULT_MIN_SCORE
    return min(1.0, max(0.0, v))


def allowed_hostnames(s: dict[str, str]) -> set[str]:
    """An empty set means the domain is not checked."""
    raw = s.get("captcha_hostnames", DEFAULT_HOSTNAMES)
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def _http(app) -> httpx.Client:
    # Sync client: the login and signup routes are synchronous (run in the threadpool).
    client = getattr(app.state, "captcha_http", None)
    if client is None:
        client = app.state.captcha_http = httpx.Client(timeout=TIMEOUT)
    return client


def _unavailable() -> ApiError:
    return ApiError(503, "captcha_unavailable", MSG_UNAVAILABLE, "api_error")


def verify(request: Request, db: Database, token: str | None, action: str) -> None:
    """Verifies the token when captcha is on; raises ApiError when it fails.

    Synchronous (makes a network call): call it from async code via `run_in_threadpool`.
    """
    got = _resolve(db, request.app.state.config)
    if got is None:
        return
    _, secret, s = got
    token = (token or "").strip()
    if not token:
        raise ApiError(400, "captcha_required", MSG_REQUIRED)
    if len(token) > TOKEN_MAX:
        raise ApiError(400, "captcha_failed", MSG_FAILED)
    try:
        r = _http(request.app).post(
            VERIFY_URL, timeout=TIMEOUT,
            data={"secret": secret, "response": token, "remoteip": client_ip(request)})
        data = r.json() if r.status_code == 200 else None
    except Exception as e:
        # Only the exception type: the request body holds the secret, never log it.
        log.warning("captcha: could not reach Google (%s)", type(e).__name__)
        raise _unavailable() from None
    if not isinstance(data, dict):
        log.warning("captcha: unexpected response from Google (HTTP %s)", r.status_code)
        raise _unavailable()
    if data.get("success") is not True:
        codes = [str(c) for c in (data.get("error-codes") or [])][:5]
        if any(c in ("missing-input-secret", "invalid-input-secret") for c in codes):
            # Our configuration is wrong: not the user's fault.
            log.error("captcha: secret is invalid or missing (%s)", ",".join(codes))
            raise _unavailable()
        log.info("captcha rejected action=%s (%s)", action, ",".join(codes) or "-")
        raise ApiError(400, "captcha_failed", MSG_FAILED)
    got_action = str(data.get("action") or "")
    if got_action != action:
        log.info("captcha: action mismatch (expected %s, got %.32s)", action, got_action)
        raise ApiError(400, "captcha_failed", MSG_FAILED)
    hosts = allowed_hostnames(s)
    host = str(data.get("hostname") or "").lower()
    if hosts and host not in hosts:
        log.warning("captcha: token from a foreign domain (%.64s) action=%s", host, action)
        raise ApiError(400, "captcha_failed", MSG_FAILED)
    score = data.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        # No score — not a v3 key (e.g. a v2 key was configured)
        log.error("captcha: no score in the response — is the key not reCAPTCHA v3?")
        raise _unavailable()
    if score < min_score(s):
        log.info("captcha: low score %.1f action=%s", score, action)
        raise ApiError(400, "captcha_low_score", MSG_LOW_SCORE)
