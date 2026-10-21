"""Password reset via the Telegram bot.

WHY NOT EMAIL: there is no mail sender, and setting one up is a project of its
own (SPF, DKIM, deliverability). Most users already sign in through Telegram,
so their `telegram_id` is known; sending through that channel is both faster
and more reliable.

SECURITY RULES (each one deliberate):

1. The response is ALWAYS the same. Whether the email exists, or whether it
   has Telegram linked, cannot be inferred from the response; otherwise this
   would become an account enumeration tool.

2. The token ITSELF is never stored, only its sha256 hash. A database leak
   does not yield a usable token.

3. The token is single-use and expires in 15 minutes.

4. After the password changes, ALL of the user's sessions are deleted:
   a password reset is often caused by a compromised account.

5. A new request for an account invalidates older unused tokens, so there is
   never more than one valid link at a time.

6. External (Google/Telegram) accounts have no password at all; no reset link
   is sent to them because there is nothing to reset.
"""
from __future__ import annotations

import logging
import secrets
import time

from .db import Database
from . import branding
from . import security

log = logging.getLogger("password_reset")

# Link lifetime. Short is better: the message stays in Telegram and a
# long-lived link is a risk.
TOKEN_TTL = 15 * 60

# The `password_resets` table lives in `schema.sql` (the app role cannot run DDL).


def cleanup(db: Database) -> None:
    """Removes expired and used rows (keeps the table from growing)."""
    cutoff = int(time.time()) - 7 * 86400
    with db.tx() as c:
        c.execute("DELETE FROM password_resets WHERE expires_at < ? OR used_at IS NOT NULL",
                  (cutoff,))


def create_request(db: Database, email: str, ip: str | None) -> tuple[int, str] | None:
    """Creates a reset token.

    Returns (telegram_id, token) for sending the message, or None if the
    account is not found, has no Telegram linked, or has no password. The
    caller MUST return the same response in every case.
    """
    row = db.one(
        "SELECT id, telegram_id, password_hash, status FROM users WHERE email=?",
        (email.strip(),))
    if row is None or row["status"] != "active":
        return None
    if not row["telegram_id"]:
        return None
    if not security.has_password(row["password_hash"]):
        # External account: no password to reset
        return None

    token = secrets.token_urlsafe(32)
    ts = int(time.time())
    with db.tx() as c:
        # Invalidate older unused links
        c.execute("DELETE FROM password_resets WHERE user_id=? AND used_at IS NULL",
                  (row["id"],))
        c.execute(
            "INSERT INTO password_resets(user_id, token_hash, created_at, expires_at, request_ip)"
            " VALUES (?,?,?,?,?)",
            (row["id"], security.sha256_hex(token), ts, ts + TOKEN_TTL, ip))
    return int(row["telegram_id"]), token


def check_token(db: Database, token: str) -> int | None:
    """Whether the token is valid. Returns user_id or None."""
    r = db.one(
        "SELECT user_id, expires_at, used_at FROM password_resets WHERE token_hash=?",
        (security.sha256_hex(token),))
    if r is None or r["used_at"] is not None:
        return None
    if r["expires_at"] < int(time.time()):
        return None
    return int(r["user_id"])


def reset_password(db: Database, token: str, new_password: str) -> bool:
    """Consumes the token and changes the password. All sessions are deleted."""
    uid = check_token(db, token)
    if uid is None:
        return False
    token_hash = security.sha256_hex(token)
    ts = int(time.time())
    with db.tx() as c:
        # Race protection: mark as used only if it is still unused.
        cur = c.execute(
            "UPDATE password_resets SET used_at=? WHERE token_hash=? AND used_at IS NULL",
            (ts, token_hash))
        if cur.rowcount == 0:
            return False
        c.execute("UPDATE users SET password_hash=? WHERE id=?",
                  (security.hash_password(new_password), uid))
        # A reset may mean the account was compromised: sign out everywhere.
        c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    log.info("password reset user_id=%s", uid)
    return True


def message_text(link: str) -> str:
    return (
        f"{branding.BRAND_NAME} — parolni tiklash\n\n"
        "Parolni tiklash so'rovi keldi. Yangi parol o'rnatish uchun quyidagi "
        "havolaga o'ting:\n"
        f"{link}\n\n"
        "Havola 15 daqiqa amal qiladi va bir marta ishlaydi.\n\n"
        "Agar bu so'rovni siz yubormagan bo'lsangiz, hech narsa qilmang — "
        "parolingiz o'zgarmaydi."
    )
