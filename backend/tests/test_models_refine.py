import json

import httpx

from .conftest import CHAT, CSRF, SOM, add_key, add_model, add_user, bearer, configure_external, wallet


def chat(env, key, model, **extra):
    return env.client.post("/v1/chat/completions", json={**CHAT, "model": model, **extra}, headers=bearer(key))


def sse(env, key, model, **extra):
    with env.client.stream("POST", "/v1/chat/completions", json={**CHAT, "model": model, "stream": True, **extra}, headers=bearer(key)) as r:
        raw = "".join(r.iter_text())
    events = [json.loads(line[5:]) for line in raw.splitlines() if line.startswith("data:") and "[DONE]" not in line]
    return r, raw, events


# --- name hiding (white-label) ----------------------------------------------------------------------------------
def test_alias_hides_real_model_everywhere(env):
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    add_model(env, "sf-lite", display="Gateway Lite")
    env.db.conn().execute("UPDATE models SET listed=0 WHERE id='model-1.0'")
    # the list
    lst = env.client.get("/v1/models", headers=bearer(key)).json()["data"]
    assert [m["id"] for m in lst] == ["sf-lite"] and "qwen3" not in json.dumps(lst)
    assert "qwen3" not in env.client.get("/portal/models").text
    # the response (plain and streamed)
    r = chat(env, key, "sf-lite")
    assert r.json()["model"] == "sf-lite" and "system_fingerprint" not in r.json() and env.up["last"]["model"] == "qwen3:14b"
    _, raw, events = sse(env, key, "sf-lite")
    assert "qwen3" not in raw and "fp_ollama" not in raw and all(e["model"] == "sf-lite" for e in events)
    # the unknown-model error does not reveal the real name
    err = chat(env, key, "gpt-4").json()["error"]["message"]
    assert "sf-lite" in err and "qwen3" not in err
    # an unlisted model still works when called (for old clients)
    assert chat(env, key, "model-1.0").status_code == 200


def test_model_error_message_does_not_leak_upstream_name(env):
    env.up["mode"] = "400"
    env.up["usage"] = None
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    add_model(env, "sf-lite")
    # the mock returns "context length exceeded"; we check even though it has no name in it
    r = chat(env, key, "sf-lite")
    assert r.status_code == 400 and "qwen3" not in r.text


def test_public_model_metadata_is_truthful_and_has_no_internal_fields(env):
    env.db.conn().execute("UPDATE models SET listed=0 WHERE id='model-1.0'")
    add_model(env, "sf-lite")
    add_model(env, "sf-lite-checked", refine=1)
    add_model(env, "sf-cloud", provider="external", upstream="posiden/gpt-6-luna")
    items = {m["id"]: m for m in env.client.get("/portal/models").json()["items"]}
    assert items["sf-lite"]["data_processing"] == "private" and items["sf-lite"]["capabilities"]["reasoning_levels"] == ["none", "low", "medium", "high"]
    # Verification is conditional: the catalog does not advertise it as a guarantee
    checked = items["sf-lite-checked"]
    assert checked["data_processing"] == "third_party" and checked["capabilities"]["verified_answers"] is False
    assert checked["capabilities"]["conditional_verification"] is True
    assert checked["verification"]["mode"] == "conditional" and checked["verification"]["response_field"] == "gateway.verified"
    assert "verification" not in items["sf-lite"] and items["sf-lite"]["capabilities"]["conditional_verification"] is False
    assert items["sf-cloud"]["data_processing"] == "third_party" and items["sf-cloud"]["capabilities"]["reasoning_levels"] == []
    blob = json.dumps(items)
    for secret in ("upstream_name", "qwen3", "posiden", "provider", "parameter"):
        assert secret not in blob


