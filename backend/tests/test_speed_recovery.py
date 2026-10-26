"""Bounded recovery keeps real failures distinct from page/session health."""
import asyncio
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from cluster import pool, runmeta, settings as cfg
from cluster.adapters import gemini_adapter as ga
from .test_audit_regressions import cluster_client


@pytest.mark.parametrize("reloaded", [False, True])
def test_only_successful_reload_clears_stale_timeout(reloaded):
    w = pool.WorkerTab(19871, "recovery", "gemini", "ws://unused", object())
    w.last_error = "timeout"
    w.consecutive_failures = 2
    w.set_cooldown(30, reason="timeout")
    w.apply_probe(True, "ok", reloaded=reloaded)
    assert (w.last_error is None) is reloaded
    assert (w.cooldown_until == 0) is reloaded
    assert w.last_probe["ok"]


def test_failed_reload_and_quota_are_not_cleared():
    w = pool.WorkerTab(19872, "recovery", "gemini", "ws://unused", object())
    w.last_error = "timeout"
    w.apply_probe(False, "probe_failed:TimeoutError", reloaded=True)
    assert w.last_error == "timeout"
    w.last_error = "quota"
    w.set_cooldown(300, reason="quota:limit")
    w.apply_probe(True, "ok", reloaded=True)
    assert w.last_error == "quota" and w.cooldown_until > time.time()


def test_reload_keeps_an_account_block_from_a_sibling():
    a = pool.WorkerTab(19874, "first", "qwen", "ws://unused", object())
    b = pool.WorkerTab(19874, "second", "qwen", "ws://unused", object())
    a.last_error = "timeout"
    a.set_cooldown(30, reason="timeout")
    b.set_cooldown(300, reason="quota:limit")
    a.apply_probe(True, "ok", reloaded=True)
    assert a.last_error is None and a.cooldown_until > time.time()
    pool._ACCOUNT_BLOCKED.pop(a.account_key, None)


def test_fast_default_effort_is_used_for_first_text_deadline(monkeypatch):
    from cluster.adapters import base_adapter as ba
    from .test_live_stream import _poll_adapter

    async def evaluate(ws, js, timeout=20):
        return {"answer": "", "generating": True}

    monkeypatch.setattr(ba.cdp, "evaluate", evaluate)
    monkeypatch.setattr(cfg, "POLL_INTERVAL", .01)
    monkeypatch.setattr(cfg, "POLL_FIRST_DELAY", .01)
    monkeypatch.setattr(cfg, "ANSWER_FIRST_TEXT_TIMEOUT", .06)

    async def run():
        runmeta.start(effective_effort="low")
        await _poll_adapter().poll_answer(None, "script", timeout=1, initial_delay=.01)

    with pytest.raises(asyncio.TimeoutError, match="fast-effort"):
        asyncio.run(run())


@pytest.mark.parametrize("challenge", [None, "slider"])
def test_gemini_redo_is_latest_response_scoped_and_confirmed(monkeypatch, challenge):
    events = []
    adapter = ga.GeminiAdapter()

    async def detect(ws):
        return challenge

    async def evaluate(ws, script, timeout):
        if '[role="menuitem"]' in script:
            events.append("menu")
            assert "longer" not in script.lower()
            return {"x": 15, "y": 40}
        assert "redo|retry" in script and "model-response" in script
        assert "pre, code" in script and "aria-disabled" in script
        events.append("locate")
        return {"x": 12, "y": 34}

    async def click(ws, x, y):
        events.append("click")

    async def wait(ws, script, timeout, interval):
        assert "stop|останов" in script
        events.append("confirmed")
        return True

    adapter.detect_challenge, adapter.wait_until = detect, wait
    monkeypatch.setattr(ga, "cdp", SimpleNamespace(evaluate=evaluate, click_at=click))
    assert asyncio.run(adapter.regenerate_response(None)) is (not challenge)
    assert events == ([] if challenge else ["locate", "click", "menu", "click", "confirmed"])


