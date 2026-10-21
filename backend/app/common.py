"""Shared helpers: error format, auth dependencies, user creation."""
from __future__ import annotations

from fastapi import Depends, Request

from . import referral, security
from .billing import credit, ensure_wallet
from .db import Database, now

SESSION_COOKIE = "sf_session"


class ApiError(Exception):
    """OpenAI-style error: {"error": {"message", "type", "code"}}."""

    def __init__(self, status: int, code: str, message: str, type_: str = "invalid_request_error",
                 headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.type, self.headers = status, code, message, type_, headers or {}

    def body(self) -> dict:
        return {"error": {"message": self.message, "type": self.type, "code": self.code}}


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
