import asyncio
import json

import httpx

from app import billing
from app.gateway import Ticket, _stream_body
from .conftest import (CHAT, IN_PER_TOKEN, OUT_PER_TOKEN, SOM, add_key, add_user, bearer, reserve, wallet)

COST_11_7 = 11 * IN_PER_TOKEN + 7 * OUT_PER_TOKEN  # 320 000 micro-som = 0.32 som


def test_non_stream_charges_actual_tokens(env):
    uid = add_user(env, balance_som=1000)
    key, key_id = add_key(env, uid)
    r = env.client.post("/v1/chat/completions", json=CHAT, headers=bearer(key))
    assert r.status_code == 200
    assert r.json()["usage"]["prompt_tokens"] == 11
    bal, held = wallet(env, uid)
    assert held == 0
    assert bal == 1000 * SOM - COST_11_7
    log = env.db.one("SELECT * FROM usage_logs WHERE user_id=?", (uid,))
    assert (log["prompt_tokens"], log["completion_tokens"], log["cost"], log["status_code"]) == (11, 7, COST_11_7, 200)
    led = env.db.one("SELECT * FROM ledger WHERE user_id=? AND kind='usage'", (uid,))
    assert led["amount"] == -COST_11_7 and led["balance_after"] == bal and led["usage_id"] == log["id"]
    assert r.headers["x-sf-cost"] == "0.32" and r.headers["x-request-id"].startswith("req_")


def test_stream_billed_from_usage_and_usage_chunk_hidden_when_not_requested(env):
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    with env.client.stream("POST", "/v1/chat/completions", json={**CHAT, "stream": True}, headers=bearer(key)) as r:
        body = "".join(r.iter_text())
    assert r.status_code == 200
    assert '"usage"' not in body and "[DONE]" in body and "Sa" in body
    assert env.up["last"]["stream_options"]["include_usage"] is True  # we requested it from the model side
    bal, held = wallet(env, uid)
    assert (bal, held) == (1000 * SOM - COST_11_7, 0)


def test_stream_usage_chunk_kept_when_client_asked(env):
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    payload = {**CHAT, "stream": True, "stream_options": {"include_usage": True}}
    with env.client.stream("POST", "/v1/chat/completions", json=payload, headers=bearer(key)) as r:
        body = "".join(r.iter_text())
    assert '"completion_tokens": 7' in body


def test_stream_without_usage_falls_back_to_estimate(env):
    env.up["no_usage"] = True
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    with env.client.stream("POST", "/v1/chat/completions", json={**CHAT, "stream": True}, headers=bearer(key)) as r:
        "".join(r.iter_text())
    log = env.db.one("SELECT * FROM usage_logs WHERE user_id=?", (uid,))
    assert log["estimated"] == 1 and log["completion_tokens"] >= 1 and log["cost"] > 0
    assert wallet(env, uid)[1] == 0


def test_insufficient_balance_rejected_before_calling_model(env):
    uid = add_user(env, balance_som=0)
    key, _ = add_key(env, uid)
    r = env.client.post("/v1/chat/completions", json=CHAT, headers=bearer(key))
    assert r.status_code == 402 and r.json()["error"]["code"] == "insufficient_balance"
    assert env.up.get("calls", 0) == 0
    # the rejected request shows up in the report but does not touch money or the ledger
    log = env.db.one("SELECT * FROM usage_logs")
    assert (log["status_code"], log["category"], log["error"], log["cost"]) == (402, "billing", "insufficient_balance", 0)
    assert r.headers["x-request-id"] == log["request_id"]
    assert env.db.scalar("SELECT COUNT(*) FROM ledger WHERE kind='usage'") == 0


