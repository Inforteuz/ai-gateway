"""Monitoring and reports: detailed views for administrators, a public status page for everyone."""
from __future__ import annotations

import csv
import io
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from . import admission, branding, monitor
from .billing import MICRO, TASHKENT
from .common import ApiError, csrf_guard, get_db, require_admin
from . import cluster_watch
from .db import Database, now
from .failures import CATEGORY_LABELS, error_label
from .routes_account import page_args

admin_router = APIRouter(prefix="/portal/admin", dependencies=[Depends(csrf_guard), Depends(require_admin)], tags=["monitoring"])
public_router = APIRouter(prefix="/portal", dependencies=[Depends(csrf_guard)], tags=["status"])


@public_router.get("/benchmark")
def public_benchmark(db: Database = Depends(get_db)):
    """Model benchmark results: public data.

    No login required: useful both for marketing and for users choosing a
    model. The numbers come from our own benchmark, so publishing them does
    not reveal the architecture.
    """
    from .routes_account import benchmark_results
    return benchmark_results(db)


@public_router.get("/benchmark/external")
def external_benchmark():
    """Published results of external models, with sources.

    NEVER MIXED with our own numbers: this is a separate endpoint because
    the measurements come from different benchmarks. The reasoning is in the
    header comment of `external_benchmarks.py`.

    The data is static (in code), not stored in the database, so no db is
    needed and every number's source is checked in code review.
    """
    from . import external_benchmarks
    return external_benchmarks.public_data()


def status_block(app, h: dict) -> dict:
    state = h["state"]
    # Reservations and slot leases span every API worker, unlike open_tickets.
    current = app.state.db.one("""SELECT COUNT(*) total, COUNT(*) FILTER (WHERE EXISTS (
        SELECT 1 FROM inference_slots s WHERE s.request_id=r.request_id AND s.active=1)) active
        FROM reservations r WHERE r.expires_at>?""", (now(),))
    in_flight = current["active"]
    return {
        "state": state, "label": monitor.STATE_TEXT.get(state, state), "online": state == "up",
        "since": h["since"] or None, "last_check": h["ts"] or None, "latency_ms": h.get("latency_ms"), "error": h.get("error"),
        "in_flight": in_flight, "queued": current["total"] - in_flight, "providers": admission.snapshot(app.state.db),
    }


# --- public status page --------------------------------------------------------------------------------------------
@public_router.get("/status/models")
async def status_models(request: Request):
    """Current state of each public model (for the banner and the status page)."""
    local = request.app.state.health.get("state")
    return await run_in_threadpool(
        cluster_watch.public_status, request.app.state.db, local)


@public_router.get("/status/history")
def status_history(db: Database = Depends(get_db)):
    t = now()
    # Count from the service launch date: the earlier build/test period is not shown
    since = monitor.public_since(db)
    frm = lambda sec: max(t - sec, since)  # noqa: E731
    incs = db.all("SELECT * FROM incidents WHERE started_at >= ? ORDER BY id DESC LIMIT 30", (frm(30 * 86400),))
    return {
        "since": since or None,
        "since_date": datetime.fromtimestamp(since, TASHKENT).strftime("%Y-%m-%d") if since else None,
        "uptime": {"h24": monitor.uptime_pct(db, frm(86400)), "d7": monitor.uptime_pct(db, frm(7 * 86400)),
                   "d30": monitor.uptime_pct(db, frm(30 * 86400))},
        "daily": monitor.daily_uptime(db, 30, since),
        "incidents": [{"started_at": r["started_at"], "ended_at": r["ended_at"], "ongoing": r["ended_at"] is None,
                       "duration": (r["ended_at"] or t) - r["started_at"],
                       "reason_text": monitor.PUBLIC_STATE_TEXT.get(r["reason"], "Aloqa uzilgan")} for r in incs],
    }


# --- administrator: overall monitoring -------------------------------------------------------------------------
@admin_router.get("/monitoring")
async def monitoring(request: Request, days: int = 7, db: Database = Depends(get_db)):
    days = max(1, min(days, 90))
    t = now()
    h = await monitor.current(request.app)
    incs = db.all("SELECT * FROM incidents ORDER BY id DESC LIMIT 20")
    return {
        "status": status_block(request.app, h),
        "uptime": {"h24": monitor.uptime_pct(db, t - 86400), "d7": monitor.uptime_pct(db, t - 7 * 86400), "d30": monitor.uptime_pct(db, t - 30 * 86400)},
        "timeline": monitor.timeline(db, 24, 10),
        "incidents": [monitor.incident_out(db, r) for r in incs],
        "quality": monitor.quality(db, t - days * 86400),
        "days": days,
        "monitor_enabled": request.app.state.config.monitor_enabled,
    }


def _failure_where(category: str, user_id: int | None, since: int) -> tuple[str, list]:
    where, args = ["u.created_at >= ?", "u.category IS NOT NULL"], [since]
    if category in CATEGORY_LABELS:
        where.append("u.category = ?"); args.append(category)
    if user_id:
        where.append("u.user_id = ?"); args.append(user_id)
    return " AND ".join(where), args


