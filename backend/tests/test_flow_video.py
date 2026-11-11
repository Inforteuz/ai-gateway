"""Video via Google Flow in the browser cluster (2026-11-12).

Flow tabs are media-only workers: never picked for chat, even under "auto"; detected
by host even on a port whose override names the Gemini chat; the video endpoint picks
a free Flow tab, returns the clip as base64 and rests an account whose daily credits
ran out.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import urllib.request
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from cluster import balancer as B, pool as P, server, settings as cfg
from cluster.adapters import get_adapter
from cluster.adapters.base_adapter import Capabilities
from cluster.adapters.flow_adapter import FlowAdapter, FlowError
from cluster.pool import WorkerTab

MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 64


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for d in (P._ACCOUNT_INFLIGHT, P._ACCOUNT_LAST_SEND, P._ACCOUNT_SERIAL_UNTIL, P._TAB_OPENED_AT,
              P._ACCOUNT_DISPATCH, P._ACCOUNT_LOCKS, P._ACCOUNT_BLOCKED):
        d.clear()
    monkeypatch.setattr(server.cfg, "API_KEY", "")
    monkeypatch.setattr(server.app.state, "balancer", getattr(server.app.state, "balancer", None), raising=False)
    yield


class _FakeFlow:
    caps = Capabilities(chat=False, video_gen=True)

    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, []

    async def generate_video(self, ws, prompt, *, image_path=None, aspect="16:9", timeout=300.0, **_):
        first = None
        if image_path:
            with open(image_path, "rb") as f:
                first = f.read(8)
        self.calls.append({"prompt": prompt, "aspect": aspect, "image": first})
        if self.error:
            raise self.error
        return self.result, {"seconds": 8}


class _Worker(WorkerTab):
    @asynccontextmanager
    async def _ws(self):
        yield SimpleNamespace()

    async def connect(self, open_timeout: float = 10.0):
        return self._ws()


def _app_with(workers):
    pool = SimpleNamespace(workers=workers, scan=lambda force=False: asyncio.sleep(0),
                           workers_for_model=lambda m: [w for w in workers if w.model_name == m])
    server.app.state.balancer = SimpleNamespace(pool=pool)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://cluster")


def _post(workers, body):
    async def run():
        async with _app_with(workers) as client:
            return await client.post("/v1/videos/generations", json=body)
    return asyncio.run(run())


def test_flow_adapter_is_registered_and_media_only():
    a = get_adapter("flow")
    assert isinstance(a, FlowAdapter) and a.caps.chat is False and a.caps.video_gen is True
    assert P.detect_model_from_url("https://flow.google.com/project/abc") == "flow"


def test_auto_never_picks_a_flow_tab(monkeypatch):
    chat = WorkerTab(19801, "chat-tab", "gemini", "ws://unused", SimpleNamespace(caps=Capabilities()))
    flow = WorkerTab(19801, "flow-tab", "flow", "ws://unused", FlowAdapter())
    pool = SimpleNamespace(workers=[flow, chat], workers_for_model=lambda m: [w for w in (flow, chat) if w.model_name == m])
    bal = B.ClusterBalancer(pool)
    picked = bal.pick_candidates("auto")
    assert flow not in picked and chat in picked
    assert flow not in bal._matching("auto", ())


def test_flow_tab_on_a_gemini_port_is_still_flow(monkeypatch):
    monkeypatch.setattr(cfg, "PORT_MODEL_OVERRIDE", {19802: "gemini"})
    tabs = [{"type": "page", "id": "g1", "url": "https://gemini.google.com/app", "webSocketDebuggerUrl": "ws://g1"},
            {"type": "page", "id": "f1", "url": "https://flow.google.com/project/x", "webSocketDebuggerUrl": "ws://f1"}]

    class _Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp(json.dumps(tabs).encode()))
    pool = P.ClusterPool(ports=[19802])
    monkeypatch.setattr(pool, "_ensure_parallel_tabs", lambda port, tabs: None)
    pool._scan_sync()
    models = {w.tab_id: w.model_name for w in pool.workers}
    assert models.get("f1") == "flow" and models.get("g1") == "gemini"


def test_video_endpoint_returns_the_clip_and_prices_one_clip():
    fake = _FakeFlow(result=MP4)
    w = _Worker(19803, "flow-a", "flow", "ws://unused", fake)
    r = _post([w], {"prompt": "a paper boat on a pond", "size": "720x1280"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert base64.b64decode(body["data"][0]["b64_mp4"]) == MP4
    assert body["data"][0]["size"] == "720x1280" and body["usage"]["completion_tokens"] == 1_000_000
    assert fake.calls[0]["aspect"] == "9:16" and w.total_requests == 1


def test_first_frame_image_reaches_the_adapter():
    fake = _FakeFlow(result=MP4)
    w = _Worker(19804, "flow-b", "flow", "ws://unused", fake)
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    r = _post([w], {"prompt": "animate this", "image": "data:image/png;base64," + base64.b64encode(png).decode()})
    assert r.status_code == 200, r.text
    assert fake.calls[0]["image"] == png[:8]
    bad = _post([w], {"prompt": "x", "image": "data:text/plain;base64,aGk="})
    assert bad.status_code == 400


def test_quota_rests_the_account_and_returns_503():
    fake = _FakeFlow(error=FlowError("quota_exceeded", "no credits"))
    w = _Worker(19805, "flow-c", "flow", "ws://unused", fake)
    r = _post([w], {"prompt": "x"})
    assert r.status_code == 503 and r.headers.get("retry-after")
    assert w.cooldown_reason == "quota" and not w.is_available()


def test_no_flow_tab_is_503():
    assert _post([], {"prompt": "x"}).status_code == 503
