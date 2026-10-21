"""Per-request internal metadata passed between server and pool, bypassing the balancer.

At the start of a request the server creates a dict with `start(...)` (e.g. whether
a fallback model is allowed), and the pool writes results into it (whether the
fallback was used, which internal model served it). `contextvars` are copied into
asyncio tasks, but THE DICT ITSELF is a shared object, so changes made inside
`asyncio.wait_for` are visible on the server side too.

Internal model names are only logged; they are never exposed to the client.
"""
from __future__ import annotations

import contextvars

_CURRENT: contextvars.ContextVar = contextvars.ContextVar("cluster_run_meta", default=None)


def start(**values) -> dict:
    meta = dict(values)
    _CURRENT.set(meta)
    return meta


def get() -> dict:
    meta = _CURRENT.get()
    return meta if meta is not None else {}
