"""Load spill: a browser model with no free tab hands the request to the key pool.

Owner's call 2026-11-12: the pool is not a model of its own; it takes requests the
cluster cannot start right away. Same public model, same price.
"""
import json

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import capacity
from .conftest import add_key, add_model, add_user, bearer


@pytest.fixture(autouse=True)
def fresh_cache():
    capacity._cache.update(at=0.0, free=None)
    yield
    capacity._cache.update(at=0.0, free=None)


def _env(env, *, qwen_free, status_ok=True, pool_mode="ok", spill="gemini-zaxira"):
    calls = {"cluster": 0, "pool": 0, "status": 0}
    cluster = FastAPI()

    @cluster.get("/cluster/status")
    async def status():
        calls["status"] += 1
        if not status_ok:
            return JSONResponse({"detail": "down"}, status_code=503)
        return {"workers": [{"model": "qwen", "available": qwen_free}, {"model": "qwen", "available": False},
                            {"model": "gemini", "available": True}]}

    @cluster.post("/v1/chat/completions")
    async def cluster_chat(request: Request):
        calls["cluster"] += 1
        return {"choices": [{"index": 0, "message": {"role": "assistant", "content": "from-cluster"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

    pool = FastAPI()

    @pool.post("/v1/chat/completions")
    async def pool_chat(request: Request):
        calls["pool"] += 1
        if pool_mode == "down":
            return JSONResponse({"error": {"message": "no key answered"}}, status_code=503)
        return {"choices": [{"index": 0, "message": {"role": "assistant", "content": "from-pool"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 100}}

    env.app.state.cluster_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=cluster), base_url="http://cluster")
    env.app.state.ext_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=pool), base_url="http://gemini-pool")
    for k, v in {"cluster_base_url": "http://cluster/v1", "fastpath_enabled": "0", "pool_base_url": "http://gemini-pool:8088/v1",
                 "pool_api_key": "pool-token", "pool_spill_model": spill}.items():
        env.db.set_setting(k, v)
    add_model(env, "slow-agent", upstream="qwen", provider="cluster")
    add_model(env, "gemini-zaxira", upstream="gemini-3.5-flash", provider="pool", listed=0, price_in=1, price_out=1)
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    return calls, key


def _ask(env, key):
    return env.client.post("/v1/chat/completions", headers=bearer(key),
                           json={"model": "slow-agent", "messages": [{"role": "user", "content": "hello there"}]})


def test_busy_model_goes_to_the_pool_at_the_requested_price(env):
    calls, key = _env(env, qwen_free=False)
    r = _ask(env, key)
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "from-pool"
    assert calls == {"cluster": 0, "pool": 1, "status": 1}
    row = env.db.one("SELECT model, cost, attribution FROM usage_logs")
    meta = json.loads(row["attribution"])
    assert row["model"] == "slow-agent" and meta["route_model"] == "gemini-zaxira" and meta["route_reason"] == "capacity"
    # 1000 in + 100 out at the requested model's price, not the pool row's 1 micro-som
    assert row["cost"] == 1000 * 10_000 + 100 * 30_000


def test_free_tab_keeps_the_cluster(env):
    calls, key = _env(env, qwen_free=True)
    assert _ask(env, key).json()["choices"][0]["message"]["content"] == "from-cluster"
    assert calls["pool"] == 0


def test_unknown_status_keeps_the_cluster(env):
    calls, key = _env(env, qwen_free=False, status_ok=False)
    assert _ask(env, key).json()["choices"][0]["message"]["content"] == "from-cluster"
    assert calls["pool"] == 0


def test_status_is_cached_between_requests(env):
    calls, key = _env(env, qwen_free=False)
    _ask(env, key)
    _ask(env, key)
    assert calls["status"] == 1 and calls["pool"] == 2


def test_failing_pool_sends_the_request_back_to_its_browser_model(env):
    calls, key = _env(env, qwen_free=False, pool_mode="down")
    r = _ask(env, key)
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "from-cluster"
    assert calls["pool"] == 1 and calls["cluster"] == 1
    assert r.headers.get("x-fallback-model") == "slow-agent"


def test_spill_can_be_switched_off(env):
    calls, key = _env(env, qwen_free=False, spill="")
    assert _ask(env, key).json()["choices"][0]["message"]["content"] == "from-cluster"
    assert calls["pool"] == 0 and calls["status"] == 0