@admin_router.get("/failures")
def failures(category: str = "all", user_id: int | None = None, days: int = 7, limit: int = 25, offset: int = 0,
             db: Database = Depends(get_db)):
    limit, offset = page_args(limit, offset)
    since = now() - max(1, min(days, 90)) * 86400
    where, args = _failure_where(category, user_id, since)
    total = db.scalar(f"SELECT COUNT(*) FROM usage_logs u WHERE {where}", args)
    rows = db.all(
        f"""SELECT u.*, k.name AS key_name, us.email AS email FROM usage_logs u
            LEFT JOIN api_keys k ON k.id=u.key_id LEFT JOIN users us ON us.id=u.user_id
            WHERE {where} ORDER BY u.id DESC LIMIT ? OFFSET ?""", args + [limit, offset])
    counts_where, counts_args = _failure_where("all", user_id, since)
    counts = {r["category"]: r["n"] for r in db.all(
        f"SELECT u.category, COUNT(*) n FROM usage_logs u WHERE {counts_where} GROUP BY u.category", counts_args)}
    return {
        "total": total,
        "counts": [{"category": k, "label": v, "count": counts.get(k, 0)} for k, v in CATEGORY_LABELS.items()],
        "items": [{
            "id": r["id"], "request_id": r["request_id"], "created_at": r["created_at"], "email": r["email"], "key_name": r["key_name"],
            "model": r["model"], "endpoint": r["endpoint"], "status_code": r["status_code"], "category": r["category"],
            "category_label": CATEGORY_LABELS.get(r["category"], r["category"]), "error": r["error"],
            "reason": error_label(r["error"], r["status_code"]), "stream": bool(r["stream"]), "latency_ms": r["latency_ms"],
        } for r in rows],
    }


# --- CSV report --------------------------------------------------------------------------------------------------------
def _csv_safe(v):
    """Prevents values from being executed as Excel formulas (CSV injection)."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


CSV_COLUMNS = ["Sana (Toshkent)", "Foydalanuvchi", "Kalit", "Model", "Yo'l", "HTTP", "Natija", "Sabab", "Kirish tokenlari",
               "Chiqish tokenlari", "Narx (so'm)", "Javob vaqti (ms)", "Birinchi token (ms)", "Oqim", "Fikrlash", "So'rov ID"]


@admin_router.get("/reports/usage.csv")
def usage_csv(days: int = 30, only_failed: bool = False, user_id: int | None = None, db: Database = Depends(get_db)):
    since = now() - max(1, min(days, 365)) * 86400
    where, args = ["u.created_at >= ?"], [since]
    if only_failed:
        where.append("u.category IS NOT NULL")
    if user_id:
        where.append("u.user_id = ?"); args.append(user_id)
    w = " AND ".join(where)

    def rows():
        buf = io.StringIO()
        out = csv.writer(buf)
        buf.write("﻿")  # BOM so Excel opens UTF-8 correctly
        out.writerow(CSV_COLUMNS)
        yield buf.getvalue(); buf.seek(0); buf.truncate()
        last_id = 1 << 62
        while True:
            batch = db.all(
                f"""SELECT u.*, k.name AS key_name, us.email AS email FROM usage_logs u
                    LEFT JOIN api_keys k ON k.id=u.key_id LEFT JOIN users us ON us.id=u.user_id
                    WHERE {w} AND u.id < ? ORDER BY u.id DESC LIMIT 2000""", args + [last_id])
            if not batch:
                break
            for r in batch:
                out.writerow([_csv_safe(x) for x in [
                    datetime.fromtimestamp(r["created_at"], TASHKENT).strftime("%Y-%m-%d %H:%M:%S"), r["email"], r["key_name"],
                    r["model"], r["endpoint"], r["status_code"],
                    CATEGORY_LABELS.get(r["category"], "muvaffaqiyatli") if r["category"] else "muvaffaqiyatli",
                    error_label(r["error"], r["status_code"]) if r["error"] else "",
                    r["prompt_tokens"], r["completion_tokens"], f"{r['cost'] / MICRO:.6f}".rstrip("0").rstrip(".") or "0",
                    r["latency_ms"], r["ttfb_ms"], "ha" if r["stream"] else "yo'q", r["reasoning"] or "", r["request_id"]]])
            last_id = batch[-1]["id"]
            yield buf.getvalue(); buf.seek(0); buf.truncate()

    name = datetime.now(TASHKENT).strftime("%Y%m%d")
    return StreamingResponse(rows(), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="gateway-{"failures" if only_failed else "usage"}-{name}.csv"'})


# --- alert test ------------------------------------------------------------------------------------------------------
@admin_router.post("/alerts/test")
async def alerts_test(request: Request):
    ok, detail = await monitor.send_telegram(request.app, f"{branding.BRAND_NAME}: xabarnomalar ishlayapti. Bu sinov xabari.")
    if not ok:
        raise ApiError(400, "alert_failed", f"Xabar yuborilmadi: {detail}")
    return {"ok": True}
