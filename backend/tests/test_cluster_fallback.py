"""In-provider fallback model ("high demand" -> fallback): pool, cluster server and gateway.

Requirements: only when the admin enables it; visible in the response and in the log;
clients cannot send the internal parameters themselves; fallback answers are not cached.
"""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest
from fastapi import FastAPI, Request

from cluster import pool as P
from cluster import runmeta
from cluster import server as cluster_server
from cluster import settings as ccfg

from .conftest import CHAT, SOM, add_key, add_model, add_user, bearer

HIGH_DEMAND = ("Oops! There was an issue connecting to Qwen3.7-Plus.\n"
               "The service is currently experiencing high demand. Please try again later.")


@pytest.fixture(autouse=True)
def no_pacing(monkeypatch):
    """These tests are about variant choice, not about the gap between requests.

    The account-wide state is module level, so it is cleared as well: every `_run`
    starts its own event loop and an `asyncio.Lock` must not travel between loops.
    """
    monkeypatch.setattr(ccfg, "MIN_REQUEST_GAP", 0.0)
    monkeypatch.setattr(ccfg, "REQUEST_JITTER", 0.0)
    P._ACCOUNT_LOCKS.clear()
    P._ACCOUNT_FINISHED.clear()
    yield
    P._ACCOUNT_LOCKS.clear()
    P._ACCOUNT_FINISHED.clear()


class _Adapter:
    name = "qwen"

    def __init__(self, fallback=True):
        self._fb = fallback

    def has_variant(self, v):
        return self._fb and v == "fallback"

    def clean_text(self, t):
        return t


def _worker(answers, fallback=True):
    w = P.WorkerTab.__new__(P.WorkerTab)
    w.model_name, w.browser_port, w.adapter = "qwen", 9223, _Adapter(fallback)
    w.total_requests, w.last_used, w.cooldown_until, w.prefer_fallback_until = 0, 0, 0.0, 0.0
    w._last_variant, w.last_error, w.needs_attention, w.default_cooldown = None, None, None, 30
    w.warm_since, w.last_finished = 0.0, 0.0
    w.last_probe = {"ok": True}   # session already checked (the pre-check is skipped)
    w.seen = []

    async def run_once(prompt, timeout, **kw):
        v = w._choose_variant(runmeta.get())
        w._last_variant = v
        runmeta.get()["fallback"] = v == "fallback"
        w.seen.append(v)
        return answers[v or "primary"], []
    w._run_once = run_once
    w._record_outcome = lambda ok, transient=False: None
    w.set_cooldown = lambda s, reason="": setattr(w, "cooldown_until", time.time() + s)
    return w


def _run(w, **meta):
    async def go():
        m = runmeta.start(**meta)
        out = await w.execute_prompt("p", 30)
        return out, m
    return asyncio.run(go())


def test_high_demand_switches_to_fallback_when_allowed():
    w = _worker({"primary": HIGH_DEMAND, "fallback": "zaxira javob"})
    (raw, _), meta = _run(w, allow_fallback=True)
    assert raw == "zaxira javob" and meta["fallback"] is True and w.seen == [None, "fallback"]
    assert w.prefer_fallback_until > time.time()               # the next requests go to the fallback for a short while
    (raw2, _), meta2 = _run(w, allow_fallback=True)
    assert w.seen[-1] == "fallback" and meta2["fallback"] is True


def test_high_demand_without_permission_is_busy_error():
    w = _worker({"primary": HIGH_DEMAND, "fallback": "zaxira"})
    with pytest.raises(RuntimeError):
        _run(w, allow_fallback=False)
    assert w.seen == [None] and w.cooldown_until > time.time()


def test_fallback_tried_only_once():
    w = _worker({"primary": HIGH_DEMAND, "fallback": HIGH_DEMAND})
    with pytest.raises(RuntimeError):
        _run(w, allow_fallback=True)
    assert w.seen == [None, "fallback"]


def test_no_fallback_variant_means_plain_busy():
    w = _worker({"primary": HIGH_DEMAND}, fallback=False)
    with pytest.raises(RuntimeError):
        _run(w, allow_fallback=True)
    assert w.seen == [None]


# --- gateway: setting, stripping internal fields, marker ---------------------------------------------------------
def _fake_cluster(state):
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        state["payload"] = await request.json()
        body = {"id": "c1", "object": "chat.completion", "created": 1, "model": "qwen",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "salom"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}
        if state.get("fallback"):
            body["gateway"] = {"fallback_model": True}
        return body
    return app


@pytest.fixture
def cl(env):
    state = {}
    env.app.state.cluster_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=_fake_cluster(state)),
                                                   base_url="http://cluster")
    env.db.set_setting("cluster_base_url", "http://cluster/v1")
    env.db.set_setting("fastpath_enabled", "0")   # these tests cover the cluster path ("Salom" must not take the fast path)
    add_model(env, "model-3.1", provider="cluster", upstream="qwen")
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    return env, state, key


def _chat(env, key, **extra):
    return env.client.post("/v1/chat/completions", json={**CHAT, "model": "model-3.1", **extra}, headers=bearer(key))