# --- external provider model --------------------------------------------------------------------------------------
def test_external_model_routes_to_provider_and_rewrites_names(env):
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    add_model(env, "sf-cloud", provider="external", upstream="posiden/gpt-6-luna")
    assert chat(env, key, "sf-cloud").status_code == 503                      # no key configured
    assert chat(env, key, "sf-cloud").json()["error"]["code"] == "model_unavailable"
    assert wallet(env, uid) == (1000 * SOM, 0)
    configure_external(env)
    r = chat(env, key, "sf-cloud")
    assert r.status_code == 200 and r.json()["model"] == "sf-cloud" and "fp_ext" not in r.text
    assert env.ext["last"]["model"] == "posiden/gpt-6-luna" and env.ext["auth"] == "Bearer ext-secret-key"
    assert "reasoning_effort" not in env.ext["last"]                           # the default effort is not applied to external models
    assert env.up.get("calls", 0) == 0                                         # local Ollama was not touched
    bal, held = wallet(env, uid)
    assert held == 0 and bal == 1000 * SOM - (20 * 10_000 + 9 * 30_000)        # charged from the external usage
    _, raw, events = sse(env, key, "sf-cloud")
    assert "posiden" not in raw and all(e["model"] == "sf-cloud" for e in events if "model" in e)
    assert chat(env, key, "sf-cloud").status_code == 200
    assert env.client.post("/v1/completions", json={"model": "sf-cloud", "prompt": "x"}, headers=bearer(key)).status_code == 400


# --- verified model -----------------------------------------------------------------------------------------------
def test_refined_answer_replaces_draft_and_bills_draft_tokens(env):
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    add_model(env, "sf-lite-checked", refine=1)
    configure_external(env)
    env.up["draft"] = "Bulut hisoblash - internet orqali foydalaniish."
    env.ext["reply"] = "Bulut hisoblash internet orqali xizmatlardan foydalanishdir."
    r = chat(env, key, "sf-lite-checked")
    d = r.json()
    assert d["choices"][0]["message"]["content"] == env.ext["reply"] and d["gateway"] == {"verified": True}
    assert r.headers["x-sf-verified"] == "true" and d["model"] == "sf-lite-checked"
    # sent to the external model: system prompt + conversation + draft, with the right key
    sent = env.ext["last"]["messages"]
    assert sent[0]["role"] == "system" and "QORALAMA" in sent[1]["content"] and "Salom" in sent[1]["content"] and env.up["draft"] in sent[1]["content"]
    assert env.ext["last"]["model"] == "posiden/gpt-6-luna" and env.ext["auth"] == "Bearer ext-secret-key"
    # billing: by the local draft tokens (11 in, 7 out)
    assert wallet(env, uid) == (1000 * SOM - (11 * 10_000 + 7 * 30_000), 0)
    log = env.db.one("SELECT * FROM usage_logs")
    assert log["refined"] == "ok" and log["refine_ms"] is not None and log["model"] == "sf-lite-checked"


def test_refined_stream_replays_final_text_as_sse(env):
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    add_model(env, "sf-lite-checked", refine=1)
    configure_external(env)
    env.ext["reply"] = "Bu tekshirilgan javob matni bo'lib, bir necha so'zdan iborat uzunroq gap."
    r, raw, events = sse(env, key, "sf-lite-checked", stream_options={"include_usage": True})
    text = "".join((e["choices"][0]["delta"].get("content") or "") for e in events if e["choices"])
    assert text == env.ext["reply"] and raw.rstrip().endswith("[DONE]") and r.headers["x-sf-verified"] == "true"
    assert events[-1]["usage"]["completion_tokens"] == 7                       # client asked: usage chunk present
    assert all(e["model"] == "sf-lite-checked" for e in events)
    _, raw2, events2 = sse(env, key, "sf-lite-checked")
    assert not any("usage" in e for e in events2)                              # not asked: absent


