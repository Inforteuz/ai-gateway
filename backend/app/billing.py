"""Billing core: reserve -> request to the model -> charge for the actual tokens.

Rules:
  * Money is an integer (micro-UZS). No floats.
  * Every change happens in one database transaction: wallet + ledger + request log.
  * Before a request starts, the worst-case amount is reserved in `held`. If the balance is short, the request is rejected.
  * If the model does not answer (error, disconnect) nothing is charged and the reserved amount is released.
"""
from __future__ import annotations

import json

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import referral
from .db import Database, now
from .failures import classify

MICRO = 1_000_000  # 1 UZS
TASHKENT = timezone(timedelta(hours=5))


class BillingError(Exception):
    def __init__(self, code: str, message: str, status: int = 402):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def cost_for(tokens: int, price_per_1m: int) -> int:
    """Rounds up: the client never pays less than the real cost; precise to 1 micro-UZS."""
    if tokens <= 0 or price_per_1m <= 0:
        return 0
    return -(-tokens * price_per_1m // MICRO)


def month_start_ts(ts: int | None = None) -> int:
    dt = datetime.fromtimestamp(ts if ts is not None else now(), TASHKENT)
    return int(dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())


def day_start_ts(ts: int | None = None) -> int:
    """Start of the current day (00:00) in Tashkent time."""
    dt = datetime.fromtimestamp(ts if ts is not None else now(), TASHKENT)
    return int(dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def next_day_ts(ts: int | None = None) -> int:
    """Next Tashkent midnight. UTC+5 has no DST, so a day is always 86400 seconds."""
    return day_start_ts(ts) + 86400


# --- daily token limit on free models ------------------------------------------------------------------------
# A user who has never made a deposit cannot use more than `free_daily_token_limit` tokens per day on free
# models. Deposit = `ledger.kind='topup'` with a positive amount: written when the user's top-up request is
# approved or when an admin enters a "Top-up" manually. The signup bonus ('bonus'), adjustments ('adjustment')
# and refunds ('refund') do not count as deposits.

def free_limit_models(settings: dict) -> list[str]:
    return [m.strip() for m in (settings.get("free_daily_limit_models") or "").split(",") if m.strip()]


def is_deposit(kind: str, amount: int) -> bool:
    """A real deposit (money paid in). MUST match the SQL condition in `has_deposit`."""
    return kind == "topup" and amount > 0


def has_deposit(db: Database, user_id: int) -> bool:
    return db.one("SELECT 1 FROM ledger WHERE user_id=? AND kind='topup' AND amount>0 LIMIT 1", (user_id,)) is not None


def free_tokens_used(db: Database, user_id: int, models: list[str], ts: int | None = None) -> int:
    """Tokens of today's (Tashkent day) successful requests. `idx_usage_user_model_time` reads only this day."""
    if not models:
        return 0
    marks = ",".join("?" * len(models))
    return db.scalar(
        f"""SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) FROM usage_logs
            WHERE user_id=? AND model IN ({marks}) AND created_at>=? AND status_code=200""",
        (user_id, *models, day_start_ts(ts)),
    )


@dataclass
class FreeQuota:
    models: list[str]
    limit: int
    used: int
    resets_at: int
    requested: int = 0      # worst-case tokens of the refused request (0 = plain "quota used up")

    @property
    def exhausted(self) -> bool:
        # `>=`: the request that crosses the limit is let through; only the following ones are stopped.
        return self.used >= self.limit

    def out(self) -> dict:
        return {"models": self.models, "limit": self.limit, "used": self.used,
                "remaining": max(0, self.limit - self.used), "resets_at": self.resets_at}


def free_quota(db: Database, *, user_id: int, settings: dict, ts: int | None = None) -> FreeQuota | None:
    """The user's free quota for today. None = no limit (limit is 0 or the user has deposited)."""
    limit = int(settings.get("free_daily_token_limit") or 0)
    models = free_limit_models(settings)
    if limit <= 0 or not models or has_deposit(db, user_id):
        return None
    ts = ts if ts is not None else now()
    return FreeQuota(models, limit, free_tokens_used(db, user_id, models, ts), next_day_ts(ts))


def ensure_wallet(c, user_id: int) -> None:
    c.execute("INSERT INTO wallets(user_id, balance, held, updated_at) VALUES (?,0,0,?) ON CONFLICT (user_id) DO NOTHING", (user_id, now()))


class FreeQuotaExceeded(BillingError):
    """The free daily quota (completed + pending requests) is used up."""

    def __init__(self, quota: "FreeQuota"):
        super().__init__("free_daily_limit", "free daily quota exhausted", 429)
        self.quota = quota


# --- in-flight reservations ---------------------------------------------------------------------------------------
# Every admitted request has one `reservations` row until it is settled or released. All checks
# and the insert run inside `db.tx()` (global write lock), so parallel requests — also from other
# processes — see each other. `wallets.held` stays a cached SUM(amount) per user.

LEASE_STALE_SECONDS = 75     # an owner without a heartbeat for this long is considered dead


def register_instance(db: Database, owner: str) -> None:
    ts = now()
    with db.tx() as c:
        c.execute("INSERT INTO app_instances(owner, started_at, last_seen) VALUES (?,?,?) "
                  "ON CONFLICT (owner) DO UPDATE SET last_seen=excluded.last_seen", (owner, ts, ts))


def unregister_instance(db: Database, owner: str) -> None:
    """Graceful shutdown: this owner's leftover reservations become orphans and are released at once."""
    with db.tx() as c:
        c.execute("DELETE FROM app_instances WHERE owner=?", (owner,))
    sweep_reservations(db)


def sweep_reservations(db: Database, owner: str | None = None) -> int:
    """Heartbeat (when `owner` is given) + release of orphaned/expired reservations.

    A reservation is dropped when it expired or its owner has not beaten for LEASE_STALE_SECONDS.
    `held` is then recomputed from the remaining rows for every wallet that is out of sync, which
    also clears amounts left by versions before v5 (they had no rows). Returns rows released.
    """
    ts = now()
    with db.tx() as c:
        if owner:
            c.execute("INSERT INTO app_instances(owner, started_at, last_seen) VALUES (?,?,?) "
                      "ON CONFLICT (owner) DO UPDATE SET last_seen=excluded.last_seen", (owner, ts, ts))
        released = c.execute(
            """DELETE FROM reservations r WHERE r.expires_at < ? OR NOT EXISTS (
                   SELECT 1 FROM app_instances i WHERE i.owner = r.owner AND i.last_seen >= ?)""",
            (ts, ts - LEASE_STALE_SECONDS)).rowcount
        c.execute(
            """UPDATE wallets w SET held = COALESCE(p.total, 0), updated_at = ?
               FROM (SELECT w2.user_id, (SELECT SUM(amount) FROM reservations r WHERE r.user_id = w2.user_id) AS total
                     FROM wallets w2) p
               WHERE p.user_id = w.user_id AND w.held <> COALESCE(p.total, 0)""", (ts,))
        c.execute("DELETE FROM app_instances WHERE last_seen < ?", (ts - 86400,))
    return released


def reserve(db: Database, *, request_id: str, user_id: int, key_id: int, model: str, amount: int, tokens: int,
            free: bool, monthly_limit: int | None, owner: str, ttl: int,
            max_concurrent: int | None = None, quota_limit: int = 0, quota_models: list[str] | None = None) -> int:
    """Admits a request atomically and records its reservation. Returns the money actually held (0 for a free key).

    Checked in one transaction, counting other in-flight requests:
      * per-key concurrency (`max_concurrent`) — across all app processes;
      * the free daily quota: completed + pending tokens of `quota_models` (when `quota_limit` > 0);
      * the wallet: balance - held >= amount;
      * the key's monthly limit: spent this month + pending holds of the key + amount.
    """
    money = 0 if (free or amount <= 0) else amount
    ts = now()
    with db.tx() as c:
        if max_concurrent is not None:
            active = c.execute("SELECT COUNT(*) FROM reservations WHERE key_id=?", (key_id,)).fetchone()[0]
            if active >= max_concurrent:
                raise BillingError("too_many_concurrent",
                                   f"Bir vaqtda {max_concurrent} tadan ortiq so'rov yuborib bo'lmaydi.", 429)
        if quota_limit > 0 and quota_models and model in quota_models:
            used = free_tokens_used(db, user_id, quota_models, ts)
            marks = ",".join("?" * len(quota_models))
            pending = c.execute(f"SELECT COALESCE(SUM(tokens), 0) FROM reservations WHERE user_id=? AND model IN ({marks})",
                                (user_id, *quota_models)).fetchone()[0]
            # Strict: admitted only if its worst case (prompt estimate + max_tokens) still fits into what is
            # left after completed and in-flight requests of the same user (all keys share one quota). The
            # request's max_tokens is never changed silently; the error says how much is left. Real usage can
            # still exceed the estimate slightly when the provider counts more prompt tokens than estimated.
            if used + pending + max(0, tokens) > quota_limit:
                raise FreeQuotaExceeded(FreeQuota(quota_models, quota_limit, used + pending, next_day_ts(ts),
                                                  requested=max(0, tokens)))
        if money:
            w = c.execute("SELECT balance, held FROM wallets WHERE user_id=?", (user_id,)).fetchone()
            if w is None:
                raise BillingError("insufficient_balance", "Hisobingizda mablag' yo'q. Hisobni to'ldiring.")
            if w["balance"] - w["held"] < money:
                raise BillingError(
                    "insufficient_balance",
                    "Hisobingizda mablag' yetarli emas. Hisobni to'ldiring yoki so'rovdagi max_tokens ni kamaytiring.",
                )
            if monthly_limit is not None:
                spent = c.execute("SELECT COALESCE(SUM(cost),0) FROM usage_logs WHERE key_id=? AND created_at>=?",
                                  (key_id, month_start_ts(ts))).fetchone()[0]
                pending = c.execute("SELECT COALESCE(SUM(amount),0) FROM reservations WHERE key_id=?",
                                    (key_id,)).fetchone()[0]
                if spent + pending + money > monthly_limit:
                    raise BillingError("key_limit_reached", "Bu kalitning oylik limiti tugagan.", 429)
            c.execute("UPDATE wallets SET held = held + ?, updated_at=? WHERE user_id=?", (money, ts, user_id))
        c.execute("""INSERT INTO reservations(request_id, user_id, key_id, model, amount, tokens, owner, created_at, expires_at)
                     VALUES (?,?,?,?,?,?,?,?,?)""",
                  (request_id, user_id, key_id, model, money, max(0, tokens), owner, ts, ts + ttl))
    return money


def _drop_reservation(c, request_id: str, user_id: int, ts: int) -> int:
    """Deletes the reservation and takes its amount out of `held`. Idempotent: 0 when already gone
    (settled, or released by the sweeper, which then already recomputed `held`)."""
    row = c.execute("DELETE FROM reservations WHERE request_id=? RETURNING amount", (request_id,)).fetchone()
    amount = row[0] if row else 0
    if amount:
        c.execute("UPDATE wallets SET held = GREATEST(0, held - ?), updated_at=? WHERE user_id=?", (amount, ts, user_id))
    return amount


def release(db: Database, *, request_id: str, user_id: int) -> None:
    """Drops a reservation without charging (request never reached settle). Safe to call twice."""
    with db.tx() as c:
        _drop_reservation(c, request_id, user_id, now())


@dataclass
class Settled:
    usage_id: int
    cost: int
    balance_after: int


def settle(
    db: Database,
    *,
    request_id: str,
    user_id: int,
    key_id: int | None,
    model: str,
    endpoint: str,
    reserved: int,
    free: bool,
    price_in: int,
    price_out: int,
    prompt_tokens: int,
    completion_tokens: int,
    status_code: int,
    stream: bool,
    estimated: bool = False,
    latency_ms: int | None = None,
    ttfb_ms: int | None = None,
    error: str | None = None,
    ip: str | None = None,
    reasoning: str = "",
    refined: str | None = None,
    refine_ms: int | None = None,
    attribution: dict | None = None,
) -> Settled:
    """Finalises a request: logs it, charges the wallet, releases its reservation. `reserved` is kept for
    callers' bookkeeping; the amount released is the one stored in the reservation row (so a request whose
    reservation was swept is not subtracted from `held` twice). A second settle of the same request_id fails
    on the usage_logs unique constraint and rolls back entirely."""
    # Only successful (2xx) responses are charged. Even with `error` set (e.g. the client dropped the stream),
    # tokens the model has already produced are paid for.
    ok = 200 <= status_code < 300
    cost = 0 if (free or not ok) else cost_for(prompt_tokens, price_in) + cost_for(completion_tokens, price_out)
    ts = now()
    with db.tx() as c:
        cur = c.execute(
            """INSERT INTO usage_logs(request_id, user_id, key_id, model, endpoint, prompt_tokens, completion_tokens,
                   cost, status_code, stream, estimated, latency_ms, ttfb_ms, error, category, ip, reasoning, refined, refine_ms, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (request_id, user_id, key_id, model, endpoint, prompt_tokens if ok else 0, completion_tokens if ok else 0,
             cost, status_code, int(stream), int(estimated), latency_ms, ttfb_ms, error, classify(status_code, error), ip, reasoning or None, refined, refine_ms, ts),
        )
        usage_id = cur.lastrowid
        if attribution:
            c.execute("UPDATE usage_logs SET attribution=? WHERE id=?",
                      (json.dumps(attribution, ensure_ascii=False), usage_id))
        ensure_wallet(c, user_id)
        _drop_reservation(c, request_id, user_id, ts)
        w = c.execute("SELECT balance FROM wallets WHERE user_id=?", (user_id,)).fetchone()
        # The hold covers the worst case for the estimated prompt; if the provider reports more prompt tokens
        # than estimated the balance may go slightly negative. Such a debt blocks the next paid request
        # (balance - held < hold) until the user tops up.
        balance = w["balance"] - cost
        c.execute("UPDATE wallets SET balance=?, updated_at=? WHERE user_id=?", (balance, ts, user_id))
        if cost:
            c.execute(
                """INSERT INTO ledger(user_id, kind, amount, balance_after, usage_id, note, created_at)
                   VALUES (?, 'usage', ?, ?, ?, ?, ?)""",
                (user_id, -cost, balance, usage_id, f"{model}: {prompt_tokens} kirish + {completion_tokens} chiqish", ts),
            )
        if key_id is not None:
            c.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (ts, key_id))
    return Settled(usage_id, cost, balance)


def credit_tx(c, *, user_id: int, amount: int, kind: str, note: str = "", admin_id: int | None = None) -> int:
    """In-transaction variant of `credit` (the caller must already hold a write transaction)."""
    if kind not in {"topup", "adjustment", "refund", "bonus"}:
        raise ValueError("invalid kind")
    if amount == 0:
        raise BillingError("invalid_amount", "Summa nolga teng bo'lmasligi kerak.", 400)
    ts = now()
    ensure_wallet(c, user_id)
    w = c.execute("SELECT balance FROM wallets WHERE user_id=?", (user_id,)).fetchone()
    balance = w["balance"] + amount
    c.execute("UPDATE wallets SET balance=?, updated_at=? WHERE user_id=?", (balance, ts, user_id))
    ledger_id = c.execute(
        """INSERT INTO ledger(user_id, kind, amount, balance_after, note, admin_id, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (user_id, kind, amount, balance, note, admin_id, ts),
    ).lastrowid
    if is_deposit(kind, amount):
        # Referrer reward — in the same transaction (together with the deposit or not at all).
        referral.on_deposit_tx(c, user_id=user_id, deposit_ledger_id=ledger_id, amount=amount)
    return balance


def credit(db: Database, *, user_id: int, amount: int, kind: str, note: str = "", admin_id: int | None = None) -> int:
    """Manual credit/debit (top-up, adjustment, bonus). Returns the new balance."""
    with db.tx() as c:
        return credit_tx(c, user_id=user_id, amount=amount, kind=kind, note=note, admin_id=admin_id)


def log_rejection(db: Database, *, request_id: str, user_id: int, key_id: int | None, model: str, endpoint: str,
                  status_code: int, code: str, stream: bool, ip: str | None) -> None:
    """Logs a request that never reached the model (rejected): the balance is unchanged but it shows in reports."""
    with db.tx() as c:
        c.execute(
            """INSERT INTO usage_logs(request_id, user_id, key_id, model, endpoint, cost, status_code, stream,
                   error, category, ip, created_at) VALUES (?,?,?,?,?,0,?,?,?,?,?,?)
                   ON CONFLICT (request_id) DO NOTHING""",
            (request_id, user_id, key_id, model, endpoint, status_code, int(stream), code,
             classify(status_code, code), ip, now()),
        )