def test_model_error_is_not_charged_and_hold_released(env):
    env.up["mode"] = "500"
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    r = env.client.post("/v1/chat/completions", json=CHAT, headers=bearer(key))
    assert r.status_code == 502
    assert wallet(env, uid) == (1000 * SOM, 0)
    log = env.db.one("SELECT * FROM usage_logs")
    assert log["cost"] == 0 and log["status_code"] == 502
    assert env.db.scalar("SELECT COUNT(*) FROM ledger WHERE kind='usage'") == 0


def test_model_rejecting_request_returns_400_without_charge(env):
    env.up["mode"] = "400"
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    r = env.client.post("/v1/chat/completions", json=CHAT, headers=bearer(key))
    assert r.status_code == 400 and "context length" in r.json()["error"]["message"]
    assert wallet(env, uid) == (1000 * SOM, 0)


def test_offline_model_gives_503_without_charge(env):
    def boom(request):
        raise httpx.ConnectError("refused")
    env.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    r = env.client.post("/v1/chat/completions", json=CHAT, headers=bearer(key))
    assert r.status_code == 503 and r.json()["error"]["code"] == "model_offline"
    assert r.headers["retry-after"]
    assert wallet(env, uid) == (1000 * SOM, 0)


def test_auth_errors(env):
    uid = add_user(env, balance_som=1000)
    key, key_id = add_key(env, uid)
    assert env.client.post("/v1/chat/completions", json=CHAT).status_code == 401
    assert env.client.post("/v1/chat/completions", json=CHAT, headers=bearer("sk-sf-" + "x" * 40)).status_code == 401
    env.client.post("/portal/auth/login", json={"email": "a@test.uz", "password": "parol-12345"}, headers={"X-SF-CSRF": "1"})
    assert env.client.post(f"/portal/keys/{key_id}/revoke", headers={"X-SF-CSRF": "1"}).status_code == 200
    r = env.client.post("/v1/chat/completions", json=CHAT, headers=bearer(key))
    assert r.status_code == 401 and r.json()["error"]["code"] == "revoked_api_key"


def test_suspended_user_blocked(env):
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    with env.db.tx() as c:
        c.execute("UPDATE users SET status='suspended' WHERE id=?", (uid,))
    assert env.client.post("/v1/chat/completions", json=CHAT, headers=bearer(key)).status_code == 403


