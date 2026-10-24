"""/portal/*: account, keys, usage, top-up requests, public pricing and status for users."""
from __future__ import annotations

import time
from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from pydantic import BaseModel, Field

from . import security
from .billing import MICRO, free_quota, month_start_ts, TASHKENT
from . import catalog, monitor, topup_telegram
from .common import ApiError, csrf_guard, get_db, require_user
from .failures import CATEGORY_LABELS, error_label
from .db import Database, now

router = APIRouter(prefix="/portal", dependencies=[Depends(csrf_guard)], tags=["account"])

MAX_ACTIVE_KEYS = 20


def page_args(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(limit, 100)), max(0, offset)


# --- public data ---------------------------------------------------------------------------------------------
# When old model IDs were migrated to new names, their measurement history
# stayed under the old names. Map them to the new name so the speed figure is
# real; otherwise the page shows "no data".
_LEGACY_NAMES: dict[str, tuple[str, ...]] = {}


# Speed is computed over this many most recent SUCCESSFUL requests.
# A request count, not a time window: once the system is fixed, new
# measurements quickly push out the old ones and the figure converges to
# reality. With a time window an old faulty period lingered for a long time.
SPEED_SAMPLE_SIZE = 20
# With fewer measurements than this no number is shown: a figure derived
# from 1-2 requests is misleading.
MIN_SPEED_SAMPLES = 5


def _real_speed(db: Database) -> dict[str, dict]:
    """REAL speed per model: the median of the most recent requests.

    Why the median and not the mean: a single long timeout (e.g. 116 seconds)
    inflates the mean several times and even a fast model looks slow. The
    median is robust to such outliers, so it better answers "how long does it
    usually take".

    No estimated numbers are used: without enough measurements the field is
    not returned and the page shows nothing.
    """
    out: dict[str, dict] = {}
    # EVERY model in the database is computed (not only those in _LEGACY_NAMES);
    # legacy names are counted under the new one.
    groups = {r["id"]: (r["id"], *_LEGACY_NAMES.get(r["id"], ())) for r in db.all("SELECT id FROM models")}
    for new, olds in _LEGACY_NAMES.items():
        groups.setdefault(new, (new, *olds))
    for new, names in groups.items():
        placeholders = ",".join("?" for _ in names)
        rows = db.all(
            f"SELECT status_code, latency_ms FROM usage_logs"
            f" WHERE model IN ({placeholders}) AND latency_ms IS NOT NULL"
            f" ORDER BY id DESC LIMIT ?", (*names, SPEED_SAMPLE_SIZE * 3))
        recent = list(rows)[: SPEED_SAMPLE_SIZE * 3]
        ok_latencies = [r["latency_ms"] for r in recent if r["status_code"] == 200][:SPEED_SAMPLE_SIZE]
        if len(ok_latencies) < MIN_SPEED_SAMPLES:
            continue
        times = sorted(ok_latencies)
        mid = len(times) // 2
        median = times[mid] if len(times) % 2 else (times[mid - 1] + times[mid]) / 2
        total = len(recent[:SPEED_SAMPLE_SIZE])
        out[new] = {
            "typical_latency_ms": round(median),
            "success_rate": round(len(ok_latencies) / total, 3) if total else None,
            "samples": len(ok_latencies),
        }
    return out


@router.get("/benchmark")
def benchmark_results(db: Database = Depends(get_db)):
    """Model results on our own benchmark.

    The numbers are measured on our tasks, through our gateway, i.e. the
    result a user actually gets. Figures copied from external sources are not
    used: they were measured under different conditions and do not reflect
    our result.
    """
    try:
        # Only models in the catalog are shown. If a delisted model's result
        # lingered, users would be misled by a model that is not on sale
        # (and usually weak).
        rows = db.all(
            "SELECT b.model, b.kategoriya, b.jami, b.togri, b.o_rtacha_ms, b.yangilandi"
            " FROM benchmark_results b"
            " JOIN models m ON m.id = b.model AND m.enabled = 1 AND m.listed = 1"
            " ORDER BY b.model, b.kategoriya")
    except Exception:
        return {"items": [], "categories": [], "updated_at": None}

    models: dict[str, dict] = {}
    categories: list[str] = []
    updated = 0
    for r in rows:
        m = models.setdefault(r["model"], {"id": r["model"], "scores": {},
                                           "total": 0, "correct": 0})
        ratio = round(r["togri"] / r["jami"], 3) if r["jami"] else 0
        m["scores"][r["kategoriya"]] = {
            "correct": r["togri"], "total": r["jami"], "ratio": ratio,
            "avg_ms": r["o_rtacha_ms"],
        }
        m["total"] += r["jami"]
        m["correct"] += r["togri"]
        if r["kategoriya"] not in categories:
            categories.append(r["kategoriya"])
        updated = max(updated, r["yangilandi"] or 0)

    # Attach the visible model names
    names = {x["id"]: x["display_name"]
             for x in db.all("SELECT id, display_name FROM models")}
    items = []
    for m in models.values():
        m["display_name"] = names.get(m["id"], m["id"])
        m["ratio"] = round(m["correct"] / m["total"], 3) if m["total"] else 0
        items.append(m)
    items.sort(key=lambda x: -x["ratio"])

    return {"items": items, "categories": sorted(categories),
            "updated_at": updated or None}


