"""Shared helpers: error format, auth dependencies, user creation."""
from __future__ import annotations

from fastapi import Depends, Request

from . import referral, security
from .billing import credit, ensure_wallet
from .db import Database, now

SESSION_COOKIE = "sf_session"

# When a model is down and the request is retried on its stand-in, the second
# attempt needs a request id of its own (`usage_logs.request_id` is unique) and
# the two have to stay recognisably one request. The retry carries the original
# id plus this suffix, which is what lets the panel show a failed first attempt
# together with the answer that was actually delivered.
FALLBACK_SUFFIX = "f"


# English wording for the /v1 API, by error code.
#
# The panel speaks Uzbek to the people who run the gateway; /v1 speaks to other people's
# programs, where an SDK, a log line or a support thread is read in English — and where
# the code, not the prose, is what a client branches on. The message is replaced by code
# at the boundary (see main.create_app) so call sites keep their Uzbek text for the panel.
# A code that is missing here keeps whatever message it was raised with.
API_ERRORS_EN = {
    # request
    "invalid_json": "The request body is not valid JSON.",
    "invalid_body": "The request body is not valid.",
    "invalid_messages": "`messages` must be a non-empty list of chat messages.",
    "invalid_prompt": "`prompt` must be a non-empty string.",
    "invalid_max_tokens": "`max_tokens` is out of range.",
    "invalid_n": "`n` is out of range; only one completion per request is supported.",
    "invalid_image": "The image could not be read.",
    "invalid_request": "The request is not valid.",
    "unsupported_endpoint": "This endpoint is not supported.",
    "unsupported_feature": "The selected model does not support this feature.",
    "invalid_response_format": "The requested `response_format` is not supported by this model.",
    "request_too_large": "The request body is too large.",
    # context_length_exceeded is deliberately absent: it is already raised in English,
    # with the model's context window in it, and agent CLIs read that number.
    # authentication and access
    "not_authenticated": "No API key was provided.",
    "invalid_api_key": "The API key is not valid.",
    "revoked_api_key": "This API key has been revoked.",
    "account_suspended": "This account is suspended.",
    "forbidden": "This key is not allowed to use that.",
    # model
    "model_not_found": "The requested model does not exist.",
    "model_offline": "The requested model is offline.",
    "model_unavailable": "The requested model is unavailable right now.",
    "model_busy": "The model is busy. Try again shortly.",
    "queue_full": "The queue for this model is full. Try again shortly.",
    # money and limits
    "insufficient_balance": "The account balance is too low for this request.",
    "key_limit_reached": "This key has reached its monthly spending limit.",
    "too_many_concurrent": "Too many requests in flight for this key.",
    "free_daily_limit": "The free daily token limit for this model has been used up.",
    "server_busy": "The gateway is at capacity. Try again shortly.",
    # upstream
    "upstream_rejected": "The model rejected the request.",
    "upstream_failed": "The model did not return an answer.",
    "upstream_timeout": "The model did not answer in time.",
    "upstream_error": "The model server returned an error. Try again shortly.",
    "invalid_output": "The model's answer could not be used. Try again.",
    "generation_failed": "The model's answer got stuck repeating itself. Try again.",
    "rate_limit_exceeded": "Rate limit exceeded. Try again shortly.",
    "ip_rate_limit_exceeded": "Rate limit for this IP address exceeded. Try again shortly.",
    "key_shared_rate_limit": "This key is used from too many addresses and has been slowed down.",
    "not_found": "Not found.",
}


class ApiError(Exception):
    """OpenAI-style error: {"error": {"message", "type", "code"}}."""

    def __init__(self, status: int, code: str, message: str, type_: str = "invalid_request_error",
                 headers: dict[str, str] | None = None, en: str | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.type, self.headers = status, code, message, type_, headers or {}
        # Set where the Uzbek message carries numbers or names worth keeping in English
        # too; otherwise API_ERRORS_EN[code] is used.
        self.en = en

    def body(self, english: bool = False) -> dict:
        message = (self.en or API_ERRORS_EN.get(self.code) or self.message) if english else self.message
        return {"error": {"message": message, "type": self.type, "code": self.code}}


def get_db(request: Request) -> Database:
    return request.app.state.db


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "-"


def csrf_guard(request: Request) -> None:
    """Portal API: state-changing requests are accepted only from our own pages (with a custom header)."""
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-sf-csrf") != "1":
        raise ApiError(403, "csrf", "So'rov rad etildi.", "permission_error")


def optional_user(request: Request, db: Database = Depends(get_db)):
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    row = db.one(
        """SELECT u.id, u.email, u.name, u.role, u.status, s.expires_at
           FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.id = ?""",
        (security.sha256_hex(token),),
    )
    if row is None or row["expires_at"] < now() or row["status"] != "active":
        return None
    return dict(row)


def require_user(user=Depends(optional_user)):
    if user is None:
        raise ApiError(401, "not_authenticated", "Tizimga kiring.", "authentication_error")
    return user


def require_admin(user=Depends(require_user)):
    if user["role"] != "admin":
        raise ApiError(403, "forbidden", "Bu bo'lim faqat administratorlar uchun.", "permission_error")
    return user


def create_user(db: Database, *, email: str, name: str, password: str | None, role: str = "user",
                bonus_micro: int = 0, admin_id: int | None = None,
                signup_ip: str | None = None, google_sub: str | None = None,
                telegram_id: str | None = None, referral_code: str | None = None,
                signup_method: str = "password", device: str | None = None) -> int:
    """Creates a user. `password=None` means an account created via Google/Telegram.

    `referral_code` is the code from an invite link: the referral link is written here, in the same
    transaction as the account (see referral.py). An invalid code does not block sign-up.
    """
    email = email.strip().lower()
    if db.one("SELECT 1 FROM users WHERE email=?", (email,)):
        raise ApiError(409, "email_taken", "Bu email bilan foydalanuvchi allaqachon bor.")
    pw = security.hash_password(password) if password is not None else security.NO_PASSWORD
    with db.tx() as c:
        cur = c.execute(
            "INSERT INTO users(email, name, password_hash, role, status, created_at, signup_ip, google_sub, telegram_id)"
            " VALUES (?,?,?,?, 'active', ?, ?, ?, ?)",
            (email, name.strip(), pw, role, now(), signup_ip, google_sub, telegram_id),
        )
        uid = cur.lastrowid
        ensure_wallet(c, uid)
        referral.on_signup_tx(c, user_id=uid, code=referral_code, method=signup_method,
                              ip=signup_ip, device=referral.normalize_device(device))
    if bonus_micro > 0:
        credit(db, user_id=uid, amount=bonus_micro, kind="bonus", note="Boshlang'ich mablag'", admin_id=admin_id)
    return uid


def public_user(row) -> dict:
    return {"id": row["id"], "email": row["email"], "name": row["name"], "role": row["role"]}
