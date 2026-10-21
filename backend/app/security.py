"""Password, session token and API key helpers. Standard library only."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

KEY_PREFIX = "sk-sf-"

# scrypt: 2^15 * 8 * 128 = 32 MiB of memory, one check takes ~60-100 ms.
_N, _R, _P = 2 ** 15, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, maxmem=64 * 1024 * 1024, dklen=32)
    return "scrypt${}${}${}${}${}".format(_N, _R, _P, base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, dk_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
        got = hashlib.scrypt(password.encode(), salt=salt, n=int(n), r=int(r), p=int(p),
                             maxmem=64 * 1024 * 1024, dklen=len(expected))
        return hmac.compare_digest(got, expected)
    except Exception:
        return False


# Keeps timing identical for non-existent users (so login does not reveal which emails exist).
DUMMY_HASH = hash_password("dummy-password-for-timing")

# Accounts created via Google or Telegram have no password. This marker matches no
# password (`verify_password` cannot parse it and returns False), so any attempt
# to log in with a password is always rejected.
NO_PASSWORD = "!external"


def has_password(stored: str | None) -> bool:
    """Whether the account has a password (accounts created via external login do not)."""
    return bool(stored) and stored.startswith("scrypt$")


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def new_api_key() -> tuple[str, str, str, str]:
    """(full key, displayed prefix, last 4 characters, sha256)."""
    raw = KEY_PREFIX + secrets.token_urlsafe(32)
    return raw, raw[:12], raw[-4:], sha256_hex(raw)


def new_temp_password() -> str:
    # Easy-to-read password without confusable characters (0/O, 1/l/I).
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(14))
