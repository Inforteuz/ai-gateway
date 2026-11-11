"""Load spill: send a request to the key pool when its browser model has no free tab.

The owner's call (2026-11-12): the pool is not offered as a model of its own; it
takes requests the browser cluster cannot start right away. Without this a request
for a busy model (qwen, the slowest, most often) waits up to 45 s in the
cluster's queue and then takes as long again to answer.

Free tabs come from the cluster's /cluster/status, cached for a few seconds: the
check runs on every cluster request and must not add a round trip to each. When
the status cannot be read, nothing is rerouted.
"""
from __future__ import annotations

import logging
import time

from fastapi.concurrency import run_in_threadpool

log = logging.getLogger("gateway.capacity")

TTL = 3.0
_cache: dict = {"at": 0.0, "free": None}


async def free_tabs(state, settings) -> dict[str, int] | None:
    """{cluster model name: tabs free right now}, or None when unknown."""
    now = time.monotonic()
    if now - _cache["at"] < TTL:
        return _cache["free"]
    base = (settings.get("cluster_base_url") or "").rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    free = None
    if base:
        headers = {"User-Agent": "ai-gateway/1.0"}
        if settings.get("cluster_api_key"):
            headers["Authorization"] = f"Bearer {settings['cluster_api_key']}"
        try:
            r = await state.cluster_http.get(f"{base}/cluster/status", headers=headers, timeout=2.0)
            if r.status_code == 200:
                free = {}
                for w in r.json().get("workers") or []:
                    name = str(w.get("model") or "").lower()
                    free[name] = free.get(name, 0) + (1 if w.get("available") else 0)
        except Exception as e:      # an unreadable status must never fail the request
            log.debug("cluster status unavailable: %s", type(e).__name__)
    _cache.update(at=now, free=free)
    return free


async def pool_spill_target(state, db, settings, model) -> dict | None:
    """The pool model to answer instead of `model`, or None to keep the cluster."""
    name = (settings.get("pool_spill_model") or "").strip()
    if not name or not (settings.get("pool_api_key") or "").strip():
        return None
    free = await free_tabs(state, settings)
    if free is None:
        return None
    upstream = str(model.get("upstream_name") or "").lower()
    if free.get(upstream, 1) > 0:        # free tab, or a model the status does not list
        return None
    row = await run_in_threadpool(db.one, "SELECT * FROM models WHERE id=? AND enabled=1 AND provider='pool'", (name,))
    return dict(row) if row else None
