"""Monitoring of the model server (the upstream Ollama host): probes, outages (incidents), uptime, alerts, daily report.

Probe states (based on what the server sees through 127.0.0.1:11434):
  up           Ollama answers /api/tags
  tunnel_down  port not open: the host is off, has no internet, or the tunnel is down
  ollama_down  the tunnel is up, but Ollama returns errors or does not respond
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta

import httpx
from fastapi.concurrency import run_in_threadpool

from . import branding
from .billing import MICRO, TASHKENT
from .db import Database, now
from .config import DEFAULT_SETTINGS
from .failures import CATEGORY_LABELS

log = logging.getLogger("monitor")

FAIL_THRESHOLD = 2          # consecutive failed probes before an outage is declared
RETENTION_DAYS = 45
FRESH_SECONDS_FACTOR = 3    # a probe older than this many intervals is considered stale

STATE_TEXT = {
    "up": "Ishlayapti",
    "tunnel_down": "Kompyuter yoki internet aloqasi yo'q",
    "ollama_down": "Kompyuter ulangan, lekin Ollama javob bermayapti",
    "unknown": "Noma'lum",
}
# For public pages: the internal setup (host, tunnel, Ollama) is not disclosed.
PUBLIC_STATE_TEXT = {"up": "Ishlayapti", "tunnel_down": "Aloqa uzilgan", "ollama_down": "Model xizmati javob bermayapti", "unknown": "Noma'lum"}
_PRIORITY = {"tunnel_down": 2, "ollama_down": 1, "up": 0}


# --- helpers -----------------------------------------------------------------------------------------------------
def fmt_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if d: parts.append(f"{d} kun")
    if h: parts.append(f"{h} soat")
    if m and not d: parts.append(f"{m} daqiqa")
    if s and not (d or h): parts.append(f"{s} soniya")
    return " ".join(parts) or "0 soniya"


def fmt_som(micro: int) -> str:
    v = micro / MICRO
    s = f"{v:,.0f}" if abs(v) >= 1000 else f"{v:,.2f}"
    return s.replace(",", " ") + " so'm"


def tz_time(ts: int) -> str:
    return datetime.fromtimestamp(ts, TASHKENT).strftime("%d.%m %H:%M")


def day_start(ts: int | None = None) -> int:
    dt = datetime.fromtimestamp(ts if ts is not None else now(), TASHKENT)
    return int(dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


# --- probing -------------------------------------------------------------------------------------------------------
async def probe(state) -> tuple[str, int | None, str | None]:
    url = state.config.upstream_url + "/api/tags"
    t0 = time.monotonic()
    ms = lambda: int((time.monotonic() - t0) * 1000)  # noqa: E731
    try:
        r = await state.http.get(url, timeout=httpx.Timeout(connect=3.0, read=6.0, write=3.0, pool=3.0))
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return "tunnel_down", None, "ulanib bo'lmadi"
    except httpx.TimeoutException:
        return "ollama_down", ms(), "javob kutish vaqti tugadi"
    except httpx.HTTPError as e:
        return "ollama_down", ms(), type(e).__name__
    if r.status_code != 200:
        return "ollama_down", ms(), f"HTTP {r.status_code}"
    return "up", ms(), None


def load_state(app) -> None:
    """On restart, restores the last state and how long it has lasted from the database."""
    db: Database = app.state.db
    rows = db.all("SELECT ts, state FROM health_checks ORDER BY ts DESC LIMIT 5000")
    h = app.state.health
    if rows:
        cur = rows[0]["state"]
        since = rows[0]["ts"]
        for r in rows:
            if r["state"] != cur:
                break
            since = r["ts"]
        h.update(state=cur, ts=rows[0]["ts"], since=since)


def record(app, state_name: str, latency: int | None, error: str | None, ts: int | None = None) -> list[dict]:
    """Records a probe result and opens/closes incidents. Returns the events that need an alert."""
    db: Database = app.state.db
    h = app.state.health
    ts = ts if ts is not None else now()
    events: list[dict] = []
    with db.tx() as c:
        c.execute("INSERT INTO health_checks(ts, state, latency_ms, error) VALUES (?,?,?,?)", (ts, state_name, latency, error))
    if state_name != h["state"]:
        h["since"] = ts
    h.update(state=state_name, ts=ts, latency_ms=latency, error=error)

    open_inc = db.one("SELECT * FROM incidents WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1")
    if state_name == "up":
        h["streak"] = 0
        if open_inc:
            with db.tx() as c:
                c.execute("UPDATE incidents SET ended_at=? WHERE id=?", (ts, open_inc["id"]))
            events.append({"type": "recovered", "incident_id": open_inc["id"]})
    else:
        if h["streak"] == 0:
            h["first_fail"] = ts
        h["streak"] += 1
        if open_inc is None and h["streak"] >= FAIL_THRESHOLD:
            with db.tx() as c:
                cur = c.execute("INSERT INTO incidents(started_at, reason) VALUES (?,?)", (h["first_fail"], state_name))
            events.append({"type": "down", "incident_id": cur.lastrowid})
        elif open_inc is not None and open_inc["reason"] != state_name:
            with db.tx() as c:
                c.execute("UPDATE incidents SET reason=? WHERE id=?", (state_name, open_inc["id"]))
    return events


def prune(db: Database) -> None:
    cutoff = now() - RETENTION_DAYS * 86400
    with db.tx() as c:
        c.execute("DELETE FROM health_checks WHERE ts < ?", (cutoff,))


async def current(app) -> dict:
    """Current state. If the monitor is not running or the state is stale, probes immediately (without recording)."""
    h = app.state.health
    if now() - h["ts"] <= app.state.config.probe_interval * FRESH_SECONDS_FACTOR and h["state"] != "unknown":
        return h
    name, ms, err = await probe(app.state)
    return {**h, "state": name, "latency_ms": ms, "error": err, "ts": now()}


# --- request statistics ----------------------------------------------------------------------------------------
def incident_out(db: Database, r) -> dict:
    end = r["ended_at"] or now()
    st = db.one(
        """SELECT COUNT(*) n, COUNT(DISTINCT user_id) u FROM usage_logs
           WHERE category='platform' AND created_at BETWEEN ? AND ?""", (r["started_at"], end + 2))
    return {
        "id": r["id"], "started_at": r["started_at"], "ended_at": r["ended_at"], "ongoing": r["ended_at"] is None,
        "duration": end - r["started_at"], "reason": r["reason"], "reason_text": STATE_TEXT.get(r["reason"], r["reason"]),
        "failed_requests": st["n"], "affected_users": st["u"],
    }


def uptime_pct(db: Database, since: int) -> float | None:
    r = db.one("SELECT COUNT(*) n, SUM(CASE WHEN state='up' THEN 1 ELSE 0 END) ok FROM health_checks WHERE ts>=?", (since,))
    if not r["n"]:
        return None
    return round(100.0 * (r["ok"] or 0) / r["n"], 3)


def timeline(db: Database, hours: int = 24, bucket_minutes: int = 10) -> list[dict]:
    step = bucket_minutes * 60
    end = now()
    start = (end - hours * 3600) // step * step
    n = (end - start) // step + 1
    worst: list[str | None] = [None] * n
    for r in db.all("SELECT ts, state FROM health_checks WHERE ts>=? ORDER BY ts", (start,)):
        i = (r["ts"] - start) // step
        if 0 <= i < n and (worst[i] is None or _PRIORITY.get(r["state"], 0) > _PRIORITY.get(worst[i], 0)):
            worst[i] = r["state"]
    return [{"t": start + i * step, "s": worst[i] or "none"} for i in range(n)]


def public_since(db: Database) -> int:
    """When the public status page starts counting (start of the Tashkent day); 0 if not configured."""
    row = db.one("SELECT value FROM settings WHERE key='status_public_since'")
    val = (row["value"] if row else DEFAULT_SETTINGS.get("status_public_since", "")) or ""
    try:
        return int(datetime.strptime(val, "%Y-%m-%d").replace(tzinfo=TASHKENT).timestamp())
    except ValueError:
        return 0


def daily_uptime(db: Database, days: int = 30, since: int = 0) -> list[dict]:
    first = day_start() - (days - 1) * 86400
    if since > first:
        # Days before launch are not shown (the build period is not service history)
        first = day_start(since) if since <= now() else day_start()
        days = max(1, (day_start() - first) // 86400 + 1)
    checks = {r["d"]: r for r in db.all(
        """SELECT to_char(to_timestamp(ts + 18000) AT TIME ZONE 'UTC', 'YYYY-MM-DD') d, COUNT(*) n,
                  SUM(CASE WHEN state='up' THEN 1 ELSE 0 END) ok FROM health_checks WHERE ts>=? GROUP BY d""", (first,))}
    incs = {r["d"]: r["n"] for r in db.all(
        "SELECT to_char(to_timestamp(started_at + 18000) AT TIME ZONE 'UTC', 'YYYY-MM-DD') d, COUNT(*) n FROM incidents WHERE started_at>=? GROUP BY d", (first,))}
    out = []
    for i in range(days):
        d = datetime.fromtimestamp(first + i * 86400 + 3600, TASHKENT).strftime("%Y-%m-%d")
        r = checks.get(d)
        out.append({"date": d, "uptime": round(100.0 * (r["ok"] or 0) / r["n"], 2) if r and r["n"] else None, "incidents": incs.get(d, 0)})
    return out


def _pct(values: list[int], p: float) -> int | None:
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(round(p * (len(values) - 1))))]


def quality(db: Database, since: int, until: int | None = None) -> dict:
    until = until or now() + 1
    t = db.one(
        """SELECT COUNT(*) n, SUM(CASE WHEN category IS NULL THEN 1 ELSE 0 END) ok,
                  COALESCE(SUM(cost),0) cost, COALESCE(SUM(prompt_tokens+completion_tokens),0) tokens
           FROM usage_logs WHERE created_at>=? AND created_at<?""", (since, until))
    cats = {r["category"]: r["n"] for r in db.all(
        "SELECT category, COUNT(*) n FROM usage_logs WHERE category IS NOT NULL AND created_at>=? AND created_at<? GROUP BY category",
        (since, until))}
    lat = db.all("""SELECT latency_ms, ttfb_ms, attribution FROM usage_logs WHERE category IS NULL AND latency_ms IS NOT NULL
                    AND created_at>=? AND created_at<? ORDER BY id DESC LIMIT 20000""", (since, until))
    from .attribution import unpack
    measured = [r for r in lat if unpack(r["attribution"]).get("cache_hit") is not True]
    total, ok = t["n"], t["ok"] or 0
    platform = cats.get("platform", 0)
    return {
        "requests": total, "succeeded": ok, "failed": total - ok, "cost": t["cost"], "tokens": t["tokens"],
        "success_rate": round(100.0 * ok / total, 2) if total else None,
        # our side only: client errors and billing/limit rejections are not counted
        "reliability": round(100.0 * ok / (ok + platform), 2) if (ok + platform) else None,
        "by_category": [{"category": k, "label": CATEGORY_LABELS[k], "count": cats.get(k, 0)}
                        for k in ("platform", "billing", "limits", "client", "cancelled")],
        "cache_hits_in_latency_sample": len(lat) - len(measured), "latency_samples": len(measured),
        "latency_p50": _pct([r["latency_ms"] for r in measured], 0.5), "latency_p95": _pct([r["latency_ms"] for r in measured], 0.95),
        "ttfb_p50": _pct([r["ttfb_ms"] for r in measured if r["ttfb_ms"] is not None], 0.5),
    }


# --- alerts --------------------------------------------------------------------------------------------------------
async def send_telegram(app, text: str) -> tuple[bool, str]:
    s = await run_in_threadpool(app.state.db.get_settings)
    token, chat = s.get("alert_telegram_token", ""), s.get("alert_telegram_chat_id", "")
    if not token or not chat:
        return False, "Telegram sozlanmagan"
    try:
        r = await app.state.notify_http.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                              json={"chat_id": chat, "text": text, "disable_web_page_preview": True})
    except Exception as e:  # the exception text may contain the token: return only its type
        return False, f"ulanib bo'lmadi ({type(e).__name__})"
    if r.status_code != 200:
        return False, f"Telegram javobi: HTTP {r.status_code}"
    return True, ""


async def announce(app, event: dict) -> None:
    db: Database = app.state.db
    row = await run_in_threadpool(db.one, "SELECT * FROM incidents WHERE id=?", (event["incident_id"],))
    if row is None:
        return
    inc = await run_in_threadpool(incident_out, db, row)
    if event["type"] == "down":
        text = (f"Model ulanmagan\n{inc['reason_text']}\nBoshlandi: {tz_time(inc['started_at'])}\n"
                f"Hozircha bajarilmagan so'rovlar: {inc['failed_requests']}")
    else:
        text = (f"Model qayta ishladi\nUzilish: {tz_time(inc['started_at'])} dan {tz_time(inc['ended_at'])} gacha "
                f"({fmt_duration(inc['duration'])})\nSabab: {inc['reason_text']}\n"
                f"Bajarilmagan so'rovlar: {inc['failed_requests']} ({inc['affected_users']} foydalanuvchi)")
    ok, detail = await send_telegram(app, text)
    if not ok and detail != "Telegram sozlanmagan":
        log.warning("alert not sent: %s", detail)


def build_daily_report(db: Database, day_begin: int) -> str:
    day_end = day_begin + 86400
    q = quality(db, day_begin, day_end)
    inc = db.all("SELECT * FROM incidents WHERE started_at>=? AND started_at<?", (day_begin, day_end))
    downtime = sum(((r["ended_at"] or min(now(), day_end)) - r["started_at"]) for r in inc)
    r = db.one("SELECT COUNT(*) n, SUM(CASE WHEN state='up' THEN 1 ELSE 0 END) ok FROM health_checks WHERE ts>=? AND ts<?", (day_begin, day_end))
    up = round(100.0 * (r["ok"] or 0) / r["n"], 2) if r["n"] else None
    cats = {c["category"]: c["count"] for c in q["by_category"]}
    lines = [f"{branding.BRAND_NAME}: kunlik hisobot ({datetime.fromtimestamp(day_begin, TASHKENT).strftime('%d.%m.%Y')})",
             f"So'rovlar: {q['requests']}, muvaffaqiyatli: {q['succeeded']}" + (f" ({str(q['success_rate']).replace('.', ',')}%)" if q["success_rate"] is not None else "")]
    if q["failed"]:
        lines.append(f"Bajarilmadi: {q['failed']} (model muammosi {cats['platform']}, hisob yetmadi {cats['billing']}, "
                     f"tezlik chegarasi {cats['limits']}, noto'g'ri so'rov {cats['client']}, bekor qilingan {cats['cancelled']})")
    lines.append(f"Yechildi: {fmt_som(q['cost'])}, tokenlar: {q['tokens']:,}".replace(",", " "))
    lines.append("Model ishlash vaqti: " + (f"{str(up).replace('.', ',')}%" if up is not None else "ma'lumot yo'q")
                 + f", uzilishlar: {len(inc)}" + (f" (jami {fmt_duration(downtime)})" if inc else ""))
    pending = db.scalar("SELECT COUNT(*) FROM topup_requests WHERE status='pending'")
    if pending:
        lines.append(f"Ko'rib chiqilmagan to'ldirish so'rovlari: {pending}")
    return "\n".join(lines)


async def maybe_daily_report(app) -> None:
    """Yesterday's report, once a day, after 09:00 Tashkent time."""
    db: Database = app.state.db
    local = datetime.now(TASHKENT)
    if local.hour < 9:
        return
    today = local.strftime("%Y-%m-%d")
    s = await run_in_threadpool(db.get_settings)
    if s.get("last_daily_report") == today or not s.get("alert_telegram_token") or not s.get("alert_telegram_chat_id"):
        return
    text = await run_in_threadpool(build_daily_report, db, day_start() - 86400)
    ok, detail = await send_telegram(app, text)
    if ok:
        await run_in_threadpool(db.set_setting, "last_daily_report", today)
    else:
        log.warning("daily report not sent: %s", detail)


# --- main loop -------------------------------------------------------------------------------------------------------
async def loop(app) -> None:
    cfg = app.state.config
    await run_in_threadpool(load_state, app)
    last_prune = 0.0
    while True:
        try:
            name, ms, err = await probe(app.state)
            events = await run_in_threadpool(record, app, name, ms, err)
            for ev in events:
                await announce(app, ev)
            if time.monotonic() - last_prune > 3600:
                await run_in_threadpool(prune, app.state.db)
                last_prune = time.monotonic()
            await maybe_daily_report(app)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("error in monitoring loop")
        await asyncio.sleep(cfg.probe_interval)
