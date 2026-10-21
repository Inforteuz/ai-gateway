"""Sign-in via Google and Telegram: signature verification and account linking.

TWO METHODS, ONE PATH. Both Google and Telegram end up in `link_or_create`.
There:

  - if the email matches, the EXISTING account is linked (no duplicate is created);
  - a new account goes through `abuse.check_signup` and `abuse.signup_bonus`,
    so the limits on many accounts per IP and on bonus farming work exactly
    as for password registration;
  - when registration is closed no new account is created, but an already
    linked account can still sign in.

TRUST BOUNDARY. Data coming from the browser is never trusted as is:
  - Telegram: the fields returned by the widget are verified with HMAC-SHA256
    keyed by the bot token, and a stale `auth_date` is rejected.
  - Google: the browser only delivers a one-time `code`; the `id_token`
    itself is fetched directly from Google's token endpoint over TLS. So the
    RSA signature does not need re-checking (OpenID Connect 3.1.3.7), but
    `iss`, `aud`, `exp` and `nonce` are still verified.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any
from urllib.parse import urlencode

from . import abuse
from .common import ApiError, create_user

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}
GOOGLE_CALLBACK_PATH = "/portal/auth/google/callback"

TELEGRAM_API = "https://api.telegram.org"
# Telegram provides no email, but `users.email` must not be empty.
# `.invalid` is a domain that never exists (RFC 2606), so no mail can ever
# be sent to this address.
TELEGRAM_EMAIL_DOMAIN = "telegram.invalid"
# Widget data is considered stale after this many seconds.
TELEGRAM_MAX_AGE = 300

# OAuth flow state is kept in this cookie (lives 10 minutes).
OAUTH_COOKIE = "sf_oauth"
OAUTH_TTL = 600


# --- settings -----------------------------------------------------------------

def site_base(db, request=None) -> str:
    """The site's external base URL (without a trailing /)."""
    configured = (db.get_settings().get("site_base_url") or "").strip()
    if configured:
        return configured.rstrip("/")
    return str(request.base_url).rstrip("/") if request is not None else ""


def google_keys(db) -> tuple[str, str]:
    s = db.get_settings()
    return (s.get("google_client_id") or "").strip(), (s.get("google_client_secret") or "").strip()


def google_ready(db) -> bool:
    client_id, secret = google_keys(db)
    return bool(client_id and secret)


def telegram_token(db) -> str:
    """Bot token for the login widget (may be the same bot as admin alerts)."""
    s = db.get_settings()
    if s.get("telegram_login_enabled") != "1":
        return ""
    # A dedicated login bot if configured, otherwise the alert bot.
    # The signature MUST be checked with the token of the exact bot embedded
    # in the widget, otherwise even a valid signature is rejected.
    return ((s.get("telegram_login_token") or "").strip()
            or (s.get("alert_telegram_token") or "").strip())


# --- Google -------------------------------------------------------------------

def google_start(db, request) -> tuple[str, str]:
    """Returns (URL to redirect to Google, state to store in the cookie).

    PKCE (S256) is used: even if the `code` is intercepted it cannot be
    exchanged without the `code_verifier`.
    """
    client_id, secret = google_keys(db)
    if not (client_id and secret):
        raise ApiError(503, "google_not_configured",
                       "Google orqali kirish hozircha sozlanmagan.", "invalid_request_error")
    state, verifier, nonce = (secrets.token_urlsafe(24) for _ in range(3))
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    query = urlencode({
        "client_id": client_id,
        "redirect_uri": site_base(db, request) + GOOGLE_CALLBACK_PATH,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
    })
    return f"{GOOGLE_AUTH_URL}?{query}", "|".join((state, verifier, nonce))


def google_exchange_form(db, request, code: str, verifier: str) -> dict[str, str]:
    client_id, secret = google_keys(db)
    return {
        "code": code,
        "client_id": client_id,
        "client_secret": secret,
        "redirect_uri": site_base(db, request) + GOOGLE_CALLBACK_PATH,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    }


