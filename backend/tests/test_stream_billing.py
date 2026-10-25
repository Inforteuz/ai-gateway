"""A stream that fails before the model says anything is not billed.

Measured 2026-10-25: during a long silence the cluster sends one reasoning
chunk of its own ("[gateway] waiting for the model..."). The gateway counted it
as model output, so two streams that ended in upstream_failed were logged as
successful and charged the full prompt price (~515 so'm each).
"""
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from .conftest import add_model, add_user, add_key, bearer, wallet

ROLE = 'data: {"choices":[{"index":0,"delta":{"role":"assistant"}}]}\n\n'
WAIT = 'data: {"choices":[{"index":0,"delta":{"reasoning_content":"[gateway] waiting for the model..."}}]}\n\n'
FAIL = 'data: {"error":{"message":"The model did not return an answer.","type":"server_error","code":"upstream_failed"}}\n\n'
DONE = 'data: [DONE]\n\n'


def _cluster(env, events):
    fake = FastAPI()

    @fake.post("/v1/chat/completions")
    async def chat(request: Request):
        async def body():
            for e in events:
                yield e
        return StreamingResponse(body(), media_type="text/event-stream")
    env.app.state.cluster_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=fake))
    env.db.set_setting("cluster_base_url", "http://cluster/v1")
    env.db.set_setting("fastpath_enabled", "0")
    add_model(env, "agent", provider="cluster", upstream="qwen")
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    return uid, key


def _ask(env, key):
    return env.client.post("/v1/chat/completions", headers=bearer(key), json={
        "model": "agent", "stream": True, "messages": [{"role": "user", "content": "x" * 4000}]})


def test_failed_stream_with_only_the_waiting_note_is_free(env):
    uid, key = _cluster(env, [ROLE, WAIT, FAIL, DONE])
    before = wallet(env, uid)
    r = _ask(env, key)
    assert '"upstream_failed"' in r.text
    row = env.db.one("SELECT status_code, cost, error FROM usage_logs ORDER BY id DESC LIMIT 1")
    assert row["status_code"] == 502 and row["cost"] == 0
    assert row["error"].startswith("stream_upstream_error")
    assert wallet(env, uid) == before


def test_real_partial_output_is_still_billed(env):
    # Not a blanket exemption: what the model did write is paid for.
    text = 'data: {"choices":[{"index":0,"delta":{"content":"' + "word " * 200 + '"}}]}\n\n'
    uid, key = _cluster(env, [ROLE, WAIT, text, FAIL, DONE])
    _ask(env, key)
    row = env.db.one("SELECT status_code, cost FROM usage_logs ORDER BY id DESC LIMIT 1")
    assert row["status_code"] == 200 and row["cost"] > 0