@router.get("/models")
def public_models(request: Request, db: Database = Depends(get_db)):
    rows = db.all("SELECT * FROM models WHERE enabled=1 AND listed=1 ORDER BY id")
    s = db.get_settings()
    speeds = _real_speed(db)
    maintenance = frozenset(x.strip() for x in (s.get("maintenance_models") or "").split(",") if x.strip())
    health = catalog.health_map(db, (getattr(request.app.state, "health", None) or {}).get("state"))
    items = []
    for r in rows:
        m = catalog.public_model(r, int(s["max_max_tokens"]), maintenance, health)
        measured = speeds.get(m["id"])
        if measured:
            m["measured"] = measured
        items.append(m)
    return {"free_mode": s.get("free_mode") == "1", "items": items,
            "limits": {"default_rpm": int(s["default_rpm"]), "default_max_tokens": int(s["default_max_tokens"]),
                       "max_max_tokens": int(s["max_max_tokens"]),
                       # Daily limit of the free model for accounts without a deposit (0 = unlimited)
                       "free_daily_tokens": int(s.get("free_daily_token_limit") or 0),
                       "free_daily_models": [x.strip() for x in (s.get("free_daily_limit_models") or "").split(",") if x.strip()]},
            # The landing page reads the referral terms from settings; the number is never hard-coded
            "referral": {"enabled": s.get("referral_enabled") == "1",
                         "percent": float(s.get("referral_percent") or 0)}}


def model_out(r) -> dict:
    return catalog.public_model(r, 0) | {
        "upstream_name": r["upstream_name"], "provider": r["provider"], "refine": bool(r["refine"]),
        "listed": bool(r["listed"]), "default_reasoning": r["default_reasoning"],
        "fallback_price_from": r["fallback_price_from"], "allow_thinking": bool(r["allow_thinking"]),
    }


async def upstream_online(request: Request) -> bool:
    """Is the model server responding? (monitoring state; probes immediately if stale)"""
    return (await monitor.current(request.app))["state"] == "up"


@router.get("/status")
async def status(request: Request, db: Database = Depends(get_db)):
    h = await monitor.current(request.app)
    t = now()
    online = h["state"] == "up"
    return {"model_online": online, "state": "up" if online else "down", "label": monitor.PUBLIC_STATE_TEXT.get(h["state"], "Noma'lum"),
            "since": h.get("since") or None, "uptime_30d": monitor.uptime_pct(db, max(t - 30 * 86400, monitor.public_since(db))),
            "models": [r["id"] for r in db.all("SELECT id FROM models WHERE enabled=1 AND listed=1")]}


@router.get("/samples")
def samples(db: Database = Depends(get_db)):
    rows = db.all("SELECT * FROM samples ORDER BY position")
    return {"generated_at": max((r["created_at"] for r in rows), default=None), "items": [{
        "category": r["category"], "prompt": r["prompt"],
        "fast": {"model": r["fast_model"], "answer": r["fast_answer"], "ms": r["fast_ms"]},
        "pro": {"model": r["pro_model"], "answer": r["pro_answer"], "ms": r["pro_ms"], "verified": bool(r["pro_verified"])},
    } for r in rows]}