def test_refine_falls_back_to_draft_on_any_problem(env):
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    add_model(env, "sf-lite-checked", refine=1)
    configure_external(env)
    env.up["draft"] = "A" * 300                                                # long draft (to detect a truncated answer)
    cases = {"500": "http_500", "empty": "empty", "short": "too_short"}
    for mode, reason in cases.items():
        env.ext["mode"] = mode
        r = chat(env, key, "sf-lite-checked")
        assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "A" * 300
        assert r.json()["gateway"]["verified"] is False and r.headers["x-sf-verified"] == "false"
        assert env.db.one("SELECT refined FROM usage_logs ORDER BY id DESC LIMIT 1")["refined"] == f"fallback:{reason}"

    def boom(request):
        raise httpx.ReadTimeout("slow")
    env.app.state.ext_http = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    assert chat(env, key, "sf-lite-checked").json()["gateway"]["verified"] is False
    assert env.db.one("SELECT refined FROM usage_logs ORDER BY id DESC LIMIT 1")["refined"] == "fallback:timeout"
    assert wallet(env, uid)[1] == 0


def test_refine_not_configured_falls_back_and_never_claims_verified(env):
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    add_model(env, "sf-lite-checked", refine=1)
    r = chat(env, key, "sf-lite-checked")                                      # no external key configured
    assert r.status_code == 200 and r.json()["gateway"]["verified"] is False
    assert env.db.one("SELECT refined FROM usage_logs")["refined"] == "fallback:not_configured"
    assert env.ext.get("calls", 0) == 0