def test_rate_limit_per_key(env):
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid, rpm=2)
    codes = [env.client.post("/v1/chat/completions", json=CHAT, headers=bearer(key)).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_max_tokens_always_bounded(env):
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    env.client.post("/v1/chat/completions", json=CHAT, headers=bearer(key))
    assert env.up["last"]["max_tokens"] == 4096                      # not given: default
    env.client.post("/v1/chat/completions", json={**CHAT, "max_tokens": 10**9}, headers=bearer(key))
    assert env.up["last"]["max_tokens"] == 16384                     # too large: upper bound
    env.client.post("/v1/chat/completions", json={**CHAT, "max_completion_tokens": 50}, headers=bearer(key))
    assert env.up["last"]["max_tokens"] == 50 and "max_completion_tokens" not in env.up["last"]
    assert env.client.post("/v1/chat/completions", json={**CHAT, "max_tokens": -5}, headers=bearer(key)).status_code == 400


def test_request_validation(env):
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    h = bearer(key)
    assert env.client.post("/v1/chat/completions", json={**CHAT, "model": "gpt-4"}, headers=h).status_code == 404
    assert env.client.post("/v1/chat/completions", json={"model": "model-1.0"}, headers=h).status_code == 400
    assert env.client.post("/v1/chat/completions", content=b"not json", headers=h).status_code == 400
    assert env.client.post("/v1/chat/completions", content=b"[1,2]", headers=h).status_code == 400
    assert env.up.get("calls", 0) == 0


def test_free_key_logged_but_not_charged(env):
    uid = add_user(env, balance_som=0)
    key, _ = add_key(env, uid, free=1)
    r = env.client.post("/v1/chat/completions", json=CHAT, headers=bearer(key))
    assert r.status_code == 200
    assert wallet(env, uid) == (0, 0)
    log = env.db.one("SELECT * FROM usage_logs")
    assert log["prompt_tokens"] == 11 and log["cost"] == 0


def test_monthly_key_limit(env):
    uid = add_user(env, balance_som=1000)
    # the 0.32 som already charged + the next request's hold must not exceed the limit
    key, _ = add_key(env, uid, monthly=1 * SOM)
    r = env.client.post("/v1/chat/completions", json={**CHAT, "max_tokens": 10}, headers=bearer(key))
    assert r.status_code == 200
    r = env.client.post("/v1/chat/completions", json={**CHAT, "max_tokens": 100000}, headers=bearer(key))
    assert r.status_code == 429 and r.json()["error"]["code"] == "key_limit_reached"


def test_balance_and_models_endpoints(env):
    uid = add_user(env, balance_som=500)
    key, _ = add_key(env, uid)
    r = env.client.get("/v1/balance", headers=bearer(key))
    assert r.json()["balance"] == 500.0 and r.json()["currency"] == "UZS"
    m = env.client.get("/v1/models", headers=bearer(key)).json()
    assert m["data"][0]["id"] == "model-1.0" and m["data"][0]["pricing"]["input_per_1m_tokens"] == 10000.0


# --- billing core ----------------------------------------------------------------------------------------------
def test_reserve_cannot_overdraw(env):
    uid = add_user(env, balance_som=10)
    _, key_id = add_key(env, uid)
    reserve(env, uid, key_id, 6 * SOM, request_id="req_a")
    try:
        reserve(env, uid, key_id, 6 * SOM, request_id="req_b")
        assert False, "the second reserve should have been rejected"
    except billing.BillingError as e:
        assert e.code == "insufficient_balance"
    billing.release(env.db, request_id="req_a", user_id=uid)
    billing.release(env.db, request_id="req_a", user_id=uid)   # idempotent
    assert wallet(env, uid) == (10 * SOM, 0)


def test_cost_rounds_up():
    assert billing.cost_for(1, 10_000 * SOM) == 10_000
    assert billing.cost_for(0, 10_000 * SOM) == 0
    assert billing.cost_for(1, 1) == 1  # even the smallest price never rounds down to 0


def test_settle_is_atomic_and_finish_is_idempotent(env):
    uid = add_user(env, balance_som=100)
    _, key_id = add_key(env, uid)
    key = {"id": key_id, "user_id": uid, "free": 0}
    t = Ticket(state=env.app.state, request_id="req_x", key=key, model_id="model-1.0", endpoint="chat/completions",
               price_in=10_000 * SOM, price_out=30_000 * SOM, reserved=0, stream=False, ip="1.1.1.1", t0=0.0, est_prompt=5)

    async def run():
        a = await t.finish(status_code=200, prompt_tokens=100, completion_tokens=100)
        b = await t.finish(status_code=200, prompt_tokens=100, completion_tokens=100)
        return a, b

    a, b = asyncio.run(run())
    assert a is not None and b is None
    assert env.db.scalar("SELECT COUNT(*) FROM usage_logs") == 1
    assert wallet(env, uid)[0] == 100 * SOM - (100 * IN_PER_TOKEN + 100 * OUT_PER_TOKEN)


def test_client_disconnect_mid_stream_bills_generated_tokens(env):
    uid = add_user(env, balance_som=100)
    _, key_id = add_key(env, uid)
    reserve(env, uid, key_id, 5 * SOM, request_id="req_d")
    key = {"id": key_id, "user_id": uid, "free": 0}
    t = Ticket(state=env.app.state, request_id="req_d", key=key, model_id="model-1.0", endpoint="chat/completions",
               price_in=10_000 * SOM, price_out=30_000 * SOM, reserved=5 * SOM, stream=True, ip="1.1.1.1", t0=0.0, est_prompt=20)

    class FakeResp:
        async def aiter_lines(self):
            yield 'data: ' + json.dumps({"choices": [{"delta": {"content": "x" * 50}}]})
            yield ""
            await asyncio.sleep(10)  # the client disconnects here

        async def aclose(self):
            pass

    async def run():
        gen = _stream_body(FakeResp(), t, False, "model-1.0")
        await gen.__anext__()
        await gen.__anext__()
        task = asyncio.ensure_future(gen.__anext__())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except BaseException:
            pass
        await gen.aclose()

    asyncio.run(run())
    log = env.db.one("SELECT * FROM usage_logs WHERE request_id='req_d'")
    assert log["estimated"] == 1 and log["completion_tokens"] == 20 and log["error"] == "client_disconnected"
    bal, held = wallet(env, uid)
    assert held == 0 and bal == 100 * SOM - log["cost"] and log["cost"] > 0


def test_concurrent_reserves_never_overdraw(env):
    """20 threads try to reserve 3 som at once with a 10 som balance: exactly 3 must succeed."""
    import threading
    uid = add_user(env, balance_som=10)
    _, key_id = add_key(env, uid)
    ok, fail = [], []
    barrier = threading.Barrier(20)

    def worker():
        barrier.wait()
        try:
            reserve(env, uid, key_id, 3 * SOM)
            ok.append(1)
        except billing.BillingError:
            fail.append(1)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(ok) == 3 and len(fail) == 17
    assert wallet(env, uid) == (10 * SOM, 9 * SOM)


def test_oversized_body_rejected_before_reading(env):
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    big = {"model": "model-1.0", "messages": [{"role": "user", "content": "x" * (env.cfg.max_body_bytes + 10)}]}
    r = env.client.post("/v1/chat/completions", json=big, headers=bearer(key))
    assert r.status_code == 413 and env.up.get("calls", 0) == 0


def set_key_effort(env, key_id, value):
    with env.db.tx() as c:
        c.execute("UPDATE api_keys SET reasoning_effort=? WHERE id=?", (value, key_id))


def test_reasoning_precedence_request_over_key_over_global(env):
    uid = add_user(env, balance_som=1000)
    key, key_id = add_key(env, uid)
    h = bearer(key)
    chat = lambda extra=None: env.client.post("/v1/chat/completions", json={**CHAT, **(extra or {})}, headers=h)  # noqa: E731

    chat()
    assert env.up["last"]["reasoning_effort"] == "low"                         # global default: low
    set_key_effort(env, key_id, "none")
    chat()
    assert env.up["last"]["reasoning_effort"] == "none"                        # the key setting beats the global one
    chat({"reasoning_effort": "high"})
    assert env.up["last"]["reasoning_effort"] == "high"                        # the request beats the key
    chat({"think": True})
    assert "reasoning_effort" not in env.up["last"] and env.up["last"]["think"] is True
    set_key_effort(env, key_id, "model")
    chat()
    assert "reasoning_effort" not in env.up["last"]                            # the model decides
    set_key_effort(env, key_id, None)
    env.db.set_setting("default_reasoning_effort", "")
    chat()
    assert "reasoning_effort" not in env.up["last"]                            # neither on the key nor globally
    env.db.set_setting("default_reasoning_effort", "medium")
    chat()
    assert env.up["last"]["reasoning_effort"] == "medium"
    # the completions path is not affected
    env.client.post("/v1/chat/completions", json=CHAT, headers=h)


def test_applied_reasoning_is_logged_per_request(env):
    uid = add_user(env, balance_som=1000)
    key, key_id = add_key(env, uid)
    h = bearer(key)
    env.client.post("/v1/chat/completions", json=CHAT, headers=h)
    set_key_effort(env, key_id, "none")
    env.client.post("/v1/chat/completions", json=CHAT, headers=h)
    env.client.post("/v1/chat/completions", json={**CHAT, "reasoning_effort": "high"}, headers=h)
    env.client.post("/v1/chat/completions", json={**CHAT, "think": True}, headers=h)
    rows = [r["reasoning"] for r in env.db.all("SELECT reasoning FROM usage_logs ORDER BY id")]
    assert rows == ["low", "none", "high", "think"]


def test_key_reasoning_setting_via_user_and_admin_api(env):
    add_user(env, "admin@test.uz", role="admin")
    uid = add_user(env, "u@test.uz")
    csrf = {"X-SF-CSRF": "1"}
    login = lambda e: env.client.post("/portal/auth/login", json={"email": e, "password": "parol-12345"}, headers=csrf)  # noqa: E731
    login("u@test.uz")
    r = env.client.post("/portal/keys", json={"name": "bot", "reasoning_effort": "none"}, headers=csrf).json()
    kid = r["item"]["id"]
    assert r["item"]["reasoning_effort"] == "none"
    assert env.client.post("/portal/keys", json={"name": "x"}, headers=csrf).json()["item"]["reasoning_effort"] == "inherit"
    assert env.client.patch(f"/portal/keys/{kid}", json={"reasoning_effort": "high"}, headers=csrf).json()["item"]["reasoning_effort"] == "high"
    assert env.client.patch(f"/portal/keys/{kid}", json={"name": "bot2"}, headers=csrf).json()["item"]["reasoning_effort"] == "high"  # other edits leave it alone
    assert env.client.patch(f"/portal/keys/{kid}", json={"reasoning_effort": "inherit"}, headers=csrf).json()["item"]["reasoning_effort"] == "inherit"
    assert env.client.patch(f"/portal/keys/{kid}", json={"reasoning_effort": "turbo"}, headers=csrf).status_code == 422
    env.client.post("/portal/auth/logout", headers=csrf)
    login("admin@test.uz")
    assert env.client.patch(f"/portal/admin/keys/{kid}", json={"reasoning_effort": "medium"}, headers=csrf).json()["item"]["reasoning_effort"] == "medium"
    assert env.client.get("/portal/admin/settings").json()["default_reasoning_effort"] == "low"
    assert env.client.put("/portal/admin/settings", json={"default_reasoning_effort": "high"}, headers=csrf).json()["default_reasoning_effort"] == "high"
    assert env.client.put("/portal/admin/settings", json={"default_reasoning_effort": ""}, headers=csrf).json()["default_reasoning_effort"] == ""
    assert env.client.put("/portal/admin/settings", json={"default_reasoning_effort": "turbo"}, headers=csrf).status_code == 422


# --- stream guards ----------------------------------------------------------------------------------------------
def _stalling_transport(first_chunk: bool):
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            if first_chunk:
                yield b'data: {"model": "qwen3:14b", "choices": [{"delta": {"content": "Salom"}}]}\n\n'
            raise httpx.ReadTimeout("went silent")
    return httpx.MockTransport(lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream()))


