"""Parallel tabs on one account (cfg.PARALLEL_TAB_MODELS, owner's call 2026-10-25).

Accounts are scarce and opencode prompts only fit qwen, so a qwen account gets a
second tab that may answer at the same time. Qwen's captcha follows request
density, hence the two safeguards tested here: a parallel send waits
PARALLEL_SEND_GAP after the account's previous send, and a captcha puts the
account back to one request at a time.
"""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from cluster import balancer as B, pool as P, runmeta, settings as cfg
from cluster.pool import WorkerTab


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(cfg, "PARALLEL_TAB_MODELS", frozenset({"qwen"}))
    monkeypatch.setattr(cfg, "PARALLEL_TABS", 2)
    monkeypatch.setattr(cfg, "PARALLEL_SEND_GAP", 30)
    for d in (P._ACCOUNT_INFLIGHT, P._ACCOUNT_LAST_SEND, P._ACCOUNT_SERIAL_UNTIL, P._TAB_OPENED_AT,
              P._ACCOUNT_DISPATCH, P._ACCOUNT_LOCKS, P._ACCOUNT_BLOCKED):
        d.clear()
    yield


def tabs(model="qwen", port=19801):
    adapter = SimpleNamespace()
    return (WorkerTab(port, "tab-a", model, "ws://unused", adapter),
            WorkerTab(port, "tab-b", model, "ws://unused", adapter))


def test_second_tab_waits_the_send_gap_then_joins():
    a, b = tabs()
    a.claim()
    assert not b.is_available()                       # a sent just now
    P._ACCOUNT_LAST_SEND[a.account_key] = time.time() - 31
    assert b.is_available()                           # 30 s later it may run alongside
    assert a.dispatch_lock is not b.dispatch_lock and a.account_lock is not b.account_lock
    a.release_claim()
    P._ACCOUNT_LAST_SEND[a.account_key] = time.time()
    assert b.is_available()                           # nothing in flight: no gap needed


def test_captcha_puts_the_account_back_to_one_request():
    a, b = tabs()
    a._on_challenge("slider")
    P._ACCOUNT_BLOCKED.pop(a.account_key, None)       # look past the captcha cooldown itself
    a.cooldown_until = 0
    b.cooldown_until = 0
    a.claim()
    P._ACCOUNT_LAST_SEND[a.account_key] = time.time() - 600
    assert not b.is_available()                       # serial again, however long ago a sent
    assert a.dispatch_lock is b.dispatch_lock


def test_probe_captcha_also_puts_it_back():
    a, _ = tabs()
    a.apply_probe(False, "captcha")
    assert not P.parallel_allowed(a.account_key)


def test_other_models_keep_one_request_per_account():
    a, b = tabs(model="gemini")
    assert a.dispatch_lock is b.dispatch_lock and not b.sibling_blocked()


class _Tab(WorkerTab):
    """Real locks and gates; the browser part is a timed fake."""

    def __init__(self, tab_id, starts):
        super().__init__(19802, tab_id, "qwen", "ws://unused", SimpleNamespace(clean_text=lambda t: t))
        self.starts = starts

    async def execute_prompt(self, prompt, timeout, **kw):
        async with self.account_lock:
            self.starts.append((self.tab_id, time.monotonic()))
            await asyncio.sleep(0.6)
            return "javob", []


class _Pool:
    def __init__(self, workers):
        self.workers = workers

    async def scan(self, force=False):
        pass

    def workers_for_model(self, m):
        return [w for w in self.workers if w.model_name == m]


def _run_two(monkeypatch, gap):
    monkeypatch.setattr(cfg, "PARALLEL_SEND_GAP", gap)
    monkeypatch.setattr(B.self_heal, "is_recovering", lambda p: False)
    starts = []
    bal = B.ClusterBalancer(_Pool([_Tab("tab-a", starts), _Tab("tab-b", starts)]))

    async def run():
        runmeta.start()
        t0 = time.monotonic()
        await asyncio.gather(*(bal.execute("salom", requested_model="qwen", timeout_per_worker=5)
                               for _ in range(2)))
        return t0

    t0 = asyncio.run(run())
    return sorted(t - t0 for _, t in starts), {tab for tab, _ in starts}


def test_two_requests_overlap_after_the_gap(monkeypatch):
    (first, second), used = _run_two(monkeypatch, gap=0.2)
    assert used == {"tab-a", "tab-b"}
    assert 0.2 <= second - first < 0.6                # waited the gap, did not wait for the answer


def test_without_parallel_they_run_one_after_another(monkeypatch):
    monkeypatch.setattr(cfg, "PARALLEL_TAB_MODELS", frozenset())
    (first, second), _ = _run_two(monkeypatch, gap=0.2)
    assert second - first >= 0.6


# --- opening the second tab ---------------------------------------------------
def _scan_with(monkeypatch, pages):
    opened = []

    class Resp:
        def __init__(self, body):
            self.body = body

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(req, timeout=None):
        if getattr(req, "method", None) == "PUT":
            opened.append(req.full_url)
            return Resp(json.dumps({"id": "new-tab"}).encode())
        raise AssertionError("unexpected request")

    monkeypatch.setattr(P.urllib.request, "urlopen", urlopen)
    P.ClusterPool([19803])._ensure_parallel_tabs(19803, pages)
    return opened


QWEN = {"type": "page", "url": "https://chat.qwen.ai/?temporary-chat=true", "id": "q1"}


def test_a_single_qwen_tab_gets_a_sibling_once(monkeypatch):
    opened = _scan_with(monkeypatch, [QWEN])
    assert len(opened) == 1 and "/json/new?" in opened[0] and "chat.qwen.ai" in opened[0]
    assert _scan_with(monkeypatch, [QWEN]) == []      # rate-limited: no tab storm


def test_no_extra_tab_when_enough_or_serialized(monkeypatch):
    assert _scan_with(monkeypatch, [QWEN, {**QWEN, "id": "q2"}]) == []
    P._TAB_OPENED_AT.clear()
    P.serialize_account(("qwen", 19803), "test")
    assert _scan_with(monkeypatch, [QWEN]) == []


def test_other_models_get_no_extra_tab(monkeypatch):
    assert _scan_with(monkeypatch, [{"type": "page", "url": "https://gemini.google.com/app", "id": "g"}]) == []