def test_refine_skipped_for_tools_json_mode_and_images(env):
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    add_model(env, "sf-lite-checked", refine=1)
    configure_external(env)
    tools = [{"type": "function", "function": {"name": "f", "parameters": {"type": "object", "properties": {}}}}]
    chat(env, key, "sf-lite-checked", tools=tools)
    chat(env, key, "sf-lite-checked", response_format={"type": "json_object"})
    rejected = chat(env, key, "sf-lite-checked", messages=[{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:..."}}]}])
    assert rejected.status_code == 400 and rejected.json()["error"]["code"] == "unsupported_feature"
    env.up["finish"] = "length"
    chat(env, key, "sf-lite-checked")                                          # truncated draft
    assert env.ext.get("calls", 0) == 0                                        # nothing was sent out
    reasons = [r["refined"] for r in env.db.all("SELECT refined FROM usage_logs ORDER BY id")]
    assert reasons == ["fallback:tools", "fallback:json_mode", None, "fallback:truncated_draft"]


def test_fallback_uses_cheaper_sibling_price(env):
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    add_model(env, "sf-lite", price_in=10_000 * SOM, price_out=30_000 * SOM)
    add_model(env, "sf-lite-checked", refine=1, price_in=20_000 * SOM, price_out=60_000 * SOM, fallback="sf-lite")
    configure_external(env)
    chat(env, key, "sf-lite-checked")                                          # verified: 2x price
    ok_cost = env.db.one("SELECT cost FROM usage_logs ORDER BY id DESC LIMIT 1")["cost"]
    env.ext["mode"] = "500"
    chat(env, key, "sf-lite-checked")                                          # fallback: sf-lite price
    fb_cost = env.db.one("SELECT cost FROM usage_logs ORDER BY id DESC LIMIT 1")["cost"]
    assert ok_cost == 2 * fb_cost and fb_cost == 11 * 10_000 + 7 * 30_000


def test_refine_model_rejects_legacy_completions_endpoint(env):
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    add_model(env, "sf-lite-checked", refine=1)
    r = env.client.post("/v1/completions", json={"model": "sf-lite-checked", "prompt": "x"}, headers=bearer(key))
    assert r.status_code == 400 and r.json()["error"]["code"] == "unsupported_endpoint"


def test_model_default_reasoning_precedence(env):
    uid = add_user(env, balance_som=1000)
    key, key_id = add_key(env, uid)
    add_model(env, "sf-fast", default_reasoning="none")
    chat(env, key, "sf-fast")
    assert env.up["last"]["reasoning_effort"] == "none"                        # the model default beats the global "low"
    with env.db.tx() as c:
        c.execute("UPDATE api_keys SET reasoning_effort='high' WHERE id=?", (key_id,))
    chat(env, key, "sf-fast")
    assert env.up["last"]["reasoning_effort"] == "high"                        # the key beats the model
    chat(env, key, "sf-fast", reasoning_effort="medium")
    assert env.up["last"]["reasoning_effort"] == "medium"                      # the request beats everything


# --- admin API ------------------------------------------------------------------------------------------------------
def login_admin(env):
    add_user(env, "admin@test.uz", role="admin")
    env.client.post("/portal/auth/login", json={"email": "admin@test.uz", "password": "parol-12345"}, headers=CSRF)


def test_admin_model_crud_and_rules(env):
    login_admin(env)
    body = {"id": "sf-lite", "upstream_name": "qwen3:14b", "display_name": "Gateway Lite", "price_in_per_1m": 10 * SOM,
            "price_out_per_1m": 30 * SOM, "default_reasoning": "none", "listed": True}
    assert env.client.post("/portal/admin/models", json=body, headers=CSRF).status_code == 200
    assert env.client.post("/portal/admin/models", json={**body, "id": "sf-ext", "provider": "external", "refine": True}, headers=CSRF).status_code == 400
    assert env.client.post("/portal/admin/models", json={**body, "id": "sf-chk", "refine": True, "fallback_price_from": "nope"}, headers=CSRF).status_code == 400
    assert env.client.post("/portal/admin/models", json={**body, "id": "sf-chk", "refine": True, "fallback_price_from": "sf-lite"}, headers=CSRF).status_code == 200
    # editing leaves other fields alone; default_reasoning is cleared with an empty value
    env.client.patch("/portal/admin/models/sf-lite", json={"listed": False}, headers=CSRF)
    row = env.db.one("SELECT * FROM models WHERE id='sf-lite'")
    assert row["listed"] == 0 and row["default_reasoning"] == "none" and row["display_name"] == "Gateway Lite"
    env.client.patch("/portal/admin/models/sf-lite", json={"default_reasoning": None}, headers=CSRF)
    assert env.db.one("SELECT default_reasoning FROM models WHERE id='sf-lite'")["default_reasoning"] is None
    admin_list = {m["id"]: m for m in env.client.get("/portal/admin/models").json()["items"]}
    assert admin_list["sf-lite"]["upstream_name"] == "qwen3:14b" and admin_list["sf-chk"]["refine"] is True   # the admin sees the internal name


def test_external_settings_never_expose_key_and_test_endpoint(env):
    login_admin(env)
    assert env.client.post("/portal/admin/external/test", headers=CSRF).status_code == 400            # no key
    s = env.client.put("/portal/admin/settings", json={"external_api_key": "sk-topsecret", "external_base_url": "https://example.test/v1",
                                                       "refiner_model": "posiden/gpt-6-luna", "refine_timeout": 30}, headers=CSRF).json()
    assert s["external_api_key_set"] is True and "topsecret" not in json.dumps(s) and "topsecret" not in env.client.get("/portal/admin/settings").text
    assert env.client.put("/portal/admin/settings", json={"external_base_url": "http://insecure.test"}, headers=CSRF).status_code == 422
    env.client.put("/portal/admin/settings", json={"external_api_key": ""}, headers=CSRF)                # an empty field does not clear the key
    assert env.db.get_settings()["external_api_key"] == "sk-topsecret"
    env.db.set_setting("external_base_url", "http://external/v1")
    r = env.client.post("/portal/admin/external/test", headers=CSRF).json()
    assert r["ok"] and r["models"] == 2 and r["refiner_found"] is True and env.ext["models_auth"] == "Bearer sk-topsecret"
    names = [m["name"] for m in env.client.get("/portal/admin/external/models", headers=CSRF).json()["items"]]
    assert "posiden/gpt-6-luna" in names
    env.ext["bad_key"] = True
    assert env.client.post("/portal/admin/external/test", headers=CSRF).json()["error"]["code"] == "external_bad_key"
    env.client.put("/portal/admin/settings", json={"clear_external_api_key": True}, headers=CSRF)
    assert env.db.get_settings()["external_api_key"] == ""


def test_short_correct_refinement_of_long_bad_draft_is_accepted_but_truncated_is_not(env):
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    add_model(env, "sf-lite-checked", refine=1)
    configure_external(env)
    env.up["draft"] = "Ma'nosiz uzun qoralama. " * 40                          # a long, bad draft
    env.ext["reply"] = "Hurmatli mijoz, buyurtmangiz kechikkani uchun uzr so'raymiz."   # correct but much shorter
    r = chat(env, key, "sf-lite-checked")
    assert r.json()["gateway"]["verified"] is True and r.json()["choices"][0]["message"]["content"] == env.ext["reply"]
    env.ext["finish"] = "length"                                               # the verifier's answer is truncated
    r = chat(env, key, "sf-lite-checked")
    assert r.json()["gateway"]["verified"] is False
    assert env.db.one("SELECT refined FROM usage_logs ORDER BY id DESC LIMIT 1")["refined"] == "fallback:truncated"


def test_refiner_race_second_model_wins_when_first_is_slow_or_fails(env, monkeypatch):
    import asyncio
    from app import refine
    monkeypatch.setattr(refine, "HEDGE_AFTER", 0.05)
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    add_model(env, "sf-pro", refine=1)
    configure_external(env, model="slow-a,fast-b")
    from .conftest import make_external

    ext = make_external(env.ext)
    @ext.middleware("http")
    async def slow_a(request, call_next):
        if request.method == "POST":
            body = json.loads(await request.body())
            env.ext.setdefault("seen", []).append(body["model"])
            if body["model"] == "slow-a":
                await asyncio.sleep(1.5)
        return await call_next(request)
    env.ext["reply"] = "Javob tezroq modeldan."
    env.app.state.ext_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=ext), base_url="http://external")
    import time
    t = time.time()
    r = chat(env, key, "sf-pro")
    assert r.json()["gateway"]["verified"] is True and r.json()["choices"][0]["message"]["content"] == "Javob tezroq modeldan."
    assert time.time() - t < 1.2 and env.ext["seen"] == ["slow-a", "fast-b"]       # the slow model was not awaited, both were asked


def test_refiner_race_falls_through_when_first_fails_fast(env):
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    add_model(env, "sf-pro", refine=1)
    configure_external(env, model="a,b")
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body["model"])
        if body["model"] == "a":
            return httpx.Response(500)
        return httpx.Response(200, json={"choices": [{"message": {"content": "B javobi"}, "finish_reason": "stop"}], "usage": {}})
    env.app.state.ext_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    r = chat(env, key, "sf-pro")
    assert r.json()["choices"][0]["message"]["content"] == "B javobi" and calls == ["a", "b"] and r.json()["gateway"]["verified"] is True
    calls.clear()
    env.db.set_setting("refiner_model", "a")
    assert chat(env, key, "sf-pro").json()["gateway"]["verified"] is False       # the only model fails: the draft is returned
    assert env.db.one("SELECT refined FROM usage_logs ORDER BY id DESC LIMIT 1")["refined"] == "fallback:http_500"


