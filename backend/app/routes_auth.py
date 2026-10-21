"""/portal/auth/*: login, logout, registration, password change."""
from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Depends, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr, Field

from . import branding
from . import abuse, captcha, oauth, password_reset, referral, security
from .common import (SESSION_COOKIE, ApiError, client_ip, create_user, csrf_guard, get_db, optional_user,
                     public_user, require_user)
from .db import Database, now

log = logging.getLogger("auth")

router = APIRouter(prefix="/portal/auth", dependencies=[Depends(csrf_guard)], tags=["auth"])

LOGIN_WINDOW = 15 * 60
MAX_FAILS_PER_IP = 20
MAX_FAILS_PER_EMAIL = 8

# reCAPTCHA v3 token (see captcha.py). Ignored when captcha is disabled.
CaptchaField = Field(default="", max_length=captcha.TOKEN_MAX)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)
    # "Remember me" checkbox. When unchecked the session is short, so it
    # does not linger on a shared or foreign computer. Without this field
    # pydantic would silently drop it and the checkbox would do nothing.
    remember: bool = True
    captcha_token: str = CaptchaField


class RegisterIn(BaseModel):
    email: EmailStr
    name: str = Field(default="", max_length=80)
    password: str = Field(min_length=8, max_length=200)
    captcha_token: str = CaptchaField
    # Code from the referral link. If empty the `sf_ref` cookie is used (the frontend stores both).
    ref: str = Field(default="", max_length=32)


def _ref_code(request: Request, explicit: str = "") -> str | None:
    """Referral code: from the request body first, else from the cookie (survives OAuth redirects)."""
    return referral.normalize_code(explicit) or referral.normalize_code(request.cookies.get(referral.REF_COOKIE))


def _device(request: Request) -> str | None:
    return referral.normalize_device(request.cookies.get(referral.DEVICE_COOKIE))


class PasswordIn(BaseModel):
    current: str = Field(min_length=1, max_length=200)
    new: str = Field(min_length=8, max_length=200)


# Session lifetime in hours when "remember me" is unchecked. The cookie
# survives closing the browser but not the end of the day, which protects
# against a session left behind on a shared computer.
SHORT_SESSION_HOURS = 12


def _start_session(request: Request, response: Response, db: Database, user_id: int,
                   remember: bool = True) -> None:
    token = security.new_session_token()
    cfg = request.app.state.config
    lifetime = cfg.session_days * 86400 if remember else SHORT_SESSION_HOURS * 3600
    ts = now()
    with db.tx() as c:
        c.execute("DELETE FROM sessions WHERE expires_at < ?", (ts,))
        c.execute(
            "INSERT INTO sessions(id, user_id, created_at, expires_at, ip, user_agent) VALUES (?,?,?,?,?,?)",
            (security.sha256_hex(token), user_id, ts, ts + lifetime, client_ip(request),
             (request.headers.get("user-agent") or "")[:200]),
        )
        c.execute("UPDATE users SET last_login_at=? WHERE id=?", (ts, user_id))
    response.set_cookie(
        SESSION_COOKIE, token, max_age=lifetime, httponly=True,
        secure=cfg.cookie_secure, samesite="lax", path="/",
    )


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response, db: Database = Depends(get_db)):
    limiter = request.app.state.auth_limiter
    ip, email = client_ip(request), body.email.lower()
    if limiter.count(("ip", ip)) >= MAX_FAILS_PER_IP or limiter.count(("email", email)) >= MAX_FAILS_PER_EMAIL:
        raise ApiError(429, "too_many_attempts", "Juda ko'p urinish. 15 daqiqadan keyin qayta urinib ko'ring.",
                       "rate_limit_error", {"Retry-After": "900"})
    _login_captcha(request, db, body.captcha_token, ip, email)
    row = db.one("SELECT * FROM users WHERE email=?", (email,))
    ok = security.verify_password(body.password, row["password_hash"] if row else security.DUMMY_HASH)
    if not row or not ok:
        limiter.hit(("ip", ip))
        limiter.hit(("email", email))
        raise ApiError(401, "invalid_credentials", "Email yoki parol noto'g'ri.", "authentication_error")
    if row["status"] != "active":
        raise ApiError(403, "account_suspended", "Hisobingiz to'xtatilgan. Administrator bilan bog'laning.",
                       "permission_error")
    limiter.reset(("email", email))
    _start_session(request, response, db, row["id"], remember=body.remember)
    return {"user": public_user(row)}


