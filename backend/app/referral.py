"""Referral program: the referrer earns a percentage of every deposit made by the referred user.

RULES:
  * Every user has a unique code (`users.ref_code`) and a link `<site>/login?ref=CODE`.
  * Linking happens ONLY when a new account is created (password, Google, Telegram: all three
    go through `common.create_user`) and is written in that same transaction. In the `referrals`
    table referee_id is the primary key and the app role has no UPDATE/DELETE: an account cannot
    be re-attached to someone else later.
  * Rewards come only from a REAL deposit: `billing.is_deposit` (ledger.kind='topup', amount > 0,
    the same definition as `billing.has_deposit`). Bonuses, admin adjustments, refunds and the
    referral reward itself are not deposits.
  * The reward is written in the SAME transaction as the deposit (called from `billing.credit_tx`):
    either both or neither. One row per deposit: `referral_rewards.deposit_ledger_id` is UNIQUE,
    and a repeated call returns the existing row.
  * ROUNDING: reward = floor(deposit × percent_bp / 10 000) in micro-sum, i.e. DOWN, to 1 micro-sum
    (1 sum = 1 000 000 micro). E.g. 100 000 sum × 5% = exactly 5 000 sum; a remainder below
    0.000001 sum is dropped, so the platform never pays more than the percentage.
  * The percentage (`referral_percent`, up to 2 decimals: "5", "2.5") and on/off (`referral_enabled`)
    are admin settings. When off there are no new links and no rewards (the program stops entirely).
  * The admin can pause rewards per user (`users.referral_paused`). A paused or suspended referrer
    is not paid and the row is recorded with `status='skipped'`; re-enabling later does not pay
    retroactively for past deposits.
  * There is NO deposit refund/chargeback flow in the code: deposits are written only through admin
    approval or manual entry. Hence there is no automatic reward clawback either; if needed the
    admin reverses it with a (negative) "adjustment".
"""
from __future__ import annotations

import re
import secrets
from decimal import Decimal, InvalidOperation

from .db import Database, now

# No confusable characters (no 0/O, 1/I/L). 32^8 ≈ 10^12 combinations.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LEN = 8
CODE_RE = re.compile(r"^[A-Z0-9]{4,16}$")
DEVICE_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

REF_COOKIE = "sf_ref"     # the frontend writes `?ref=` into this cookie (and localStorage)
DEVICE_COOKIE = "sf_dev"  # device marker created by the frontend (1 year)

MAX_PERCENT_BP = 5000     # 50%: guard against input mistakes


# --- settings ----------------------------------------------------------------------------------------------------
def parse_bp(value) -> int | None:
    """'5' -> 500, '2,5' -> 250. Invalid value -> None."""
    try:
        return int((Decimal(str(value).strip().replace(",", ".")) * 100).to_integral_value())
    except (InvalidOperation, ValueError):
        return None


def percent_bp(value) -> int:
    """The configured percentage, clamped to 0..MAX_PERCENT_BP. Invalid value -> 0 (no reward, not an error)."""
    bp = parse_bp(value)
    return 0 if bp is None else max(0, min(MAX_PERCENT_BP, bp))


def bp_text(bp: int) -> str:
    """500 -> '5', 250 -> '2.5'."""
    return f"{Decimal(bp) / 100:f}".rstrip("0").rstrip(".") if bp % 100 else str(bp // 100)


def reward_for(amount: int, bp: int) -> int:
    """Reward rounded down (micro-sum). Integers only; no floats."""
    if amount <= 0 or bp <= 0:
        return 0
    return amount * bp // 10_000


def program(settings: dict) -> tuple[bool, int]:
    """(enabled, percent_bp)."""
    return settings.get("referral_enabled", "1") == "1", percent_bp(settings.get("referral_percent", "5"))


def _settings_tx(c) -> dict:
    rows = c.execute("SELECT key, value FROM settings WHERE key IN ('referral_enabled', 'referral_percent')").fetchall()
    return {r["key"]: r["value"] for r in rows}


# --- code ----------------------------------------------------------------------------------------------------------
def new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))


def normalize_code(raw) -> str | None:
    code = (raw or "").strip().upper() if isinstance(raw, str) else ""
    return code if CODE_RE.match(code) else None


def normalize_device(raw) -> str | None:
    dev = (raw or "").strip() if isinstance(raw, str) else ""
    return dev if DEVICE_RE.match(dev) else None


def _free_code(c) -> str:
    for _ in range(20):
        code = new_code()
        if c.execute("SELECT 1 FROM users WHERE ref_code=?", (code,)).fetchone() is None:
            return code
    raise RuntimeError("could not generate a referral code")  # practically impossible with 10^12 combinations


