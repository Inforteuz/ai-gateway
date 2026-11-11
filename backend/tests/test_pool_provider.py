"""Provider "pool": an OpenAI-compatible key pool on the gateway's own network.

Separate from "external", which the answer verifier (refine) keeps using, so the
two can point at different services at the same time.
"""
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .conftest import CSRF, add_key, add_model, add_user, bearer, wallet
from .test_models_refine import login_admin


def _pool(env, mode="ok"):
    seen = []
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        payload = await request.json()
        seen.append({"url": str(request.url), "auth": request.headers.get("authorization"), "body": payload})
        if mode == "down":
            return JSONResponse({"error": {"message": "no key answered"}}, status_code=503)
        if payload.get("stream"):
            async def events():
                yield 'data: {"choices":[{"index":0,"delta":{"content":"pong"}}]}\n\n'
                yield 'data: {"choices":[],"usage":{"prompt_tokens":120,"completion_tokens":30,"total_tokens":150}}\n\n'
                yield "data: [DONE]\n\n"
            return StreamingResponse(events(), media_type="text/event-stream")
        return {"model": payload["model"], "choices": [{"index": 0, "message": {"role": "assistant", "content": "pong"},
                                                         "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}}

    env.app.state.ext_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gemini-pool")
    env.db.set_setting("pool_base_url", "http://gemini-pool:8088/v1")
    env.db.set_setting("pool_api_key", "pool-token")
    add_model(env, "fast-gemini", upstream="gemini-3.5-flash", provider="pool")
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    return seen, uid, key


def _chat(env, key, **extra):
    return env.client.post("/v1/chat/completions", headers=bearer(key),
                           json={"model": "fast-gemini", "messages": [{"role": "user", "content": "ping"}], **extra})


def test_request_reaches_the_pool_with_its_token_and_upstream_name(env):
    seen, _, key = _pool(env)
    r = _chat(env, key)
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "pong"
    assert seen[0]["url"].endswith("/v1/chat/completions") and seen[0]["auth"] == "Bearer pool-token"
    assert seen[0]["body"]["model"] == "gemini-3.5-flash"
    row = env.db.one("SELECT prompt_tokens, completion_tokens, estimated, cost FROM usage_logs")
    assert (row["prompt_tokens"], row["completion_tokens"], row["estimated"]) == (120, 30, 0) and row["cost"] > 0


def test_stream_asks_for_exact_usage_and_bills_it(env):
    seen, _, key = _pool(env)
    r = _chat(env, key, stream=True)
    assert r.status_code == 200 and "pong" in r.text
    assert seen[0]["body"]["stream_options"]["include_usage"] is True
    assert '"usage"' not in r.text                    # the client did not ask for the usage chunk
    row = env.db.one("SELECT prompt_tokens, completion_tokens, estimated FROM usage_logs")
    assert (row["prompt_tokens"], row["completion_tokens"], row["estimated"]) == (120, 30, 0)


def test_without_a_token_the_model_is_unavailable(env):
    seen, uid, key = _pool(env)
    env.db.set_setting("pool_api_key", "")
    before = wallet(env, uid)
    r = _chat(env, key)
    assert r.status_code == 503 and not seen and wallet(env, uid) == before


def test_a_failing_pool_falls_back(env):
    seen, _, key = _pool(env, mode="down")
    with env.db.tx() as c:
        c.execute("UPDATE models SET fallback_to='model-1.0' WHERE id='fast-gemini'")
    r = _chat(env, key)
    assert r.status_code == 200 and len(seen) == 1             # one pool attempt, then the stand-in
    assert r.headers.get("x-fallback-model") == "model-1.0"


def test_pool_serves_only_chat_completions(env):
    _, _, key = _pool(env)
    r = env.client.post("/v1/completions", headers=bearer(key), json={"model": "fast-gemini", "prompt": "hi"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "unsupported_endpoint"


def test_admin_stores_the_pool_token_write_only(env):
    login_admin(env)
    put = env.client.put("/portal/admin/settings", headers=CSRF,
                         json={"pool_base_url": "http://gemini-pool:8088/v1", "pool_api_key": "secret-pool-token"})
    assert put.status_code == 200
    got = env.client.get("/portal/admin/settings").json()
    assert got["pool_base_url"] == "http://gemini-pool:8088/v1" and got["pool_api_key_set"] is True
    assert "secret-pool-token" not in str(got)
    env.client.put("/portal/admin/settings", headers=CSRF, json={"clear_pool_api_key": True})
    assert env.client.get("/portal/admin/settings").json()["pool_api_key_set"] is False


def test_admin_can_create_a_pool_model(env):
    login_admin(env)
    r = env.client.post("/portal/admin/models", headers=CSRF, json={
        "id": "pool-test", "upstream_name": "gemini-3-flash-preview", "display_name": "Pool test",
        "provider": "pool", "price_in_per_1m": 1, "price_out_per_1m": 1})
    assert r.status_code in (200, 201), r.text
    assert env.db.one("SELECT provider FROM models WHERE id='pool-test'")["provider"] == "pool"
