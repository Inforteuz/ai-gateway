"""OmniRoute as a second source beside the browser cluster (owner's call 2026-11-27).

Never a catalog model. Browser-model chat requests are shared with it by `omni_share`
(round-robin); a failure on one source goes to the other; with no free tab and no key
pool it takes the overflow. Same public model, same price.
"""
import json

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import capacity
from .conftest import add_key, add_model, add_user, bearer
from .test_models_refine import login_admin


@pytest.fixture(autouse=True)
def fresh_cache():
    capacity._cache.update(at=0.0, free=None)
    capacity._balance["acc"] = 0
    yield
    capacity._cache.update(at=0.0, free=None)
    capacity._balance["acc"] = 0


def _env(env, *, qwen_free=True, cluster_status=200, omni=True, pool=False, share="0", omni_status=200):
    calls = {"cluster": 0, "omni": 0, "pool": 0, "omni_auth": None, "omni_model": None}
    cluster = FastAPI()

    @cluster.get("/cluster/status")
    async def status():
        return {"workers": [{"model": "qwen", "available": qwen_free}]}

    @cluster.post("/v1/chat/completions")
    async def cluster_chat(request: Request):
        calls["cluster"] += 1
        if cluster_status != 200:
            return JSONResponse({"error": {"message": "all tabs failed", "code": "model_unavailable"}}, status_code=cluster_status)
        return {"choices": [{"index": 0, "message": {"role": "assistant", "content": "from-cluster"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

    ext = FastAPI()

    @ext.post("/v1/chat/completions")
    async def ext_chat(request: Request):
        body = await request.json()
        who = "omni" if request.url.hostname == "front-tunnel" else "pool"
        calls[who] += 1
        if who == "omni":
            calls["omni_auth"], calls["omni_model"] = request.headers.get("authorization"), body.get("model")
            if omni_status != 200:
                return JSONResponse({"error": {"message": "no provider answered"}}, status_code=omni_status)
        # OmniRoute answers with the real upstream model's name; the client must never see it
        return {"model": "kimi-k3" if who == "omni" else "gemini-3.5-flash",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": f"from-{who}"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 100}}

    env.app.state.cluster_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=cluster), base_url="http://cluster")
    env.app.state.ext_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=ext), base_url="http://ext")
    settings = {"cluster_base_url": "http://cluster/v1", "fastpath_enabled": "0", "pool_spill_model": "gemini-zaxira",
                "pool_base_url": "http://gemini-pool:8088/v1", "pool_api_key": "pool-token" if pool else "",
                "omni_base_url": "http://front-tunnel:20128/v1" if omni else "", "omni_api_key": "omni-key" if omni else "",
                "omni_spill_model": "omni-zaxira", "omni_share": share}
    for k, v in settings.items():
        env.db.set_setting(k, v)
    add_model(env, "slow-agent", upstream="qwen", provider="cluster")
    add_model(env, "gemini-zaxira", upstream="gemini-3.5-flash", provider="pool", listed=0, price_in=1, price_out=1)
    add_model(env, "omni-zaxira", upstream="auto", provider="omni", listed=0, price_in=1, price_out=1)
    key, _ = add_key(env, add_user(env, balance_som=1000))
    return calls, key


def _ask(env, key):
    return env.client.post("/v1/chat/completions", headers=bearer(key),
                           json={"model": "slow-agent", "messages": [{"role": "user", "content": "hello there"}]})


def test_busy_model_spills_to_omniroute_when_no_pool(env):
    calls, key = _env(env, qwen_free=False)
    r = _ask(env, key)
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "from-omni"
    assert calls["cluster"] == 0 and calls["omni"] == 1
    assert calls["omni_auth"] == "Bearer omni-key" and calls["omni_model"] == "auto"
    row = env.db.one("SELECT model, cost, attribution FROM usage_logs")
    meta = json.loads(row["attribution"])
    assert row["model"] == "slow-agent" and meta["route_model"] == "omni-zaxira" and meta["route_reason"] == "capacity"
    assert row["cost"] == 1000 * 10_000 + 100 * 30_000          # the requested model's price
    assert r.json()["model"] == "slow-agent"                     # the client never sees OmniRoute


def test_pool_still_comes_first(env):
    calls, key = _env(env, qwen_free=False, pool=True)
    assert _ask(env, key).json()["choices"][0]["message"]["content"] == "from-pool"
    assert calls["omni"] == 0


def test_failed_model_without_fallback_retries_on_omniroute(env):
    calls, key = _env(env, cluster_status=503)
    r = _ask(env, key)
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "from-omni"
    assert calls["cluster"] >= 1 and calls["omni"] == 1
    assert r.headers.get("x-fallback-model") == "omni-zaxira"


def test_without_omniroute_the_error_passes_through(env):
    calls, key = _env(env, cluster_status=503, omni=False)
    assert _ask(env, key).status_code == 503 and calls["omni"] == 0


def test_omniroute_is_never_listed_and_its_key_is_never_returned(env):
    _env(env)
    ids = [m["id"] for m in env.client.get("/portal/models").json()["items"]]
    assert "omni-zaxira" not in ids
    login_admin(env)
    s = env.client.get("/portal/admin/settings").json()
    assert s["omni_api_key_set"] is True and "omni-key" not in json.dumps(s)


def test_requests_alternate_between_the_two_sources(env):
    calls, key = _env(env, share="50")
    answers = [_ask(env, key).json()["choices"][0]["message"]["content"] for _ in range(4)]
    assert answers == ["from-cluster", "from-omni", "from-cluster", "from-omni"]
    reasons = [json.loads(r["attribution"]).get("route_reason") for r in env.db.all("SELECT attribution FROM usage_logs ORDER BY id")]
    assert reasons.count("balance") == 2
    costs = {r["cost"] for r in env.db.all("SELECT cost FROM usage_logs WHERE status_code=200 AND attribution LIKE '%balance%'")}
    assert costs == {1000 * 10_000 + 100 * 30_000}              # OmniRoute's turn at the requested model's price


def test_share_zero_keeps_everything_on_the_cluster(env):
    calls, key = _env(env, share="0")
    for _ in range(3):
        _ask(env, key)
    assert calls["omni"] == 0 and calls["cluster"] == 3


def test_omniroute_failure_goes_back_to_the_browser_model(env):
    calls, key = _env(env, share="100", omni_status=503)
    r = _ask(env, key)
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "from-cluster"
    assert calls["omni"] == 1 and calls["cluster"] == 1


def test_attachments_never_go_to_omniroute(env):
    calls, key = _env(env, share="100")
    r = env.client.post("/v1/chat/completions", headers=bearer(key), json={"model": "slow-agent", "messages": [
        {"role": "user", "content": [{"type": "text", "text": "what is this"},
                                     {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}}]}]})
    assert calls["omni"] == 0 and calls["cluster"] == 1, r.text