def test_rate_limited_refiner_is_skipped_until_cooldown_ends(env, monkeypatch):
    from app import refine
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    add_model(env, "sf-pro", refine=1)
    configure_external(env, model="limited,works")
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body["model"])
        if body["model"] == "limited":
            return httpx.Response(429, json={"error": {"message": "free model daily limit exceeded for limited (50/day)", "type": "rate_limit_error"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "Works javobi"}, "finish_reason": "stop"}], "usage": {}})
    env.app.state.ext_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    r = chat(env, key, "sf-pro")
    assert r.json()["gateway"]["verified"] is True and calls == ["limited", "works"]       # the 429 is detected the first time, the second model answers
    calls.clear()
    r = chat(env, key, "sf-pro")
    assert r.json()["gateway"]["verified"] is True and calls == ["works"]                  # the rate-limited model is no longer asked
    refine.COOLDOWN["limited"] = 0                                                            # cooldown over: it is tried again
    calls.clear()
    chat(env, key, "sf-pro")
    assert calls == ["limited", "works"]


def test_all_refiners_rate_limited_returns_draft_with_clear_reason(env):
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    add_model(env, "sf-pro", refine=1)
    configure_external(env, model="only")
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429, json={"error": {"message": "free model daily limit exceeded for only (50/day)"}})
    env.app.state.ext_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    r = chat(env, key, "sf-pro")
    assert r.status_code == 200 and r.json()["gateway"]["verified"] is False and len(calls) == 1
    assert env.db.one("SELECT refined FROM usage_logs ORDER BY id DESC LIMIT 1")["refined"] == "fallback:rate_limited"
    chat(env, key, "sf-pro")
    assert len(calls) == 1                                                                    # no calls to the provider at all during the cooldown
    assert env.db.one("SELECT refined FROM usage_logs ORDER BY id DESC LIMIT 1")["refined"] == "fallback:rate_limited"


