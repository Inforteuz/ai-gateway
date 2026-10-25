"""Routing must improve availability without changing privacy or overcharging."""
import json
from dataclasses import replace
import httpx
import pytest
from fastapi import FastAPI, Request

from app import routing
from app.common import ApiError
from app.gateway import _stand_in_for
from cluster.adapters import get_adapter
from .conftest import add_model, add_user, add_key, bearer, wallet


def model(mid, upstream="gemini", provider="cluster", refine=0, **kw):
    return dict(id=mid, upstream_name=upstream, provider=provider, refine=refine,
                enabled=1, price_in_per_1m=100, price_out_per_1m=200, **kw)


def body(text="x" * 32503, **kw):
    return {"messages": [{"role": "user", "content": text}], **kw}


def test_size_route_uses_adapter_limits_including_protocol(monkeypatch):
    source, wide = model("source"), model("wide", "qwen")
    selected, meta = routing.size_target(source, [wide], body())
    assert selected == wide and meta["source_input_limit"] == get_adapter("gemini").MAX_INPUT_CHARS
    assert 32503 < meta["prompt_chars"] <= get_adapter("qwen").MAX_INPUT_CHARS
    monkeypatch.setattr(get_adapter("gemini"), "MAX_INPUT_CHARS", meta["prompt_chars"])
    assert routing.size_target(source, [wide], body()) is None


def test_size_route_does_not_use_narrower_unavailable_or_incapable_models(monkeypatch):
    source, wide = model("source"), model("wide", "qwen")
    assert routing.size_target(source, [model("narrow", "deepseek")], body()) is None
    assert routing.size_target(source, [{**wide, "enabled": 0}], body()) is None
    assert routing.size_target(source, [wide], body("x" * 110000)) is None
    monkeypatch.setattr(get_adapter("qwen"), "caps", replace(get_adapter("qwen").caps, tool_calling=False))
    assert routing.size_target(source, [wide], body(tools=[{"type": "function", "function": {"name": "read_file"}}])) is None


def test_attachments_and_private_requests_keep_original_route():
    assert routing.text_profile(body([{"type": "text", "text": "x" * 40000}, {"type": "image_url", "image_url": {"url": "https://example.com/p.png"}}])) is None
    assert routing.size_target(model("local", provider="local"), [model("wide", "qwen")], body()) is None
    private, partner = model("local", provider="local"), model("wide", "qwen")
    assert not routing.compatible(private, partner)
    assert routing.compatible({**private, "refine": 1}, partner)
    # A wide text field must not remove a requested image-generation capability.
    assert routing.size_target(model("gemini"), [partner], body("draw a cat " + "x" * 32000)) is None


def test_fallback_distinguishes_provider_capacity_from_key_limits():
    source = model("source", provider="local", refine=1, fallback_to="backup")
    target = model("backup", "qwen")
    assert _stand_in_for(source, ApiError(503, "server_busy", "busy"), target) == "backup"
    assert _stand_in_for(source, ApiError(503, "server_busy", "busy"), {**target, "provider": "local"}) is None
    for code in ("too_many_concurrent", "rate_limit_exceeded", "free_daily_limit"):
        assert _stand_in_for(source, ApiError(429, code, "limit"), target) is None
    assert _stand_in_for({**source, "refine": 0}, ApiError(503, "server_busy", "busy"), target) is None
    # Fast-path greetings use the local pool even when the catalog model is cluster.
    cluster = {**source, "provider": "cluster", "refine": 0}
    assert _stand_in_for(cluster, ApiError(503, "server_busy", "busy"), target, actual_provider="local") == "backup"