def _b64url(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def id_token_claims(raw: str, *, client_id: str, nonce: str) -> dict[str, Any]:
    """Claims inside the `id_token`. Raises ApiError if invalid.

    The signature is not checked: the token was fetched from Google itself
    over TLS (see the module docstring). The remaining claims are verified.
    """
    try:
        _, payload, _ = raw.split(".")
        claims = json.loads(_b64url(payload))
    except Exception:
        raise ApiError(400, "google_bad_token", "Google javobini o'qib bo'lmadi.", "authentication_error")
    if claims.get("iss") not in GOOGLE_ISSUERS:
        raise ApiError(400, "google_bad_token", "Google javobi ishonchsiz.", "authentication_error")
    if claims.get("aud") != client_id:
        raise ApiError(400, "google_bad_token", "Google javobi boshqa ilovaga tegishli.", "authentication_error")
    if int(claims.get("exp") or 0) < time.time():
        raise ApiError(400, "google_expired", "Google javobining muddati o'tgan. Qaytadan urinib ko'ring.",
                       "authentication_error")
    if nonce and claims.get("nonce") != nonce:
        raise ApiError(400, "google_bad_token", "Google javobi bu urinishga tegishli emas.", "authentication_error")
    email = (claims.get("email") or "").strip().lower()
    if not email or not claims.get("email_verified"):
        raise ApiError(400, "google_no_email",
                       "Google akkauntingizda tasdiqlangan email yo'q. Oddiy usulda ro'yxatdan o'ting.",
                       "authentication_error")
    if not claims.get("sub"):
        raise ApiError(400, "google_bad_token", "Google javobi to'liq emas.", "authentication_error")
    return claims


# --- Telegram -----------------------------------------------------------------

def verify_telegram(data: dict[str, str], token: str, *, max_age: int = TELEGRAM_MAX_AGE) -> dict[str, str]:
    """Verifies widget data with the bot token. Raises ApiError if invalid.

    Per the Telegram docs: secret = sha256(bot_token), then the remaining
    fields as "key=value", sorted alphabetically and joined with \\n, are
    signed with HMAC-SHA256.
    """
    if not token:
        raise ApiError(503, "telegram_not_configured",
                       "Telegram orqali kirish hozircha sozlanmagan.", "invalid_request_error")
    got = (data.get("hash") or "").strip()
    fields = {k: v for k, v in data.items() if k != "hash" and v is not None}
    if not got or "id" not in fields or "auth_date" not in fields:
        raise ApiError(400, "telegram_bad_data", "Telegram ma'lumoti to'liq emas.", "authentication_error")
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hashlib.sha256(token.encode()).digest()
    want = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, got.lower()):
        # Signature mismatch: the data is not from Telegram or was tampered with.
        raise ApiError(401, "telegram_bad_signature",
                       "Telegram imzosi tasdiqlanmadi. Qaytadan urinib ko'ring.", "authentication_error")
    try:
        auth_date = int(fields["auth_date"])
    except (TypeError, ValueError):
        raise ApiError(400, "telegram_bad_data", "Telegram ma'lumoti noto'g'ri.", "authentication_error")
    # Stale (or future-dated) data could be replayed, so reject it.
    if abs(time.time() - auth_date) > max_age:
        raise ApiError(401, "telegram_expired",
                       "Telegram tasdiqlashining muddati o'tgan. Tugmani qaytadan bosing.", "authentication_error")
    return fields


def telegram_name(fields: dict[str, str]) -> str:
    parts = [(fields.get("first_name") or "").strip(), (fields.get("last_name") or "").strip()]
    name = " ".join(p for p in parts if p).strip()
    return name or (fields.get("username") or "").strip()


def telegram_email(telegram_id: str) -> str:
    return f"tg{telegram_id}@{TELEGRAM_EMAIL_DOMAIN}"