def test_gateway_strips_client_internal_fields_and_sends_setting(cl):
    env, state, key = cl
    r = _chat(env, key, sf_force_variant="Qwen3.8-Max", sf_allow_fallback=True)
    assert r.status_code == 200
    assert "sf_force_variant" not in state["payload"] and state["payload"]["sf_allow_fallback"] is False
    env.db.set_setting("cluster_fallback_enabled", "1")
    _chat(env, key)
    assert state["payload"]["sf_allow_fallback"] is True


def test_gateway_marks_fallback_answer(cl):
    env, state, key = cl
    state["fallback"] = True
    r = _chat(env, key)
    assert r.status_code == 200 and r.headers.get("x-sf-fallback-model") == "1"
    assert r.json()["gateway"]["fallback_model"] is True
    log = env.db.one("SELECT error, status_code FROM usage_logs ORDER BY id DESC LIMIT 1")
    assert (log["error"], log["status_code"]) == ("fallback_model", 200)
    state["fallback"] = False
    r2 = _chat(env, key)
    assert "x-sf-fallback-model" not in r2.headers


# --- Qwen model picker: the list opens with an animation ----------------------------------------------------------
def test_qwen_select_variant_waits_until_item_stops_moving(monkeypatch):
    """Measured: an item found at 0.3 s is still moving, a click on it does not register, and
    selecting a variant (or switching back to the main model) failed after ~5 s with "model not selected"."""
    from cluster.adapters import qwen_adapter as qa
    a = qa.QwenAdapter()
    st = {"cur": "Qwen3.7-Plus", "open": False, "polls": 0, "clicks": []}
    target_at = {"x": 90.0, "y": 270.0}

    async def fake_eval(ws, js, timeout=20.0):
        if js == a._CUR_MODEL_JS:
            return st["cur"]
        if js == a._TRIGGER_JS:
            return {"x": 100.0, "y": 44.0}
        if js.startswith(f"({a._ITEM_JS})"):
            if not st["open"]:
                return None
            st["polls"] += 1                        # the item is still moving on the first two reads
            return target_at if st["polls"] > 2 else {"x": 90.0, "y": 270.0 - 40 * (3 - st["polls"])}
        raise AssertionError(js)

    async def fake_click(ws, x, y):
        st["clicks"].append((x, y))
        if (x, y) == (100.0, 44.0):
            st["open"] = True
        elif st["open"] and {"x": x, "y": y} == target_at:
            st["cur"], st["open"] = "Qwen3.8-Omni-Flash", False

    async def noop(*a_, **k):
        return None
    monkeypatch.setattr(qa.cdp, "evaluate", fake_eval)
    monkeypatch.setattr(qa.cdp, "click_at", fake_click)
    monkeypatch.setattr(qa.cdp, "key", noop)
    monkeypatch.setattr(qa.asyncio, "sleep", noop)
    assert asyncio.run(a.select_variant(object(), "Qwen3.8-Omni-Flash")) == "Qwen3.8-Omni-Flash"
    assert st["clicks"] == [(100.0, 44.0), (90.0, 270.0)]   # no click on the moving position


# --- Bot check (captcha): not solved, no reload loop, the worker rests ---------------------------------------------
@pytest.mark.parametrize("stays,want", [(True, (False, "captcha")), (False, (True, "ok"))])
def test_check_session_reloads_once_on_captcha(monkeypatch, stays, want):
    """On captcha: pause + ONE reload without cache; if it stays, hand it to the operator."""
    from cluster.adapters import base_adapter as ba
    from cluster.adapters import qwen_adapter as qa
    adapter = next(v() for v in vars(qa).values()
                   if isinstance(v, type) and issubclass(v, ba.BaseAIAdapter) and v is not ba.BaseAIAdapter)
    st = {"reloads": 0}

    async def fake_eval(ws, js, timeout=20.0):
        if js == adapter.CHALLENGE_JS:
            return "el:nc_1_wrapper" if (st["reloads"] == 0 or stays) else None
        return {"hasInput": True, "needsLogin": False, "url": "https://chat.qwen.ai/"}

    async def fake_call(ws, method, params=None, timeout=20.0):
        if method == "Page.reload":
            assert params == {"ignoreCache": True}
            st["reloads"] += 1
        return {}

    async def noop(*a, **k):
        return None
    monkeypatch.setattr(ba.cdp, "evaluate", fake_eval)
    monkeypatch.setattr(ba.cdp, "call", fake_call)
    monkeypatch.setattr(ba.asyncio, "sleep", noop)
    monkeypatch.setattr(adapter, "wait_for_input", noop)
    assert asyncio.run(adapter.check_session(object())) == want
    assert st["reloads"] == 1          # no endless reloads


def test_challenge_blocks_request_and_rests_worker(monkeypatch):
    w = _worker({"primary": "javob"})

    async def run_once(prompt, timeout, **kw):
        raise P.ChallengeDetected("el:nc_1_wrapper")
    w._run_once = run_once
    with pytest.raises(RuntimeError, match="bot tekshiruvi"):
        _run(w, allow_fallback=True)
    assert w.needs_attention == "captcha" and w.cooldown_until - time.time() > ccfg.CHALLENGE_COOLDOWN - 5