def test_stalled_stream_is_closed_and_slot_released(env):
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    env.app.state.http = httpx.AsyncClient(transport=_stalling_transport(first_chunk=True))
    with env.client.stream("POST", "/v1/chat/completions", json={**CHAT, "stream": True}, headers=bearer(key)) as r:
        raw = "".join(r.iter_text())
    assert "stream_stalled" in raw and "Salom" in raw
    log = env.db.one("SELECT * FROM usage_logs")
    assert log["error"] == "stream_stalled" and log["estimated"] == 1 and wallet(env, uid)[1] == 0     # billed for what was sent, the hold is released
    assert env.app.state.sem._value == env.cfg.max_concurrent and not env.app.state.open_tickets          # the slot is returned
    # if nothing arrives at all: an unserved request (model/server problem), nothing is charged
    env.app.state.http = httpx.AsyncClient(transport=_stalling_transport(first_chunk=False))
    with env.client.stream("POST", "/v1/chat/completions", json={**CHAT, "stream": True}, headers=bearer(key)) as r:
        "".join(r.iter_text())
    last = env.db.one("SELECT * FROM usage_logs ORDER BY id DESC LIMIT 1")
    assert last["category"] == "platform" and last["error"] == "stream_stalled" and last["cost"] == 0


def test_tool_call_tokens_are_counted_when_usage_is_missing(env):
    env.up["tool_stream"] = True
    env.up["no_usage"] = True
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    with env.client.stream("POST", "/v1/chat/completions", json={**CHAT, "stream": True}, headers=bearer(key)) as r:
        "".join(r.iter_text())
    log = env.db.one("SELECT * FROM usage_logs")
    assert log["status_code"] == 200 and log["estimated"] == 1 and log["completion_tokens"] >= 1 and log["cost"] > 0


