"""API playground in the panel: session-authenticated, paid from the user's own balance."""
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from .conftest import CSRF, add_key, add_model, add_user, bearer, wallet


def _upstream(env):
    seen = []
    fake = FastAPI()

    @fake.post("/v1/chat/completions")
    async def chat(request: Request):
        body = await request.json()
        seen.append(body)
        if body.get("stream"):
            async def gen():
                yield 'data: {"choices":[{"index":0,"delta":{"content":"Salom"}}]}\n\n'
                yield 'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
                yield 'data: {"choices":[],"usage":{"prompt_tokens":1000,"completion_tokens":100}}\n\n'
                yield "data: [DONE]\n\n"
            return StreamingResponse(gen(), media_type="text/event-stream")
        return {"choices": [{"index": 0, "message": {"role": "assistant", "content": "Salom"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 100}}

    env.app.state.ext_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=fake), base_url="http://gemini-pool")
    for k, v in {"pool_base_url": "http://gemini-pool:8088/v1", "pool_api_key": "t", "pool_spill_model": ""}.items():
        env.db.set_setting(k, v)
    add_model(env, "play-model", upstream="up", provider="pool")
    add_model(env, "hidden-model", upstream="up", provider="pool", listed=0)
    return seen


def _login(env, email="p@test.uz", balance_som=1000):
    uid = add_user(env, email, balance_som=balance_som)
    r = env.client.post("/portal/auth/login", json={"email": email, "password": "parol-12345"}, headers=CSRF)
    assert r.status_code == 200, r.text
    return uid


def _ask(env, **over):
    body = {"model": "play-model", "messages": [{"role": "user", "content": "salom"}], "stream": False, **over}
    return env.client.post("/portal/playground/chat", json=body, headers=CSRF)


def test_playground_answers_and_charges_the_users_balance(env):
    _upstream(env)
    uid = _login(env)
    before = wallet(env, uid)
    r = _ask(env)
    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["message"]["content"] == "Salom"
    rid = r.headers["x-request-id"]
    row = env.db.one("SELECT u.cost, k.kind, k.name FROM usage_logs u JOIN api_keys k ON k.id=u.key_id WHERE u.request_id=?", (rid,))
    assert row["kind"] == "playground" and row["name"] == "Playground" and row["cost"] > 0
    assert wallet(env, uid) == (before[0] - row["cost"], 0)
    rec = env.client.get(f"/portal/playground/receipt/{rid}").json()
    assert rec["ready"] and rec["cost"] == row["cost"] and rec["prompt_tokens"] == 1000 and rec["balance"] == wallet(env, uid)[0]


def test_stream_and_one_key_per_user(env):
    seen = _upstream(env)
    _login(env)
    r = _ask(env, stream=True)
    assert r.status_code == 200 and "Salom" in r.text
    assert seen[-1]["stream"] is True and seen[-1]["stream_options"] == {"include_usage": True}
    _ask(env)
    assert env.db.scalar("SELECT COUNT(*) FROM api_keys WHERE kind='playground'") == 1


def test_needs_a_session_and_the_csrf_header(env):
    _upstream(env)
    body = {"model": "play-model", "messages": [{"role": "user", "content": "x"}]}
    assert env.client.post("/portal/playground/chat", json=body, headers=CSRF).status_code == 401
    _login(env)
    assert env.client.post("/portal/playground/chat", json=body).status_code == 403


def test_playground_key_is_hidden_and_unusable_outside(env):
    _upstream(env)
    uid = _login(env)
    _ask(env)
    assert env.client.get("/portal/keys").json()["items"] == []
    assert env.client.get("/portal/account/summary").json()["active_keys"] == 0
    kid = env.db.scalar("SELECT id FROM api_keys WHERE kind='playground'")
    assert env.client.post(f"/portal/keys/{kid}/revoke", headers=CSRF).status_code == 404
    assert env.client.get(f"/portal/keys/{kid}/reveal").status_code == 404
    # A header cannot pick the playground key; only a real API key authenticates /v1
    r = env.client.post("/v1/chat/completions", headers={"Authorization": "Bearer sk-sf-nothing-at-all", "X-Playground-Key": str(kid)},
                        json={"model": "play-model", "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 401
    key, _ = add_key(env, uid)
    assert env.client.post("/v1/chat/completions", headers=bearer(key),
                           json={"model": "play-model", "messages": [{"role": "user", "content": "x"}]}).status_code == 200


def test_body_is_narrow(env):
    seen = _upstream(env)
    _login(env)
    assert _ask(env, model="hidden-model").status_code == 404
    assert _ask(env, model="no-such").status_code == 404
    assert _ask(env, messages=[{"role": "tool", "content": "x"}]).status_code == 422
    assert _ask(env, messages=[{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]).status_code == 400
    assert _ask(env, max_tokens=100_000).status_code == 422
    assert _ask(env, messages=[{"role": "user", "content": "x" * 31_000}, {"role": "assistant", "content": "y" * 31_000},
                               {"role": "user", "content": "z"}]).status_code == 413
    r = _ask(env, tools=[{"type": "function", "function": {"name": "x"}}])
    assert r.status_code == 200 and "tools" not in seen[-1]    # unknown fields are dropped, never forwarded


def test_empty_balance_is_refused(env):
    _upstream(env)
    _login(env, balance_som=0)
    r = _ask(env)
    assert r.status_code == 402, r.text


def test_receipt_only_reads_own_rows(env):
    _upstream(env)
    _login(env)
    rid = _ask(env).headers["x-request-id"]
    env.client.post("/portal/auth/logout", headers=CSRF)
    _login(env, "other@test.uz")
    assert env.client.get(f"/portal/playground/receipt/{rid}").json() == {"ready": False}
    assert env.client.get("/portal/playground/receipt/not-an-id").status_code == 400
