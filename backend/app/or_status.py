"""Tier 3 health for admins: what the background router (OR) reports about its accounts.

OR exposes per-account state only to an API key with its `manage` scope, so this uses a
separate write-only setting, `omni_manage_key`; the traffic key (`omni_api_key`) cannot read
it. Only a whitelisted summary leaves this module — never tokens, e-mails or raw errors that
could carry a credential — and labels stay short (owner's call 2026-11-28: no architecture
names in the UI).
"""
from __future__ import annotations

import re
import time

from fastapi.concurrency import run_in_threadpool

from .db import now

CACHE_TTL = 30.0
_cache: dict = {"at": 0.0, "key": None, "data": None}
_SECRETish = re.compile(r"(sk-[A-Za-z0-9_-]{6,}|sk_[A-Za-z0-9]{6,}|eyJ[A-Za-z0-9_-]{10,}|[A-Za-z0-9_-]{32,})")


def _admin_base(settings) -> str:
    """OR's own API lives next to its OpenAI-style /v1."""
    base = (settings.get("omni_base_url") or "").strip().rstrip("/")
    return base[:-3] if base.endswith("/v1") else base


def _short_error(text) -> str:
    if not text:
        return ""
    text = _SECRETish.sub("…", str(text))
    return re.sub(r"\s+", " ", text).strip()[:140]


def _ts(value) -> float | None:
    """ISO string or epoch (s / ms) to epoch seconds."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e11 else float(value)
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def summarize(connections: list) -> list[dict]:
    """One row per provider: accounts, active, limited, last test state and last error."""
    t = time.time()
    rows: dict[str, dict] = {}
    for c in connections or []:
        if not isinstance(c, dict):
            continue
        name = str(c.get("provider") or "?")
        r = rows.setdefault(name, {"provider": name, "accounts": 0, "active": 0, "limited": 0,
                                   "failing": 0, "last_error": "", "last_error_at": None})
        r["accounts"] += 1
        active = c.get("isActive", c.get("is_active"))
        if active in (True, 1, "1", "true"):
            r["active"] += 1
        limited_until = _ts(c.get("rateLimitedUntil") or c.get("rate_limited_until"))
        if limited_until and limited_until > t:
            r["limited"] += 1
        status = str(c.get("testStatus") or c.get("test_status") or "").lower()
        if status in ("error", "failed", "unavailable", "expired", "invalid"):
            r["failing"] += 1
        err_at = _ts(c.get("lastErrorAt") or c.get("last_error_at"))
        if err_at and (r["last_error_at"] is None or err_at > r["last_error_at"]):
            r["last_error_at"] = err_at
            r["last_error"] = _short_error(c.get("lastError") or c.get("last_error"))
    for r in rows.values():
        r["state"] = ("off" if r["active"] == 0 else
                      "limited" if r["limited"] >= r["active"] else
                      "failing" if r["failing"] >= r["active"] else "up")
    order = {"up": 0, "limited": 1, "failing": 2, "off": 3}
    return sorted(rows.values(), key=lambda r: (order[r["state"]], r["provider"]))


def traffic(db, hours: int = 24) -> dict:
    """What the gateway itself sent to OR (our usage_logs), split by why it went there."""
    since = now() - hours * 3600
    rows = db.all(
        "SELECT COALESCE(attribution::json->>'route_reason','') reason, count(*) n, "
        "SUM(CASE WHEN status_code=200 THEN 1 ELSE 0 END) ok, AVG(latency_ms) avg_ms "
        "FROM usage_logs WHERE created_at >= ? AND attribution::json->>'route_model' = "
        "(SELECT value FROM settings WHERE key='omni_spill_model') GROUP BY 1", (since,))
    by = {r["reason"] or "other": {"requests": int(r["n"]), "ok": int(r["ok"] or 0),
                                   "avg_ms": round(float(r["avg_ms"] or 0))} for r in rows}
    total = sum(v["requests"] for v in by.values())
    ok = sum(v["ok"] for v in by.values())
    avg = round(sum(v["avg_ms"] * v["requests"] for v in by.values()) / total) if total else None
    return {"hours": hours, "requests": total, "ok": ok, "avg_ms": avg, "by_reason": by}


async def snapshot(app) -> dict:
    db = app.state.db
    s = await run_in_threadpool(db.get_settings)
    out = {"configured": bool((s.get("omni_base_url") or "").strip() and (s.get("omni_api_key") or "").strip()),
           "manage_key_set": bool((s.get("omni_manage_key") or "").strip()),
           "reachable": None, "providers": [], "error": None,
           "traffic": await run_in_threadpool(traffic, db)}
    if not out["configured"] or not out["manage_key_set"]:
        return out
    key = s["omni_manage_key"].strip()
    if _cache["data"] is not None and _cache["key"] == key and time.monotonic() - _cache["at"] < CACHE_TTL:
        return {**out, **_cache["data"]}
    data: dict
    try:
        r = await app.state.ext_http.get(_admin_base(s) + "/api/providers", timeout=6.0,
                                         headers={"Authorization": f"Bearer {key}"})
        if r.status_code == 200:
            body = r.json()
            conns = body.get("connections") if isinstance(body, dict) else body
            data = {"reachable": True, "providers": summarize(conns if isinstance(conns, list) else [])}
        else:
            hint = {401: "kalit noto'g'ri", 403: "kalitda manage huquqi yo'q"}.get(r.status_code, f"HTTP {r.status_code}")
            data = {"reachable": True, "error": hint}
    except Exception:
        data = {"reachable": False, "error": "javob bermadi"}
    _cache.update(at=time.monotonic(), key=key, data=data)
    return {**out, **data}