def test_stream_longer_than_limit_is_cut(env):
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    object.__setattr__(env.app.state.config, "max_stream_seconds", -1)        # the limit counts as exceeded after every chunk
    with env.client.stream("POST", "/v1/chat/completions", json={**CHAT, "stream": True}, headers=bearer(key)) as r:
        raw = "".join(r.iter_text())
    assert "stream_too_long" in raw and "[DONE]" not in raw
    assert env.db.one("SELECT error FROM usage_logs")["error"] == "stream_too_long"


# --- repetition loop ------------------------------------------------------------------------------------------------
def test_loop_detector_catches_exact_repetition_only():
    from app import loops
    assert loops.is_looping("Boshlanish. " + "Salom dunyo. " * 150)
    assert loops.is_looping("x" * 3500)
    assert not loops.is_looping("Salom dunyo. " * 8)                                   # short: not a loop
    assert not loops.is_looping("| a | b |\n|---|---|\n" * 5 + "Oddiy matn. " * 3)
    assert not loops.is_looping("0, " * 300)                                           # list of short items (900 chars): happens in valid answers too
    assert not loops.is_looping("1, 2, 3 " * 5 + "0, " * 400)                         # array of zeros (1200 chars)
    assert loops.is_looping("0, " * 1100)                                              # 3300 chars of the same pattern: a loop
    import random
    rnd = random.Random(1)
    assert not loops.is_looping(" ".join(rnd.choice(["bu", "oddiy", "matn", "so'zlar", "tasodifiy", "tartib"]) for _ in range(400)))


