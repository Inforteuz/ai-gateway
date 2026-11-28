"""Internal names for the three layers that answer requests (admin pages only).

- Tier 1: our own GPU host (local, private models through Ollama).
- Tier 2: the browser cluster (Chrome instances with web-chat accounts).
- Tier 3: the background layer — OmniRoute and the Gemini key pool — which shares
  browser-model traffic and covers failures.

The public status never names these: it shows per-model state and one overall level
(cluster_watch.public_status). Keeping the architecture off public pages was the owner's
call on 2026-11-28.
"""
from __future__ import annotations

import time

from fastapi.concurrency import run_in_threadpool

from . import capacity, monitor

_omni_probe = {"at": 0.0, "ok": None}
OMNI_PROBE_TTL = 60.0


async def _omni_alive(app, settings) -> bool | None:
    """None when OmniRoute is not configured; otherwise a cached GET /models (60 s)."""
    if not capacity.omni_ready(settings):
        return None
    if time.monotonic() - _omni_probe["at"] < OMNI_PROBE_TTL and _omni_probe["ok"] is not None:
        return _omni_probe["ok"]
    ok = False
    try:
        base = settings["omni_base_url"].rstrip("/")
        r = await app.state.ext_http.get(base + "/models", timeout=4.0,
                                         headers={"Authorization": f"Bearer {settings['omni_api_key']}"})
        ok = r.status_code == 200
    except Exception:
        ok = False
    _omni_probe.update(at=time.monotonic(), ok=ok)
    return ok


def _cluster(db) -> dict:
    rows = db.all("SELECT upstream, state FROM model_health")
    up = sum(1 for r in rows if r["state"] == "up")
    state = "unknown" if not rows else ("up" if up == len(rows) else ("partial" if up else "down"))
    return {"state": state, "up": up, "total": len(rows)}


async def snapshot(app) -> dict:
    db = app.state.db
    settings = await run_in_threadpool(db.get_settings)
    h = await monitor.current(app)
    tier1 = {"state": "up" if h["state"] == "up" else "down", "detail": monitor.PUBLIC_STATE_TEXT.get(h["state"], "")}
    tier2 = await run_in_threadpool(_cluster, db)
    omni = await _omni_alive(app, settings)
    pool = bool((settings.get("pool_api_key") or "").strip())
    tier3_state = "up" if omni or (omni is None and pool) else ("partial" if pool else ("off" if omni is None else "down"))
    cover = capacity.cover_state(settings)
    # Short keys for the same reason as the labels: OR = background router, GP = key pool
    tier3 = {"state": tier3_state, "or": {None: "off", True: "up", False: "down"}[omni], "gp": pool,
             "covers_local": cover["local"], "share": settings.get("omni_share", "0")}
    return {"tier1": tier1, "tier2": tier2, "tier3": tier3}