def _login_captcha(request: Request, db: Database, token: str, ip: str, email: str) -> None:
    """Login captcha: always, or after N failed attempts, depending on settings.

    Failed attempts are counted by the same rate limiter as above (15-minute
    window, separately per IP and per email). A regular user never sees the
    captcha, while a password-guessing bot stops after attempt N: without a
    captcha the password is not checked at all.
    """
    cfg = captcha.settings(db, request.app.state.config)
    if cfg is None:
        return
    n = cfg["login_after_fails"]
    if n:
        limiter = request.app.state.auth_limiter
        if limiter.count(("ip", ip)) < n and limiter.count(("email", email)) < n:
            return
    captcha.verify(request, db, token, "login")


@router.post("/logout")
def logout(request: Request, response: Response, db: Database = Depends(get_db)):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        with db.tx() as c:
            c.execute("DELETE FROM sessions WHERE id=?", (security.sha256_hex(token),))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def me(user=Depends(optional_user), db: Database = Depends(get_db)):
    settings = db.get_settings()
    free_mode = settings.get("free_mode") == "1"
    if user is None:
        return {"user": None, "registration_open": settings.get("registration_open") == "1", "free_mode": free_mode,
                "site": branding.site_info()}
    return {"user": public_user(user), "registration_open": False, "free_mode": free_mode, "site": branding.site_info()}


@router.post("/register")
def register(body: RegisterIn, request: Request, response: Response, db: Database = Depends(get_db)):
    settings = db.get_settings()
    if settings.get("registration_open") != "1":
        raise ApiError(403, "registration_closed", "Ro'yxatdan o'tish hozircha yopiq. Administratordan taklif so'rang.",
                       "permission_error")
    limiter = request.app.state.auth_limiter
    ip = client_ip(request)
    allowed, _ = request.app.state.signup_limiter.allow(ip, 5)
    if not allowed:
        raise ApiError(429, "too_many_attempts", "Juda ko'p urinish. Keyinroq qayta urinib ko'ring.", "rate_limit_error")
    captcha.verify(request, db, body.captcha_token, "register")
    # Limit excessive account creation from a single IP
    abuse.check_signup(db, ip)
    # Signup bonus: only once per IP. Creating the account is not blocked;
    # cloning accounts to farm bonuses just stops paying off.
    bonus, bonus_limited = abuse.signup_bonus(
        db, ip, int(settings.get("signup_bonus_micro", "0")))
    uid = create_user(db, email=body.email, name=body.name, password=body.password,
                      bonus_micro=bonus, signup_ip=ip, referral_code=_ref_code(request, body.ref),
                      signup_method="password", device=_device(request))
    _start_session(request, response, db, uid)
    response.delete_cookie(referral.REF_COOKIE, path="/")  # the code has been used
    row = db.one("SELECT * FROM users WHERE id=?", (uid,))
    return {"user": public_user(row), "bonus_micro": bonus,
            "bonus_limited": bonus_limited}