@pytest.mark.parametrize("first,committed,expected", [
    (None, False, "recovered"),
    ("Something went wrong. Please try again.", False, "recovered"),
    ("Я не могу помочь с этим запросом.", False, "Я не могу помочь с этим запросом."),
    ("42", False, "42"),
    (None, True, None),
])
def test_worker_retries_technical_failure_once_inside_original_budget(monkeypatch, first, committed, expected):
    attempts = []

    @asynccontextmanager
    async def connection():
        yield object()

    async def connect(open_timeout=5):
        return connection()

    async def yes(*args, **kwargs):
        return True

    async def no_op(*args, **kwargs):
        pass

    async def effort(ws, requested):
        return requested or "low"

    async def read(ws, timeout):
        attempts.append(("read", timeout))
        if len([x for x in attempts if x[0] == "read"]) == 1:
            if first is None:
                raise asyncio.TimeoutError("stuck")
            return first
        return "recovered"

    async def regenerate(ws):
        attempts.append(("regenerate", 0))
        return True

    adapter = SimpleNamespace(wait_for_input=no_op, select_variant=no_op, set_effort=effort,
        dismiss_overlays=no_op, clear_attachments=no_op, clear_composer_text=no_op,
        send_prompt=yes, read_response=read, regenerate_response=regenerate, detect_challenge=no_op)
    w = pool.WorkerTab(19873, "recovery", "gemini", "ws://unused", adapter)
    w.connect, w._reuse_clean_chat = connect, yes
    w._lookup_ws_url = lambda: w.ws_url

    async def run():
        meta = runmeta.start(stream_committed=committed)
        result = await w._run_once("test", 45)
        assert meta["effective_effort"] == "low"
        return result

    if expected is None:
        with pytest.raises(asyncio.TimeoutError):
            asyncio.run(run())
    else:
        assert asyncio.run(run()) == (expected, [])
    retries = [x for x in attempts if x[0] == "regenerate"]
    assert len(retries) == int(expected == "recovered")
    assert all(0 < budget <= 45 for name, budget in attempts if name == "read")


@pytest.mark.parametrize("effort", ["low", "minimal", "none"])
def test_fast_effort_does_not_trigger_yaml_plan_or_second_generation(cluster_client, monkeypatch, effort):
    from cluster import settings as cfg
    monkeypatch.setattr(cfg, "REASONING_YAML", True)
    question = "Analyze the cause and compare the available solutions. " * 8
    response, bal = cluster_client(["A complete direct answer."], {"messages": [{"role": "user", "content": question}],
                                                               "reasoning_effort": effort})
    assert response.status_code == 200 and len(bal.prompts) == 1
    assert "[Javob tartibi" not in bal.prompts[0]


def test_configured_stand_in_runs_after_two_failed_accounts(monkeypatch):
    from cluster import balancer as B
    from .test_audit_regressions import _Worker, _Pool
    order = []

    class Worker(_Worker):
        async def execute_prompt(self, prompt, timeout, **kwargs):
            order.append(self.browser_port)
            if self.model_name == "gemini":
                raise asyncio.TimeoutError()
            runmeta.get()["fallback"] = False  # Normal worker variant metadata.
            return "valid answer", []

    workers = [Worker("gemini") for _ in range(4)] + [Worker("deepseek")]
    for i, worker in enumerate(workers):
        worker.browser_port = 19880 + i
    monkeypatch.setattr(cfg, "SPILL_AFTER_FAILURES", 2)
    monkeypatch.setattr(cfg, "PREWARM_ENABLED", False)
    bal = B.ClusterBalancer(_Pool(workers))

    async def run():
        meta = runmeta.start()
        result = await bal.execute("test", "gemini", spill_to="deepseek", timeout_per_worker=3)
        assert meta["fallback"] is True
        return result

    assert asyncio.run(run())[1] == "deepseek"
    assert order == [19880, 19881, 19884]


def test_failure_spill_requires_configured_destination(monkeypatch):
    from cluster import balancer as B
    from .test_audit_regressions import _Worker, _Pool
    order = []

    class Worker(_Worker):
        async def execute_prompt(self, prompt, timeout, **kwargs):
            order.append(self.browser_port)
            if len(order) <= 2:
                raise asyncio.TimeoutError()
            return "valid answer", []

    workers = [Worker("gemini") for _ in range(3)]
    for i, worker in enumerate(workers):
        worker.browser_port = 19890 + i
    monkeypatch.setattr(cfg, "SPILL_AFTER_FAILURES", 2)
    monkeypatch.setattr(cfg, "PREWARM_ENABLED", False)
    bal = B.ClusterBalancer(_Pool(workers))

    async def run():
        runmeta.start()
        return await bal.execute("test", "gemini", timeout_per_worker=3)

    assert asyncio.run(run())[1] == "gemini" and order == [19890, 19891, 19892]