# --- streamed verification ----------------------------------------------------------------------------------------
def _setup_pro(env, **ext):
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    add_model(env, "sf-pro", refine=1)
    configure_external(env)
    env.ext.update(ext)
    return uid, key


def test_streamed_refinement_bills_draft_tokens_and_frees_slots(env):
    uid, key = _setup_pro(env, reply="Bu tekshirilgan javob matni bo'lib, bir necha so'zdan iborat.")
    r, raw, events = sse(env, key, "sf-pro", stream_options={"include_usage": True})
    text = "".join((e["choices"][0]["delta"].get("content") or "") for e in events if e["choices"])
    assert text == env.ext["reply"] and r.headers["x-sf-verified"] == "true" and raw.rstrip().endswith("[DONE]")
    assert events[-1]["usage"]["completion_tokens"] == 7 and all(e["model"] == "sf-pro" for e in events)       # billed by the draft tokens
    log = env.db.one("SELECT * FROM usage_logs")
    assert log["refined"] == "ok" and log["error"] is None and log["completion_tokens"] == 7 and log["refine_ms"] is not None
    assert wallet(env, uid) == (1000 * SOM - (11 * 10_000 + 7 * 30_000), 0)
    assert env.app.state.ext_sem._value == env.cfg.ext_concurrent and env.app.state.sem._value == env.cfg.max_concurrent
    assert not env.app.state.open_tickets


def test_stream_refiner_failure_before_first_chunk_falls_back_to_draft(env):
    uid, key = _setup_pro(env)
    for mode in ("500", "empty"):
        env.ext["mode"] = mode
        r, raw, events = sse(env, key, "sf-pro")
        text = "".join((e["choices"][0]["delta"].get("content") or "") for e in events if e["choices"])
        assert text == "Salom" and r.headers["x-sf-verified"] == "false" and raw.rstrip().endswith("[DONE]")
    assert [x["refined"] for x in env.db.all("SELECT refined FROM usage_logs ORDER BY id")] == ["fallback:http_500", "fallback:empty"]
    assert env.app.state.ext_sem._value == env.cfg.ext_concurrent


def test_stream_refiner_cut_mid_stream_reports_error_and_still_settles(env):
    uid, key = _setup_pro(env)

    class Cut(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices": [{"delta": {"content": "Boshi "}}]}\n\n'
            raise httpx.ReadError("disconnected")
    env.app.state.ext_http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Cut())))
    r, raw, events = sse(env, key, "sf-pro")
    assert r.headers["x-sf-verified"] == "true" and "stream_interrupted" in raw and "Boshi" in raw
    log = env.db.one("SELECT * FROM usage_logs")
    assert log["refined"] == "partial:stream_interrupted" and log["status_code"] == 200 and log["completion_tokens"] == 7
    assert env.app.state.ext_sem._value == env.cfg.ext_concurrent and not env.app.state.open_tickets


def test_stream_refiner_rate_limit_uses_next_model_and_cooldown(env):
    from app import refine
    uid, key = _setup_pro(env, reply="Ikkinchi modeldan javob keldi.")
    env.db.set_setting("refiner_model", "limited,works")
    seen = []

    def handler(request):
        m = json.loads(request.content)["model"]
        seen.append(m)
        if m == "limited":
            return httpx.Response(429, json={"error": {"message": "free model daily limit exceeded for limited (50/day)"}})
        body = 'data: {"choices": [{"delta": {"content": "Works javobi"}}]}\n\ndata: [DONE]\n\n'
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())
    env.app.state.ext_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    r, raw, events = sse(env, key, "sf-pro")
    assert "Works javobi" in raw and r.headers["x-sf-verified"] == "true" and seen == ["limited", "works"]
    sse(env, key, "sf-pro")
    assert seen == ["limited", "works", "works"] and "limited" in refine.COOLDOWN


