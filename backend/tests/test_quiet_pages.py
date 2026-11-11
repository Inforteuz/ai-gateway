"""Page videos are paused on idle tabs.

Measured 2026-11-11: with no GPU, Chrome decoded Venice's full-screen background
video on the CPU at 60 fps — one core at ~98% around the clock on an idle server.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from cluster import pool as P
from cluster.adapters import base_adapter as BA


class _Adapter(BA.BaseAIAdapter):
    async def send_prompt(self, ws, text):
        return True

    async def read_response(self, ws, timeout=45):
        return ""


def _adapter():
    a = object.__new__(_Adapter)
    a.name = "test"
    return a


def test_quiet_page_runs_the_script_and_survives_errors(monkeypatch):
    seen = []

    async def ok(ws, script, timeout=20):
        seen.append(script)
        return 2

    async def broken(ws, script, timeout=20):
        raise RuntimeError("page gone")

    monkeypatch.setattr(BA.cdp, "evaluate", ok)
    assert asyncio.run(_adapter().quiet_page(None)) == 2 and seen == [BA.BaseAIAdapter.QUIET_JS]
    monkeypatch.setattr(BA.cdp, "evaluate", broken)
    assert asyncio.run(_adapter().quiet_page(None)) == 0       # never breaks the caller


class _WS:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _worker(port, quiet):
    w = P.WorkerTab(port, f"tab{port}", "venice", "ws://unused", SimpleNamespace(quiet_page=quiet))

    async def connect(open_timeout=5):
        return _WS()
    w.connect = connect
    return w


def test_quiet_idle_skips_busy_tabs():
    touched = []

    async def quiet(ws):
        touched.append(1)
        return 1

    idle, busy = _worker(19901, quiet), _worker(19902, quiet)

    async def run():
        await busy.lock.acquire()            # a live request holds this tab
        try:
            return await P.ClusterPool.quiet_idle(SimpleNamespace(workers=[idle, busy]))
        finally:
            busy.lock.release()

    assert asyncio.run(run()) == 1 and len(touched) == 1


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_script_pauses_now_and_later():
    # A minimal DOM: two playing videos now, a third that starts after the script ran.
    js = """
    const listeners = [];
    const mk = () => ({ tagName: 'VIDEO', paused: false, autoplay: true,
      pause() { this.paused = true }, removeAttribute(n) { if (n === 'autoplay') this.autoplay = false } });
    const now = [mk(), mk()];
    global.window = {};
    global.document = { querySelectorAll: () => now,
      addEventListener: (type, fn, capture) => listeners.push({ type, fn, capture }) };
    const count = eval(%s);
    const later = mk();
    listeners.filter(l => l.type === 'play').forEach(l => l.fn({ target: later }));
    eval(%s);   // a second pass must not add a second listener
    console.log(JSON.stringify({ count, now: now.map(v => [v.paused, v.autoplay]),
      later: [later.paused, later.autoplay], listeners: listeners.length, capture: listeners[0].capture }));
    """ % (json.dumps(BA.BaseAIAdapter.QUIET_JS), json.dumps(BA.BaseAIAdapter.QUIET_JS))
    out = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)
    assert out == {"count": 2, "now": [[True, False], [True, False]], "later": [True, False],
                   "listeners": 1, "capture": True}