@router.post("/password")
def change_password(body: PasswordIn, request: Request, user=Depends(require_user), db: Database = Depends(get_db)):
    row = db.one("SELECT password_hash FROM users WHERE id=?", (user["id"],))
    if not security.has_password(row["password_hash"]):
        # Accounts created via Google/Telegram have no password to change.
        raise ApiError(400, "no_password",
                       "Bu hisobga Google yoki Telegram orqali kirilgan — parol o'rnatilmagan. "
                       "Parol kerak bo'lsa, administrator bilan bog'laning.")
    if not security.verify_password(body.current, row["password_hash"]):
        raise ApiError(400, "wrong_password", "Joriy parol noto'g'ri.")
    keep = security.sha256_hex(request.cookies.get(SESSION_COOKIE, ""))
    with db.tx() as c:
        c.execute("UPDATE users SET password_hash=? WHERE id=?", (security.hash_password(body.new), user["id"]))
        c.execute("DELETE FROM sessions WHERE user_id=? AND id<>?", (user["id"], keep))  # sign out other devices
    return {"ok": True}


# --- Password reset via the Telegram bot ------------------------------------
#
# The response is always the same: whether the email exists or has Telegram
# linked must not be observable from outside, otherwise this becomes an
# account enumeration tool. Details go to the log only.

class ForgotIn(BaseModel):
    email: EmailStr
    captcha_token: str = CaptchaField


class ResetIn(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    password: str = Field(min_length=8, max_length=200)


FORGOT_RESPONSE = {"ok": True, "detail": "Agar bu email bizda ro'yxatdan o'tgan va unga "
                               "Telegram ulangan bo'lsa, tiklash havolasi "
                               "Telegramga yuborildi."}


@router.post("/password/forgot")
async def password_forgot(body: ForgotIn, request: Request, db: Database = Depends(get_db)):
    ip = client_ip(request)
    # Rate limit: prevents flooding Telegram with messages through
    # repeated requests from one IP.
    ok, retry = request.app.state.ip_rpm.allow(("forgot", ip), 5)
    if not ok:
        raise ApiError(429, "too_many_requests",
                       f"Juda ko'p urinish. {retry} soniyadan keyin qayta urinib ko'ring.",
                       "rate_limit_error", {"Retry-After": str(retry)})
    # Captcha BEFORE the email lookup: its error does not reveal whether the account
    # exists, and a bot cannot flood other people's Telegram.
    await run_in_threadpool(captcha.verify, request, db, body.captcha_token, "password_reset")

    result = await run_in_threadpool(password_reset.create_request, db, body.email, ip)
    if result is None:
        return FORGOT_RESPONSE

    chat_id, token = result
    s = await run_in_threadpool(db.get_settings)
    bot_token = ((s.get("telegram_login_token") or "").strip()
                 or (s.get("alert_telegram_token") or "").strip())
    if not bot_token:
        log.warning("password reset: Telegram bot is not configured")
        return FORGOT_RESPONSE

    # Never built from the Host header: a forged Host would send the victim a link to another site
    base = ((s.get("site_base_url") or "").strip() or branding.SITE_URL).rstrip("/")
    if not base:
        log.warning("password reset: site_base_url / SF_SITE_URL is not set, link not sent")
        return FORGOT_RESPONSE
    link = f"{base}/reset-password?token={token}"
    try:
        r = await request.app.state.notify_http.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": password_reset.message_text(link),
                  "disable_web_page_preview": True})
        if r.status_code != 200:
            log.warning("password reset message not sent: HTTP %s", r.status_code)
    except Exception as e:
        log.warning("password reset message not sent: %s", type(e).__name__)
    return FORGOT_RESPONSE


@router.get("/password/reset-check")
async def password_reset_check(token: str, db: Database = Depends(get_db)):
    """Whether the link is still valid when the page opens, so the user does not
    see 'link expired' only after typing the new password."""
    uid = await run_in_threadpool(password_reset.check_token, db, token)
    return {"valid": uid is not None}


@router.post("/password/reset")
async def password_reset_apply(body: ResetIn, db: Database = Depends(get_db)):
    ok = await run_in_threadpool(password_reset.reset_password, db, body.token, body.password)
    if not ok:
        raise ApiError(400, "invalid_token",
                       "Havola eskirgan yoki allaqachon ishlatilgan. "
                       "Yangi havola so'rang.")
    return {"ok": True}