# --- tools / JSON mode: the draft is not replayed in the stream, function calls are not lost ----------------------
TOOLS = [{"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}]


def _tool_args(events):
    out = ""
    for e in events:
        for ch in e.get("choices") or []:
            for tc in (ch.get("delta") or {}).get("tool_calls") or []:
                out += (tc.get("function") or {}).get("arguments") or ""
    return out


def test_streamed_tool_call_through_verified_model_is_delivered_intact(env):
    env.up["tool_stream"] = True
    uid, key = _setup_pro(env)
    r, raw, events = sse(env, key, "sf-pro", tools=TOOLS, stream_options={"include_usage": True})
    assert _tool_args(events) == '{"city":"Toshkent"}'                       # the function call is not empty
    assert r.headers["x-sf-verified"] == "false" and raw.rstrip().endswith("[DONE]")
    assert env.up["last"]["stream"] is True and "calls" not in env.ext       # streamed straight through; no call to the external provider
    assert all(e["model"] == "sf-pro" for e in events) and events[-1]["usage"]["completion_tokens"] == 7
    log = env.db.one("SELECT * FROM usage_logs")
    assert log["refined"] == "fallback:tools" and log["status_code"] == 200 and log["stream"] == 1 and log["completion_tokens"] == 7
    assert env.app.state.sem._value == env.cfg.max_concurrent and not env.app.state.open_tickets


def test_streamed_tool_call_is_not_billed_as_empty_when_usage_missing(env):
    env.up["tool_stream"], env.up["no_usage"] = True, True
    uid, key = _setup_pro(env)
    sse(env, key, "sf-pro", tools=TOOLS)
    log = env.db.one("SELECT * FROM usage_logs")
    assert log["estimated"] == 1 and log["completion_tokens"] >= 1 and log["cost"] > 0


def test_streamed_json_mode_goes_straight_through_and_keeps_fallback_price(env):
    uid, key = _setup_pro(env)
    add_model(env, "sf-fast", price_in=1 * SOM, price_out=2 * SOM)
    env.db.conn().execute("UPDATE models SET fallback_price_from='sf-fast' WHERE id='sf-pro'")
    r, raw, events = sse(env, key, "sf-pro", response_format={"type": "json_object"})
    text = "".join((e["choices"][0]["delta"].get("content") or "") for e in events if e["choices"])
    assert r.headers["x-sf-verified"] == "false" and text == "Salom" and "calls" not in env.ext
    log = env.db.one("SELECT * FROM usage_logs")
    assert log["refined"] == "fallback:json_mode" and log["cost"] == 11 * 1 + 7 * 2 != 11 * 10_000 + 7 * 30_000          # billed at the sf-fast price (1 and 2 som/1M), not the sf-pro price


def test_replay_stream_keeps_tool_calls_as_safety_net():
    from app.gateway import _replay_stream
    data = {"id": "x", "model": "sf-pro", "choices": [{"message": {"content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]}, "finish_reason": "tool_calls"}]}
    raw = b"".join(_replay_stream(data, False, (1, 1))).decode()
    events = [json.loads(line[5:]) for line in raw.splitlines() if line.startswith("data:") and "[DONE]" not in line]
    calls = [tc for e in events for tc in (e["choices"][0]["delta"].get("tool_calls") or [])]
    assert calls and calls[0]["function"]["name"] == "f" and calls[0]["index"] == 0 and events[-1]["choices"][0]["finish_reason"] == "tool_calls"


# --- thinking: only on models that allow it --------------------------------------------------------------------------
def test_thinking_is_forced_off_on_models_that_disallow_it(env):
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    add_model(env, "sf-fast", allow_thinking=0)
    for extra in ({"reasoning_effort": "high"}, {"think": True}, {"reasoning": {"effort": "high"}}, {}):
        r = chat(env, key, "sf-fast", **extra)
        assert r.status_code == 200
        last = env.up["last"]
        assert last["reasoning_effort"] == "none" and "think" not in last and "reasoning" not in last, extra
    assert {x["reasoning"] for x in env.db.all("SELECT reasoning FROM usage_logs")} == {"none"}
    # in a stream too, and even with "high" as the key default
    env.db.conn().execute("UPDATE api_keys SET reasoning_effort='high'")
    sse(env, key, "sf-fast", reasoning_effort="high")
    assert env.up["last"]["reasoning_effort"] == "none" and env.up["last"]["stream"] is True


def test_cluster_model_that_disallows_thinking_also_forces_none(env):
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    add_model(env, "sf-cluster-fast", provider="cluster", upstream="gemini", allow_thinking=0)
    env.db.set_setting("cluster_base_url", "http://cluster/v1")
    import httpx
    from fastapi import FastAPI, Request
    fake = FastAPI()

    @fake.post("/v1/chat/completions")
    async def chat(request: Request):
        payload = await request.json()
        env.up["last"] = payload
        return {"model": payload["model"],
                "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    env.app.state.cluster_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=fake))
    response = env.client.post(
        "/v1/chat/completions", headers=bearer(key),
        json={"model": "sf-cluster-fast", "messages": [{"role": "user", "content": "hi"}],
              "reasoning_effort": "high"},
    )
    assert response.status_code == 200 and env.up["last"]["reasoning_effort"] == "none"


def test_thinking_model_obeys_request_key_and_model_default(env):
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    add_model(env, "sf-think", default_reasoning="medium", allow_thinking=1)
    chat(env, key, "sf-think")
    assert env.up["last"]["reasoning_effort"] == "medium"                      # the model default
    chat(env, key, "sf-think", reasoning_effort="high")
    assert env.up["last"]["reasoning_effort"] == "high"                        # what the client asked for
    chat(env, key, "sf-think", reasoning_effort="none")
    assert env.up["last"]["reasoning_effort"] == "none"                        # the client may also turn it off


def test_verified_model_draft_is_also_thinking_free(env):
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    add_model(env, "sf-pro", refine=1, allow_thinking=0)
    configure_external(env)
    r = chat(env, key, "sf-pro", reasoning_effort="high")
    assert r.json()["gateway"]["verified"] is True and env.up["last"]["reasoning_effort"] == "none"


def test_catalog_shows_real_reasoning_levels_and_admin_can_toggle(env):
    admin = add_user(env, "boss@test.uz", role="admin")
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    add_model(env, "sf-fast", allow_thinking=0)
    add_model(env, "sf-think", allow_thinking=1, default_reasoning="medium")
    caps = {m["id"]: m["capabilities"]["reasoning_levels"] for m in env.client.get("/v1/models", headers=bearer(key)).json()["data"]}
    assert caps["sf-fast"] == ["none"] and caps["sf-think"] == ["none", "low", "medium", "high"]
    env.client.post("/portal/auth/login", json={"email": "boss@test.uz", "password": "parol-12345"}, headers=CSRF)
    items = {m["id"]: m for m in env.client.get("/portal/admin/models").json()["items"]}
    assert items["sf-fast"]["allow_thinking"] is False and items["sf-think"]["allow_thinking"] is True
    assert env.client.patch("/portal/admin/models/sf-fast", json={"allow_thinking": True}, headers=CSRF).status_code == 200
    assert env.db.one("SELECT allow_thinking FROM models WHERE id='sf-fast'")["allow_thinking"] == 1
    body = {"id": "sf-new", "upstream_name": "qwen3:14b", "display_name": "New", "price_in_per_1m": 1, "price_out_per_1m": 1, "allow_thinking": False}
    assert env.client.post("/portal/admin/models", json=body, headers=CSRF).status_code == 200
    assert env.db.one("SELECT allow_thinking FROM models WHERE id='sf-new'")["allow_thinking"] == 0