def setup(env):
    seen = []
    fake = FastAPI()
    @fake.post("/v1/chat/completions")
    async def chat(request: Request):
        payload = await request.json()
        seen.append(payload)
        if payload.get("stream"):
            from fastapi.responses import StreamingResponse
            async def stream():
                yield 'data: {"choices":[{"delta":{"content":"done"}}]}\n\n'
                yield 'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":5}}\n\n'
                yield 'data: [DONE]\n\n'
            return StreamingResponse(stream(), media_type="text/event-stream")
        return {"model": payload["model"], "choices": [{"message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    env.app.state.cluster_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=fake))
    env.db.set_setting("cluster_base_url", "http://cluster/v1")
    env.db.set_setting("fastpath_enabled", "0")
    uid = add_user(env, balance_som=1000)
    key, kid = add_key(env, uid)
    return seen, uid, key, kid


@pytest.mark.parametrize("stream", [False, True])
def test_gateway_size_route_price_and_attribution(env, stream):
    seen, uid, key, _ = setup(env)
    add_model(env, "sized", provider="cluster", upstream="gemini", price_in=1000000, price_out=2000000)
    add_model(env, "wide", provider="cluster", upstream="qwen", price_in=9000000, price_out=18000000)
    before = wallet(env, uid)[0]
    response = env.client.post("/v1/chat/completions", headers=bearer(key), json={"model": "sized", **body(stream=stream)})
    assert response.status_code == 200 and response.headers["x-sf-routed-model"] == "wide"
    assert response.headers["x-sf-route-reason"] == "input_size"
    assert seen[0]["model"] == "qwen"
    if not stream:
        assert response.json()["model"] == "sized"
    row = env.db.one("SELECT * FROM usage_logs ORDER BY id DESC LIMIT 1")
    meta = json.loads(row["attribution"])
    assert row["model"] == "sized" and meta["route_model"] == "wide" and meta["requested_model"] == "sized"
    assert row["estimated"] == 1  # Browser token counts are character estimates, not native tokenizer usage.
    assert before - wallet(env, uid)[0] == row["cost"] == 20
    assert wallet(env, uid)[1] == 0 and not env.db.all("SELECT * FROM inference_slots")


def test_busy_provider_falls_back_once_and_releases_slots(env, monkeypatch):
    import asyncio
    seen, uid, key, _ = setup(env)
    add_model(env, "checked", provider="local", refine=1, price_in=1000, price_out=2000)
    add_model(env, "backup", provider="cluster", upstream="qwen", price_in=9000, price_out=18000)
    with env.db.tx() as c:
        c.execute("UPDATE models SET fallback_to='backup' WHERE id='checked'")
    env.app.state.config = replace(env.app.state.config, queue_wait_seconds=.05)
    env.app.state.sem = asyncio.Semaphore(0)
    response = env.client.post("/v1/chat/completions", headers=bearer(key), json={"model": "checked", **body("explain this code")})
    assert response.status_code == 200 and response.headers["x-fallback-model"] == "backup"
    assert len(seen) == 1
    rows = env.db.all("SELECT * FROM usage_logs ORDER BY id")
    assert [r["status_code"] for r in rows] == [503, 200]
    assert json.loads(rows[1]["attribution"])["route_reason"] == "provider_capacity"
    assert wallet(env, uid)[1] == 0 and not env.db.all("SELECT * FROM inference_slots")


def test_key_limit_never_falls_back(env):
    seen, _, key, kid = setup(env)
    add_model(env, "checked", provider="local", refine=1)
    add_model(env, "backup", provider="cluster", upstream="qwen")
    with env.db.tx() as c:
        c.execute("UPDATE models SET fallback_to='backup' WHERE id='checked'")
    env.app.state.inflight[kid] = env.app.state.config.per_key_concurrent
    response = env.client.post("/v1/chat/completions", headers=bearer(key), json={"model": "checked", **body("explain this code")})
    assert response.status_code == 429 and not seen


def test_gateway_does_not_accept_client_cache_namespace(env):
    seen, _, key, _ = setup(env)
    add_model(env, "normal", provider="cluster", upstream="gemini")
    response = env.client.post("/v1/chat/completions", headers=bearer(key), json={"model": "normal", **body("normal request"),
        "sf_public_model": "another-user-model", "sf_model_policy": {"injected": True}})
    assert response.status_code == 200
    assert seen[0]["sf_public_model"] == "normal" and "injected" not in seen[0]["sf_model_policy"]


def test_private_fallback_does_not_disclose_prompt(env):
    seen, _, key, _ = setup(env)
    add_model(env, "private", provider="local", refine=0)
    add_model(env, "backup", provider="cluster", upstream="qwen")
    with env.db.tx() as c:
        c.execute("UPDATE models SET fallback_to='backup' WHERE id='private'")
    env.up["mode"] = "500"
    response = env.client.post("/v1/chat/completions", headers=bearer(key), json={"model": "private", **body("confidential request")})
    assert response.status_code == 502 and not seen