async def telegram_bot_username(state) -> str:
    """Bot username for the widget. If not in settings, it is discovered via getMe.

    The result is saved to `settings`, so Telegram is queried only once. On
    error there is no retry for 10 minutes, so that opening the login page
    does not trigger an external request every time.
    """
    db = state.db
    token = telegram_token(db)
    if not token:
        return ""
    name = (db.get_settings().get("telegram_login_bot") or "").strip().lstrip("@")
    if name:
        return name
    last = getattr(state, "tg_bot_probe", 0.0)
    if time.time() - last < 600:
        return ""
    state.tg_bot_probe = time.time()
    try:
        r = await state.notify_http.get(f"{TELEGRAM_API}/bot{token}/getMe")
        name = ((r.json().get("result") or {}).get("username") or "").strip()
    except Exception:
        return ""
    if name:
        db.set_setting("telegram_login_bot", name)
    return name


# --- account linking ----------------------------------------------------------

PROVIDER_COLUMN = {"google": "google_sub", "telegram": "telegram_id"}


def link_or_create(db, *, provider: str, external_id: str, email: str, name: str, ip: str,
                   referral_code: str | None = None, device: str | None = None) -> tuple[int, int, bool]:
    """Links an external identity to an account or creates a new account.

    `referral_code` is used only when a NEW account is created; an existing account cannot be re-attached later.

    Returns: (user id, bonus in micro-sum, whether the account is new).
    """
    column = PROVIDER_COLUMN[provider]
    email = (email or "").strip().lower()
    row = db.one(f"SELECT * FROM users WHERE {column}=?", (external_id,))

    # Same email: link to the existing account instead of creating a new one.
    if row is None and email:
        row = db.one("SELECT * FROM users WHERE email=?", (email,))
        if row is not None and (row[column] or "") not in ("", external_id):
            raise ApiError(409, "already_linked",
                           "Bu emailga allaqachon boshqa hisob ulangan. Parol bilan kiring.",
                           "authentication_error")

    if row is not None:
        if row["status"] != "active":
            raise ApiError(403, "account_suspended",
                           "Hisobingiz to'xtatilgan. Administrator bilan bog'laning.", "permission_error")
        updates, args = [], []
        if not row[column]:
            updates.append(f"{column}=?")
            args.append(external_id)
        if name and not (row["name"] or "").strip():
            updates.append("name=?")
            args.append(name[:80])
        if updates:
            with db.tx() as c:
                c.execute(f"UPDATE users SET {', '.join(updates)} WHERE id=?", (*args, row["id"]))
        return row["id"], 0, False

    # New account. All password-registration limits apply here as well,
    # otherwise this path would bypass the protection.
    settings = db.get_settings()
    if settings.get("registration_open") != "1":
        raise ApiError(403, "registration_closed",
                       "Ro'yxatdan o'tish hozircha yopiq. Administratordan taklif so'rang.", "permission_error")
    abuse.check_signup(db, ip)
    bonus, _ = abuse.signup_bonus(db, ip, int(settings.get("signup_bonus_micro", "0")))
    uid = create_user(db, email=email or telegram_email(external_id), name=name, password=None,
                      bonus_micro=bonus, signup_ip=ip,
                      google_sub=external_id if provider == "google" else None,
                      telegram_id=external_id if provider == "telegram" else None,
                      referral_code=referral_code, signup_method=provider, device=device)
    return uid, bonus, True


__all__ = [
    "GOOGLE_CALLBACK_PATH", "OAUTH_COOKIE", "OAUTH_TTL", "TELEGRAM_MAX_AGE",
    "google_exchange_form", "google_keys", "google_ready", "google_start", "id_token_claims",
    "link_or_create", "site_base", "telegram_bot_username", "telegram_email",
    "telegram_name", "telegram_token", "verify_telegram",
]