def ensure_code(db: Database, user_id: int) -> str:
    """The user's code; created if missing (accounts opened before the referral program)."""
    row = db.one("SELECT ref_code FROM users WHERE id=?", (user_id,))
    if row and row["ref_code"]:
        return row["ref_code"]
    with db.tx() as c:
        row = c.execute("SELECT ref_code FROM users WHERE id=?", (user_id,)).fetchone()
        if row["ref_code"]:
            return row["ref_code"]
        code = _free_code(c)
        c.execute("UPDATE users SET ref_code=? WHERE id=? AND ref_code IS NULL", (code, user_id))
        return code


# --- linking (at signup only) --------------------------------------------------------------------------------------
def on_signup_tx(c, *, user_id: int, code: str | None, method: str, ip: str | None, device: str | None) -> int | None:
    """Assigns a code to the new account and (if the code is valid) links it to the referrer.

    Called inside the `create_user` transaction. Returns the referrer id or None.
    An invalid/stale code does NOT block registration; there is simply no link.
    """
    c.execute("UPDATE users SET ref_code=?, signup_device=? WHERE id=?", (_free_code(c), device, user_id))
    code = normalize_code(code)
    if not code:
        return None
    enabled, _ = program(_settings_tx(c))
    if not enabled:
        return None
    ref = c.execute("SELECT id, signup_ip, signup_device FROM users WHERE ref_code=?", (code,)).fetchone()
    if ref is None or ref["id"] == user_id:  # cannot refer oneself
        return None
    flags = []
    known_ips = {ref["signup_ip"]} | {r["ip"] for r in c.execute(
        "SELECT DISTINCT ip FROM sessions WHERE user_id=?", (ref["id"],)).fetchall()}
    if ip and ip != "-" and ip in known_ips:
        flags.append("same_ip")
    if device and device == ref["signup_device"]:
        flags.append("same_device")
    c.execute("INSERT INTO referrals(referee_id, referrer_id, method, flags, created_at) VALUES (?,?,?,?,?)"
              " ON CONFLICT (referee_id) DO NOTHING",
              (user_id, ref["id"], method, ",".join(flags), now()))
    return ref["id"]


# --- reward --------------------------------------------------------------------------------------------------------
def on_deposit_tx(c, *, user_id: int, deposit_ledger_id: int, amount: int) -> int:
    """Pays the referrer a reward for a deposit. Called inside the `billing.credit_tx` (deposit) transaction.

    Idempotent: does nothing if a row for this deposit exists. Returns the amount paid (0 = not paid).
    """
    if amount <= 0:
        return 0
    link = c.execute("SELECT referrer_id FROM referrals WHERE referee_id=?", (user_id,)).fetchone()
    if link is None:
        return 0
    if c.execute("SELECT 1 FROM referral_rewards WHERE deposit_ledger_id=?", (deposit_ledger_id,)).fetchone():
        return 0
    enabled, bp = program(_settings_tx(c))
    reward = reward_for(amount, bp)
    if not enabled or reward <= 0:
        return 0
    referrer_id = link["referrer_id"]
    ref = c.execute("SELECT status, referral_paused FROM users WHERE id=?", (referrer_id,)).fetchone()
    reason = "paused" if ref["referral_paused"] else ("suspended" if ref["status"] != "active" else "")
    ts = now()
    reward_ledger_id = None
    if not reason:
        c.execute("INSERT INTO wallets(user_id, balance, held, updated_at) VALUES (?,0,0,?)"
                  " ON CONFLICT (user_id) DO NOTHING", (referrer_id, ts))
        balance = c.execute("UPDATE wallets SET balance = balance + ?, updated_at=? WHERE user_id=? RETURNING balance",
                            (reward, ts, referrer_id)).fetchone()["balance"]
        reward_ledger_id = c.execute(
            "INSERT INTO ledger(user_id, kind, amount, balance_after, note, created_at) VALUES (?, 'referral', ?, ?, ?, ?)",
            (referrer_id, reward, balance, f"Taklif mukofoti: {bp_text(bp)}% — taklif qilingan foydalanuvchi depoziti",
             ts)).lastrowid
    c.execute("""INSERT INTO referral_rewards(referrer_id, referee_id, deposit_ledger_id, reward_ledger_id,
                     deposit_amount, percent_bp, amount, status, reason, created_at)
                 VALUES (?,?,?,?,?,?,?,?,?,?)""",
              (referrer_id, user_id, deposit_ledger_id, reward_ledger_id, amount, bp, reward,
               "skipped" if reason else "paid", reason, ts))
    return 0 if reason else reward


# --- presentation --------------------------------------------------------------------------------------------------
def mask_email(email: str) -> str:
    """The referrer never sees the referee's full email: al***@gmail.com."""
    email = email or ""
    if email.endswith("@telegram.invalid"):
        return "Telegram foydalanuvchisi"
    name, _, domain = email.partition("@")
    return f"{name[:2]}***@{domain}" if domain else "***"