# --- account ---------------------------------------------------------------------------------------------------
@router.get("/account/summary")
def summary(user=Depends(require_user), db: Database = Depends(get_db)):
    w = db.one("SELECT balance, held FROM wallets WHERE user_id=?", (user["id"],))
    ms = month_start_ts()
    month = db.one(
        """SELECT COUNT(*) n, COALESCE(SUM(cost),0) cost, COALESCE(SUM(prompt_tokens+completion_tokens),0) tokens
           FROM usage_logs WHERE user_id=? AND created_at>=?""", (user["id"], ms))
    today_start = int(datetime.now(TASHKENT).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    today = db.one("SELECT COUNT(*) n, COALESCE(SUM(cost),0) cost FROM usage_logs WHERE user_id=? AND created_at>=?",
                   (user["id"], today_start))
    settings = db.get_settings()
    quota = free_quota(db, user_id=user["id"], settings=settings)
    return {
        "balance": w["balance"] if w else 0,
        "held": w["held"] if w else 0,
        "month": {"requests": month["n"], "cost": month["cost"], "tokens": month["tokens"]},
        "today": {"requests": today["n"], "cost": today["cost"]},
        "active_keys": db.scalar("SELECT COUNT(*) FROM api_keys WHERE user_id=? AND status='active'", (user["id"],)),
        "pending_topups": db.scalar("SELECT COUNT(*) FROM topup_requests WHERE user_id=? AND status='pending'", (user["id"],)),
        "payment_info": settings.get("payment_info", ""),
        "min_topup": db.setting_int("min_topup_micro"),
        # Account without a deposit: today's free-model usage and limit (null = no limit)
        "free_daily": quota.out() if quota else None,
    }


# --- API keys --------------------------------------------------------------------------------------------------
REASONING_PATTERN = "^(inherit|none|low|medium|high|model)$"


def reasoning_db(value: str | None) -> str | None:
    """API value -> database value ('inherit' = follow the global setting = NULL)."""
    return None if value in (None, "inherit") else value


class KeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    monthly_limit: int | None = Field(default=None, ge=0, description="micro-sum; empty = unlimited")
    reasoning_effort: str = Field(default="inherit", pattern=REASONING_PATTERN)


class KeyPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    monthly_limit: int | None = Field(default=None, ge=0)
    clear_monthly_limit: bool = False
    reasoning_effort: str | None = Field(default=None, pattern=REASONING_PATTERN)


def key_out(r, spent: int = 0) -> dict:
    return {
        "id": r["id"], "name": r["name"], "prefix": r["prefix"], "last4": r["last4"], "status": r["status"],
        "rpm_limit": r["rpm_limit"], "monthly_limit": r["monthly_limit"], "free": bool(r["free"]),
        "reasoning_effort": r["reasoning_effort"] or "inherit",
        "created_at": r["created_at"], "last_used_at": r["last_used_at"], "revoked_at": r["revoked_at"],
        "month_spent": spent,
    }


def keys_with_spend(db: Database, where: str, args: tuple) -> list[dict]:
    ms = month_start_ts()
    rows = db.all(f"SELECT * FROM api_keys WHERE {where} ORDER BY status, id DESC", args)
    spent = {r["key_id"]: r["s"] for r in db.all(
        "SELECT key_id, SUM(cost) s FROM usage_logs WHERE created_at>=? AND key_id IS NOT NULL GROUP BY key_id", (ms,))}
    return [key_out(r, spent.get(r["id"], 0)) for r in rows]


@router.get("/keys")
def list_keys(user=Depends(require_user), db: Database = Depends(get_db)):
    return {"items": keys_with_spend(db, "user_id=?", (user["id"],))}


@router.post("/keys")
def create_key(body: KeyIn, user=Depends(require_user), db: Database = Depends(get_db)):
    if db.scalar("SELECT COUNT(*) FROM api_keys WHERE user_id=? AND status='active'", (user["id"],)) >= MAX_ACTIVE_KEYS:
        raise ApiError(400, "too_many_keys", f"Faol kalitlar soni {MAX_ACTIVE_KEYS} tadan oshmasligi kerak.")
    raw, prefix, last4, digest = security.new_api_key()
    with db.tx() as c:
        cur = c.execute(
            """INSERT INTO api_keys(user_id, name, prefix, last4, key_hash, secret_enc, monthly_limit,
                                    reasoning_effort, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (user["id"], body.name.strip(), prefix, last4, digest, security.encrypt_secret(raw),
             body.monthly_limit, reasoning_db(body.reasoning_effort), now()),
        )
    row = db.one("SELECT * FROM api_keys WHERE id=?", (cur.lastrowid,))
    return {"key": raw, "item": key_out(row)}  # the full key is shown only in this response


def _own_key(db: Database, key_id: int, user_id: int):
    row = db.one("SELECT * FROM api_keys WHERE id=? AND user_id=?", (key_id, user_id))
    if row is None:
        raise ApiError(404, "key_not_found", "Kalit topilmadi.")
    return row


@router.patch("/keys/{key_id}")
def patch_key(key_id: int, body: KeyPatch, user=Depends(require_user), db: Database = Depends(get_db)):
    row = _own_key(db, key_id, user["id"])
    name = body.name.strip() if body.name else row["name"]
    limit = None if body.clear_monthly_limit else (body.monthly_limit if body.monthly_limit is not None else row["monthly_limit"])
    effort = row["reasoning_effort"] if body.reasoning_effort is None else reasoning_db(body.reasoning_effort)
    with db.tx() as c:
        c.execute("UPDATE api_keys SET name=?, monthly_limit=?, reasoning_effort=? WHERE id=?", (name, limit, effort, key_id))
    return {"item": key_out(db.one("SELECT * FROM api_keys WHERE id=?", (key_id,)))}


@router.get("/keys/{key_id}/reveal")
def reveal_key(key_id: int, user=Depends(require_user), db: Database = Depends(get_db)):
    """The owner's own key in full, so they can copy it again.

    Only ever the caller's own key (`_own_key` filters by user_id). Returns null for
    keys made before this existed, and when SF_KEY_SECRET is not configured — the
    caller shows "revoke it and make a new one" in that case.
    """
    row = _own_key(db, key_id, user["id"])
    enc = row["secret_enc"] if "secret_enc" in row.keys() else None
    return {"key": security.decrypt_secret(enc)}


@router.post("/keys/{key_id}/revoke")
def revoke_key(key_id: int, user=Depends(require_user), db: Database = Depends(get_db)):
    _own_key(db, key_id, user["id"])
    with db.tx() as c:
        c.execute("UPDATE api_keys SET status='revoked', revoked_at=? WHERE id=? AND status='active'", (now(), key_id))
    return {"ok": True}


# --- usage and ledger history ----------------------------------------------------------------------------------
def usage_out(r) -> dict:
    return {
        "id": r["id"], "request_id": r["request_id"], "created_at": r["created_at"], "model": r["model"],
        "endpoint": r["endpoint"], "key_id": r["key_id"], "key_name": r["key_name"] if "key_name" in r.keys() else None,
        "prompt_tokens": r["prompt_tokens"], "completion_tokens": r["completion_tokens"], "cost": r["cost"],
        "status_code": r["status_code"], "stream": bool(r["stream"]), "estimated": bool(r["estimated"]),
        "latency_ms": r["latency_ms"], "ttfb_ms": r["ttfb_ms"], "error": r["error"], "category": r["category"],
        "reasoning": r["reasoning"],
        "reason": error_label(r["error"], r["status_code"]) if r["category"] else None,
    }


@router.get("/usage")
def usage(user=Depends(require_user), db: Database = Depends(get_db), limit: int = 50, offset: int = 0,
          key_id: int | None = None, failed: bool = False):
    limit, offset = page_args(limit, offset)
    where, args = ["u.user_id=?"], [user["id"]]
    if key_id is not None:
        where.append("u.key_id=?"); args.append(key_id)
    if failed:
        where.append("(u.status_code>=400 OR u.error IS NOT NULL)")
    w = " AND ".join(where)
    total = db.scalar(f"SELECT COUNT(*) FROM usage_logs u WHERE {w}", args)
    rows = db.all(
        f"""SELECT u.*, k.name AS key_name FROM usage_logs u LEFT JOIN api_keys k ON k.id=u.key_id
            WHERE {w} ORDER BY u.id DESC LIMIT ? OFFSET ?""", args + [limit, offset])
    return {"total": total, "items": [usage_out(r) for r in rows]}


def daily_series(db: Database, where: str, args: list, days: int) -> list[dict]:
    days = max(1, min(days, 90))
    today = datetime.now(TASHKENT).replace(hour=0, minute=0, second=0, microsecond=0)
    start = today - timedelta(days=days - 1)
    rows = db.all(
        f"""SELECT to_char(to_timestamp(created_at + 18000) AT TIME ZONE 'UTC', 'YYYY-MM-DD') d, COUNT(*) n, COALESCE(SUM(cost),0) cost,
                   COALESCE(SUM(prompt_tokens),0) pt, COALESCE(SUM(completion_tokens),0) ct
            FROM usage_logs WHERE created_at >= ? AND {where} GROUP BY d""", [int(start.timestamp())] + args)
    by_day = {r["d"]: r for r in rows}
    out = []
    for i in range(days):
        d = (start + timedelta(days=i)).strftime("%Y-%m-%d")
        r = by_day.get(d)
        out.append({"date": d, "requests": r["n"] if r else 0, "cost": r["cost"] if r else 0,
                    "prompt_tokens": r["pt"] if r else 0, "completion_tokens": r["ct"] if r else 0})
    return out


@router.get("/usage/models")
def usage_by_model(days: int = 30, user=Depends(require_user), db: Database = Depends(get_db)):
    """The user's usage per model (requests, tokens, sum) over the last `days` days.

    Legacy model names (sf-fast etc.) are merged under their current name.
    """
    days = max(1, min(int(days or 30), 365))
    since = now() - days * 86400
    rows = db.all(
        "SELECT model, COUNT(*) AS requests,"
        " COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens, COALESCE(SUM(completion_tokens), 0) AS completion_tokens,"
        " COALESCE(SUM(cost), 0) AS cost"
        " FROM usage_logs WHERE user_id=? AND created_at>=? GROUP BY model", (user["id"], since))
    legacy = {old: new for new, olds in _LEGACY_NAMES.items() for old in olds}
    known = {r["id"] for r in db.all("SELECT id FROM models")}
    totals: dict[str, dict] = {}
    for r in rows:
        m = legacy.get(r["model"], r["model"])
        if m not in known:
            # Unknown name (a rejected client typo like "gpt-4") or an internal name: hidden
            # if there was no spend, otherwise added to "boshqa" ("other"); internal names never leak.
            if not (r["prompt_tokens"] or r["completion_tokens"] or r["cost"]):
                continue
            m = "boshqa"
        a = totals.setdefault(m, {"model": m, "requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost": 0})
        for k in ("requests", "prompt_tokens", "completion_tokens", "cost"):
            a[k] += int(r[k] or 0)
    items = sorted(totals.values(), key=lambda x: (x["prompt_tokens"] + x["completion_tokens"], x["requests"]), reverse=True)
    return {"days": days, "items": items}


@router.get("/usage/daily")
def usage_daily(days: int = 30, user=Depends(require_user), db: Database = Depends(get_db)):
    return {"items": daily_series(db, "user_id=?", [user["id"]], days)}


@router.get("/ledger")
def ledger(user=Depends(require_user), db: Database = Depends(get_db), limit: int = 50, offset: int = 0):
    limit, offset = page_args(limit, offset)
    total = db.scalar("SELECT COUNT(*) FROM ledger WHERE user_id=?", (user["id"],))
    rows = db.all("SELECT * FROM ledger WHERE user_id=? ORDER BY id DESC LIMIT ? OFFSET ?", (user["id"], limit, offset))
    return {"total": total, "items": [
        {"id": r["id"], "kind": r["kind"], "amount": r["amount"], "balance_after": r["balance_after"],
         "note": r["note"], "created_at": r["created_at"], "usage_id": r["usage_id"]} for r in rows]}


# --- top-up requests ---------------------------------------------------------------------------------------------
class TopupIn(BaseModel):
    amount: int = Field(gt=0, description="micro-sum")
    note: str = Field(default="", max_length=300)


def topup_out(r) -> dict:
    return {"id": r["id"], "amount": r["amount"], "note": r["note"], "status": r["status"],
            "admin_note": r["admin_note"], "created_at": r["created_at"], "resolved_at": r["resolved_at"]}


@router.get("/topups")
def my_topups(user=Depends(require_user), db: Database = Depends(get_db)):
    rows = db.all("SELECT * FROM topup_requests WHERE user_id=? ORDER BY id DESC LIMIT 50", (user["id"],))
    return {"items": [topup_out(r) for r in rows]}


@router.post("/topups")
def request_topup(body: TopupIn, background: BackgroundTasks, request: Request, user=Depends(require_user),
                  db: Database = Depends(get_db)):
    minimum = db.setting_int("min_topup_micro")
    if body.amount < minimum:
        raise ApiError(400, "amount_too_small", f"Eng kam to'ldirish summasi {minimum // MICRO:,} so'm.".replace(",", " "))
    if db.scalar("SELECT COUNT(*) FROM topup_requests WHERE user_id=? AND status='pending'", (user["id"],)) >= 3:
        raise ApiError(400, "too_many_pending", "Kutilayotgan so'rovlar ko'p. Avval ular ko'rib chiqilishini kuting.")
    with db.tx() as c:
        cur = c.execute("INSERT INTO topup_requests(user_id, amount, note, created_at) VALUES (?,?,?,?)",
                        (user["id"], body.amount, body.note.strip(), now()))
    background.add_task(topup_telegram.notify_new, request.app, cur.lastrowid)  # to the owner's Telegram
    return {"item": topup_out(db.one("SELECT * FROM topup_requests WHERE id=?", (cur.lastrowid,)))}
