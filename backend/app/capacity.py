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

from . import catalog

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


def omni_ready(settings) -> bool:
    return bool((settings.get("omni_base_url") or "").strip() and (settings.get("omni_api_key") or "").strip())


def background_rows(settings) -> list[tuple[str, str]]:
    """(model id, provider) of the background layers, in order: the Gemini key pool, then OmniRoute
    (owner's call 2026-11-27: OmniRoute is not a model of its own either, it takes overflow and failures)."""
    rows = []
    name = (settings.get("pool_spill_model") or "").strip()
    if name and (settings.get("pool_api_key") or "").strip():
        rows.append((name, "pool"))
    name = (settings.get("omni_spill_model") or "").strip()
    if name and omni_ready(settings):
        rows.append((name, "omni"))
    return rows


async def pool_spill_target(state, db, settings, model) -> dict | None:
    """The background model to answer instead of `model`, or None to keep the cluster."""
    layers = background_rows(settings)
    if not layers:
        return None
    free = await free_tabs(state, settings)
    if free is None:
        return None
    upstream = str(model.get("upstream_name") or "").lower()
    if free.get(upstream, 1) > 0:        # free tab, or a model the status does not list
        return None
    for name, provider in layers:
        row = await run_in_threadpool(db.one, "SELECT * FROM models WHERE id=? AND enabled=1 AND provider=?", (name, provider))
        if row:
            return dict(row)
    return None


_balance = {"acc": 0}


def omni_balance_turn(settings) -> bool:
    """Weighted round-robin between the browser cluster and OmniRoute (owner's call 2026-11-27:
    requests are shared between the two sources, not one kept as a spare). `omni_share` is the
    percentage OmniRoute takes; 50 alternates exactly. Per process, which is all there is."""
    try:
        share = max(0, min(100, int(settings.get("omni_share") or 0)))
    except ValueError:
        share = 0
    if share <= 0 or not omni_ready(settings):
        return False
    _balance["acc"] += share
    if _balance["acc"] >= 100:
        _balance["acc"] -= 100
        return True
    return False


async def omni_balance_target(db, settings, model, body) -> dict | None:
    """OmniRoute's row when this browser-model request is OmniRoute's turn, else None."""
    name = (settings.get("omni_spill_model") or "").strip()
    if not name or not omni_balance_turn(settings):
        return None
    row = await run_in_threadpool(db.one, "SELECT * FROM models WHERE id=? AND enabled=1 AND provider='omni'", (name,))
    return dict(row) if row else None


_image_balance = {"acc": 0}


def _image_row_name(settings) -> str:
    return (settings.get("omni_image_model") or "").strip()


def image_eligible(model) -> bool:
    """A 1K browser picture. Image Pro (2K, upscaled by the cluster) stays on the cluster:
    OR's image model draws about 1024 px and would hand back less than was paid for."""
    return (model.get("provider") == "cluster"
            and str(model.get("upstream_name") or "") != catalog.IMAGE_PRO_UPSTREAM
            and str(model.get("upstream_name") or "") not in catalog.VIDEO_UPSTREAMS)


async def _image_row(db, settings) -> dict | None:
    name = _image_row_name(settings)
    if not name or not omni_ready(settings):
        return None
    row = await run_in_threadpool(db.one, "SELECT * FROM models WHERE id=? AND enabled=1 AND provider='omni'", (name,))
    return dict(row) if row else None


async def omni_image_target(state, db, settings, model) -> tuple[dict, str] | None:
    """(OR's picture row, reason) when this browser picture should be drawn by OR:
    its turn by `omni_image_share` ("balance"), or no free tab for the model ("capacity")."""
    if not image_eligible(model):
        return None
    row = await _image_row(db, settings)
    if not row:
        return None
    try:
        share = max(0, min(100, int(settings.get("omni_image_share") or 0)))
    except ValueError:
        share = 0
    if share > 0:
        _image_balance["acc"] += share
        if _image_balance["acc"] >= 100:
            _image_balance["acc"] -= 100
            return row, "balance"
    free = await free_tabs(state, settings)
    upstream = str(model.get("upstream_name") or "").lower()
    if free is not None and free.get(upstream, 1) <= 0:
        return row, "capacity"
    return None


def omni_image_stand_in(db, model) -> str | None:
    """OR's picture row for a browser picture that failed."""
    if not image_eligible(model or {}):
        return None
    s = db.get_settings()
    name = _image_row_name(s)
    return name if name and omni_ready(s) else None


def cover_state(settings) -> dict:
    """What still answers when a tier fails: OmniRoute covers browser models (fallback/stand-in)
    whenever it is configured, and local models only with `omni_cover_local` = 1."""
    omni = omni_ready(settings) and bool((settings.get("omni_spill_model") or "").strip())
    return {"browser": omni, "local": omni and str(settings.get("omni_cover_local", "1")).strip() == "1"}


def omni_covers_local(db, model) -> str | None:
    """OmniRoute's row for a local model whose GPU host failed (owner's call 2026-11-28).

    Local models are "private": their text never leaves our hardware, so the usual fallback rule
    (routing.compatible) refuses every third-party stand-in. The owner chose availability over that
    for outages of the GPU box: with `omni_cover_local` = 1 the request is answered by OmniRoute
    instead of failing. The response says so (X-Fallback-Model, X-SF-Data-Processing: third_party).
    """
    if (model or {}).get("provider") != "local":
        return None
    if str(db.get_settings().get("omni_cover_local", "1")).strip() != "1":
        return None
    return omni_stand_in(db, model)


def omni_stand_in(db, model) -> str | None:
    """OmniRoute's hidden row for a request that failed and has no fallback of its own."""
    if (model or {}).get("provider") == "omni":
        return None
    s = db.get_settings()
    name = (s.get("omni_spill_model") or "").strip()
    return name if name and omni_ready(s) and name != (model or {}).get("id") else None
