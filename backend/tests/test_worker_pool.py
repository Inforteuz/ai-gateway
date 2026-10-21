"""Several workers of one channel: a rate-limited/exhausted worker rests, healthy ones take the traffic,
and /cluster/status shows one clear state per worker."""
import asyncio
import time

import pytest

from cluster import balancer as B
from cluster import pool as pool_mod
from cluster import settings as cfg
from cluster.adapters.base_adapter import Capabilities


class _Adapter:
    caps = Capabilities(files=True)


def worker(port, model="gemini"):
    return pool_mod.WorkerTab(port, f"tab{port}", model, f"ws://127.0.0.1:{port}/x", _Adapter())


class _Pool:
    def __init__(self, workers):
        self.workers = workers

    def workers_for_model(self, m):
        return [w for w in self.workers if w.model_name == m]

    summary = pool_mod.ClusterPool.summary


def test_states():
    w = worker(9222)
    assert w.state() == "ACTIVE"
    w.set_cooldown(60, reason="rate_limit:too many requests")
    assert w.state() == "COOLDOWN" and w.cooldown_reason.startswith("rate_limit")
    w.needs_attention = "quota"
    assert w.state() == "EXHAUSTED"
    w.cooldown_until = 0.0
    assert w.state() == "ACTIVE"

    async def busy():
        async with w.lock:
            return w.state()
    assert asyncio.run(busy()) == "BUSY"


def test_rate_limited_worker_rests_and_the_other_takes_the_traffic():
    w0, w1, q = worker(9222), worker(9223), worker(9224, "qwen")
    bal = B.ClusterBalancer(_Pool([w0, w1, q]))
    w0.set_cooldown(cfg.RATE_LIMIT_COOLDOWN, reason="rate_limit:429")
    picked = bal.pick_candidates("gemini")
    assert picked[0] is w1 and w0 not in picked                 # resting worker is not tried
    w1.set_cooldown(cfg.RATE_LIMIT_COOLDOWN, reason="rate_limit:429")
    assert bal.pick_candidates("gemini") == []                  # whole channel resting
    assert bal.pick_candidates("auto")[0] is q                  # auto: another channel serves


def test_least_used_healthy_worker_first():
    w0, w1 = worker(9222), worker(9223)
    w0.total_requests = 10
    assert B.ClusterBalancer(_Pool([w0, w1])).pick_candidates("gemini")[0] is w1


def test_rate_limit_answer_puts_worker_into_cooldown(monkeypatch):
    w = worker(9222)
    monkeypatch.setattr(pool_mod, "classify_response", lambda raw, prompt: (pool_mod.CLASS_RATE_LIMIT, "too many requests"))

    async def fake_run_once(*a, **kw):
        return "You've reached the limit, try again later", []

    async def no_preflight():
        return None

    monkeypatch.setattr(w, "_run_once", fake_run_once)
    monkeypatch.setattr(w, "_preflight_session", no_preflight)
    with pytest.raises(RuntimeError, match="rate limited"):
        asyncio.run(w.execute_prompt("salom", 10))
    assert w.state() == "COOLDOWN"
    assert w.cooldown_until - time.time() > cfg.RATE_LIMIT_COOLDOWN - 5


def test_status_lists_state_instance_and_reason():
    w0, w1 = worker(9222), worker(9225)
    w1.set_cooldown(900, reason="quota:limit")
    w1.needs_attention = "quota"
    rows = _Pool([w0, w1]).summary()
    assert [(r["instance"], r["state"]) for r in rows] == [(0, "ACTIVE"), (3, "EXHAUSTED")]
    assert rows[0]["cooldown_reason"] is None and rows[1]["cooldown_reason"] == "quota:limit"