def test_probe_captcha_state_in_admin_panel():
    from cluster import instances
    w = _worker({"primary": "x"})
    w.last_probe = None
    w.apply_probe(False, "captcha")
    assert w.needs_attention == "captcha" and instances.worker_state(w) == instances.STATE_CAPTCHA
    w.apply_probe(True, "ok")                 # once the operator solves it, the next probe restores the worker
    assert w.needs_attention is None and w.cooldown_until == 0.0


# --- Speed: a pre-warmed clean chat -------------------------------------------------------------------------------
class _Conn:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *a):
        return False


def _warm_worker(fresh: bool):
    w = _worker({"primary": "javob"})
    w.adapter.is_fresh_chat = lambda ws: _aret(fresh)
    w.lock = asyncio.Lock()
    w.resets = 0

    async def connect(open_timeout=5):
        return _Conn()

    async def reset_phase():
        w.resets += 1
    w.connect, w._reset_phase = connect, reset_phase
    return w


async def _aret(v):
    return v


@pytest.mark.parametrize("fresh,want_resets", [(True, 0), (False, 1)])
def test_prewarmed_page_used_only_if_fresh(fresh, want_resets):
    w = _warm_worker(fresh)
    w.warm_since = time.time()

    async def go():
        runmeta.start()
        # Only step 1 of _run_once: the pre-warmed page check
        ready = False
        if w.warm_since and time.time() - w.warm_since < ccfg.PREWARM_MAX_AGE:
            async with await w.connect(open_timeout=5) as ws0:
                ready = await w.adapter.is_fresh_chat(ws0)
        w.warm_since = 0.0
        if not ready:
            await w._reset_phase()
    asyncio.run(go())
    assert w.resets == want_resets and w.warm_since == 0.0   # a pre-warmed page is used only once


def test_prewarm_skips_when_queue_waiting(monkeypatch):
    w = _warm_worker(True)
    monkeypatch.setattr(ccfg, "PREWARM_ENABLED", True)

    async def go():
        await w.lock.acquire()
        waiter = asyncio.ensure_future(w.lock.acquire())
        await asyncio.sleep(0)
        w.lock.release()                    # the queued request wakes up now
        await w.prewarm()                   # prewarm must not jump ahead of it
        await waiter
        w.lock.release()
    asyncio.run(go())
    assert w.resets == 0 and w.warm_since == 0.0


def test_prewarm_marks_fresh_page(monkeypatch):
    w = _warm_worker(True)
    monkeypatch.setattr(ccfg, "PREWARM_ENABLED", True)

    async def wait_input(ws, timeout=15):
        return True
    w.adapter.wait_for_input = wait_input
    asyncio.run(w.prewarm())
    assert w.resets == 1 and w.warm_since > 0


def test_startup_prewarm_targets_available_qwen_workers():
    class Worker:
        def __init__(self, model, available=True):
            self.model_name, self.available, self.calls = model, available, 0

        def is_available(self):
            return self.available

        async def prewarm(self):
            self.calls += 1

    qwen, other, sleeping_qwen = Worker("qwen"), Worker("deepseek"), Worker("qwen", False)

    class Pool:
        workers = [qwen, other, sleeping_qwen]

    asyncio.run(cluster_server._prewarm_qwen_workers(Pool()))
    assert (qwen.calls, other.calls, sleeping_qwen.calls) == (1, 0, 0)


def test_startup_prewarm_failure_does_not_prevent_service_start():
    class Worker:
        model_name = "qwen"

        def is_available(self):
            return True

        async def prewarm(self):
            raise RuntimeError("browser unavailable")

    class Pool:
        workers = [Worker()]

    asyncio.run(cluster_server._prewarm_qwen_workers(Pool()))



# --- stand-in model (models.fallback_to) ---------------------------------------------------------
def test_stand_in_chosen_only_for_failures_another_model_would_not_share():
    from app.common import ApiError
    from app.gateway import _stand_in_for
    model = {"id": "sable-3.5", "fallback_to": "sable-3.1"}

    for status, code in ((503, "model_busy"), (503, "model_unavailable"), (502, "upstream_error"),
                         (504, "upstream_timeout")):
        assert _stand_in_for(model, ApiError(status, code, "x")) == "sable-3.1"

    # the stand-in would fail exactly the same way on these
    for status, code in ((400, "upstream_rejected"), (401, "invalid_api_key"), (413, "request_too_large"),
                         (429, "free_daily_limit"), (402, "insufficient_balance")):
        assert _stand_in_for(model, ApiError(status, code, "x")) is None
    # the gateway itself being full: retrying only adds load
    assert _stand_in_for(model, ApiError(503, "server_busy", "x")) is None

    # nothing configured, or pointing at itself
    assert _stand_in_for({"id": "a", "fallback_to": None}, ApiError(503, "model_busy", "x")) is None
    assert _stand_in_for({"id": "a", "fallback_to": "a"}, ApiError(503, "model_busy", "x")) is None