def test_looping_stream_is_cut_early_with_clean_ending_and_estimated_bill(env):
    env.up["loop_stream"] = True
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    with env.client.stream("POST", "/v1/chat/completions", json={**CHAT, "stream": True, "stream_options": {"include_usage": True}}, headers=bearer(key)) as r:
        raw = "".join(r.iter_text())
    events = [json.loads(line[5:]) for line in raw.splitlines() if line.startswith("data:") and "[DONE]" not in line]
    text = "".join((e["choices"][0]["delta"].get("content") or "") for e in events if e["choices"])
    assert 500 < len(text) < 2500 < 500 * len("Salom dunyo. ")                       # stopped well before 6500 chars
    assert [e["choices"][0].get("finish_reason") for e in events if e["choices"] and e["choices"][0].get("finish_reason")] == ["length"]
    assert raw.rstrip().endswith("[DONE]") and events[-1]["usage"]["completion_tokens"] >= 1
    log = env.db.one("SELECT * FROM usage_logs")
    assert log["error"] == "repetition_loop" and log["estimated"] == 1 and log["status_code"] == 200 and log["cost"] > 0
    assert env.app.state.sem._value == env.cfg.max_concurrent and not env.app.state.open_tickets


def test_ollama_timings_are_not_forwarded(env):
    env.up["usage"] = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18, "timings": {"prompt_per_second": 2426.2}}
    from app.gateway import _scrub
    assert "timings" not in _scrub({"model": "qwen3:14b", "timings": {"x": 1}}, "sf-fast")