# --- Sign-in via Google and Telegram ----------------------------------------
#
# Both flows work through browser redirects, so they are GET: on error the
# user is sent back to the login page with a code (the text lives in the
# frontend, so a fake message cannot be injected through the URL).

def _oauth_done(request: Request, db: Database, user_id: int) -> RedirectResponse:
    response = RedirectResponse("/app", status_code=303)
    _start_session(request, response, db, user_id)
    response.delete_cookie(oauth.OAUTH_COOKIE, path="/")
    response.delete_cookie(referral.REF_COOKIE, path="/")
    return response


def _oauth_fail(code: str) -> RedirectResponse:
    response = RedirectResponse(f"/login?oauth_error={code}", status_code=303)
    response.delete_cookie(oauth.OAUTH_COOKIE, path="/")
    return response


@router.get("/providers")
async def providers(request: Request, db: Database = Depends(get_db)):
    """Tells the login page which buttons to show.

    Returns empty values when keys are not configured, so the button is not drawn at all.
    """
    return {"google": oauth.google_ready(db),
            "telegram": await oauth.telegram_bot_username(request.app.state),
            # reCAPTCHA v3: {"site_key", "login_after_fails"} or None (disabled)
            "captcha": await run_in_threadpool(captcha.settings, db, request.app.state.config)}


@router.get("/google/start")
def google_start(request: Request, db: Database = Depends(get_db)):
    url, state = oauth.google_start(db, request)
    response = RedirectResponse(url, status_code=303)
    # State, PKCE verifier and nonce: only in this browser, only for 10 minutes.
    response.set_cookie(oauth.OAUTH_COOKIE, state, max_age=oauth.OAUTH_TTL, httponly=True,
                        secure=request.app.state.config.cookie_secure, samesite="lax", path="/")
    return response


@router.get("/google/callback")
async def google_callback(request: Request, db: Database = Depends(get_db)):
    q = request.query_params
    if q.get("error"):
        return _oauth_fail("google_cancelled")
    code, state = q.get("code", ""), q.get("state", "")
    stored = (request.cookies.get(oauth.OAUTH_COOKIE) or "").split("|")
    if len(stored) != 3 or not code or not state or not hmac.compare_digest(stored[0], state):
        # State mismatch: the request did not come from a flow we started (CSRF).
        return _oauth_fail("google_state")
    _, verifier, nonce = stored
    try:
        r = await request.app.state.notify_http.post(
            oauth.GOOGLE_TOKEN_URL, data=oauth.google_exchange_form(db, request, code, verifier))
        data = r.json() if r.content else {}
    except Exception:
        return _oauth_fail("google_unreachable")
    if r.status_code != 200 or not data.get("id_token"):
        return _oauth_fail("google_rejected")
    client_id, _ = oauth.google_keys(db)
    try:
        claims = oauth.id_token_claims(data["id_token"], client_id=client_id, nonce=nonce)
        uid, _bonus, _new = oauth.link_or_create(
            db, provider="google", external_id=str(claims["sub"]),
            email=claims["email"], name=(claims.get("name") or "")[:80], ip=client_ip(request),
            referral_code=_ref_code(request), device=_device(request))
    except ApiError as exc:
        return _oauth_fail(exc.code)
    return _oauth_done(request, db, uid)


@router.get("/telegram/callback")
def telegram_callback(request: Request, db: Database = Depends(get_db)):
    """Data returned by the Telegram Login Widget (via `data-auth-url`).

    Nothing is trusted until the signature is verified with the bot token.
    """
    try:
        fields = oauth.verify_telegram(dict(request.query_params), oauth.telegram_token(db))
        uid, _bonus, _new = oauth.link_or_create(
            db, provider="telegram", external_id=str(fields["id"]), email="",
            name=oauth.telegram_name(fields)[:80], ip=client_ip(request),
            referral_code=_ref_code(request), device=_device(request))
    except ApiError as exc:
        return _oauth_fail(exc.code)
    return _oauth_done(request, db, uid)
