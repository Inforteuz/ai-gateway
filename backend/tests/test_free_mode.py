import json

from .conftest import CHAT, CSRF, SOM, add_key, add_model, add_user, bearer, configure_external, wallet


def chat(env, key, model="model-1.0", **extra):
    return env.client.post("/v1/chat/completions", json={**CHAT, "model": model, **extra}, headers=bearer(key))


# --- free mode -----------------------------------------------------------------------------------------------
def test_free_mode_needs_no_balance_and_charges_nothing(env):
    env.db.set_setting("free_mode", "1")
    uid = add_user(env, balance_som=0)
    key, _ = add_key(env, uid)                                   # regular (not free) key, empty balance
    r = chat(env, key, max_tokens=16000)                         # even the worst-case amount is 0
    assert r.status_code == 200 and r.headers["x-sf-cost"] == "0"
    assert wallet(env, uid) == (0, 0) and env.db.scalar("SELECT COUNT(*) FROM ledger") == 0
    log = env.db.one("SELECT * FROM usage_logs")
    assert (log["prompt_tokens"], log["completion_tokens"], log["cost"]) == (11, 7, 0)   # tokens are still recorded
    bal = env.client.get("/v1/balance", headers=bearer(key)).json()
    assert bal["unlimited"] is True and bal["free_mode"] is True
    m = env.client.get("/v1/models", headers=bearer(key)).json()["data"][0]["pricing"]
    assert m["free"] is True and m["input_per_1m_tokens"] == 0 and m["output_per_1m_tokens"] == 0
    assert env.client.get("/portal/models").json()["free_mode"] is True
    assert env.client.get("/portal/auth/me").json()["free_mode"] is True


def test_paid_mode_unchanged_when_free_mode_off(env):
    uid = add_user(env, balance_som=0)
    key, _ = add_key(env, uid)
    assert chat(env, key).status_code == 402
    assert env.client.get("/portal/auth/me").json()["free_mode"] is False


def test_free_mode_keeps_checked_model_price_zero_on_fallback(env):
    env.db.set_setting("free_mode", "1")
    uid = add_user(env, balance_som=0)
    key, _ = add_key(env, uid)
    add_model(env, "sf-fast")
    add_model(env, "sf-pro", refine=1, fallback="sf-fast", price_in=20_000 * SOM, price_out=60_000 * SOM)
    r = chat(env, key, "sf-pro")                                 # no external key: fallback path
    assert r.status_code == 200 and r.json()["gateway"]["verified"] is False
    assert env.db.one("SELECT cost FROM usage_logs")["cost"] == 0


def test_admin_can_toggle_free_mode(env):
    add_user(env, "admin@test.uz", role="admin")
    env.client.post("/portal/auth/login", json={"email": "admin@test.uz", "password": "parol-12345"}, headers=CSRF)
    assert env.client.get("/portal/admin/settings").json()["free_mode"] is False
    assert env.client.put("/portal/admin/settings", json={"free_mode": True}, headers=CSRF).json()["free_mode"] is True
    assert env.db.get_settings()["free_mode"] == "1"


# --- retry when reasoning eats the budget --------------------------------------------------------------------------
def test_starved_empty_answer_is_retried_without_thinking_and_only_retry_is_billed(env):
    env.up["starve"] = True
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    r = chat(env, key, max_tokens=500)                           # default "low" -> empty answer -> retry
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "Salom"
    assert env.up["calls"] == 2 and env.up["last"]["reasoning_effort"] == "none"
    assert wallet(env, uid) == (1000 * SOM - (11 * 10_000 + 7 * 30_000), 0)    # the first (500-token) attempt is not billed
    log = env.db.one("SELECT * FROM usage_logs")
    assert env.db.scalar("SELECT COUNT(*) FROM usage_logs") == 1 and log["reasoning"] == "none" and log["error"] == "auto_retry_no_thinking"
    assert (log["prompt_tokens"], log["completion_tokens"]) == (11, 7)


def test_no_retry_when_client_chose_reasoning_or_answer_not_empty(env):
    env.up["starve"] = True
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    r = chat(env, key, max_tokens=500, reasoning_effort="low")   # chosen by the client: respected
    assert env.up["calls"] == 1 and r.json()["choices"][0]["message"]["content"] == ""
    env.up.pop("starve")
    chat(env, key)                                               # not empty: no retry
    assert env.up["calls"] == 2


def test_retry_also_applies_to_checked_model_draft(env):
    env.up["starve"] = True
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    add_model(env, "sf-pro", refine=1)
    configure_external(env)
    env.ext["reply"] = "Tekshirilgan javob."
    r = chat(env, key, "sf-pro")
    assert r.json()["choices"][0]["message"]["content"] == "Tekshirilgan javob." and r.json()["gateway"]["verified"] is True
    assert env.up["calls"] == 2


# --- quality samples ----------------------------------------------------------------------------------------------
def test_samples_admin_replace_and_public_read(env):
    sample = {"category": "Tarjima", "prompt": "Salom", "fast_model": "sf-fast", "fast_answer": "A", "fast_ms": 1200,
              "pro_model": "sf-pro", "pro_answer": "B", "pro_ms": 5400, "pro_verified": True}
    assert env.client.put("/portal/admin/samples", json=[sample], headers=CSRF).status_code == 401
    add_user(env, "admin@test.uz", role="admin")
    env.client.post("/portal/auth/login", json={"email": "admin@test.uz", "password": "parol-12345"}, headers=CSRF)
    assert env.client.put("/portal/admin/samples", json=[sample, {**sample, "category": "Kod"}], headers=CSRF).json()["count"] == 2
    assert env.client.put("/portal/admin/samples", json=[sample] * 21, headers=CSRF).status_code == 400
    env.client.post("/portal/auth/logout", headers=CSRF)
    pub = env.client.get("/portal/samples").json()                 # public, no login
    assert [i["category"] for i in pub["items"]] == ["Tarjima", "Kod"] and pub["items"][0]["pro"]["verified"] is True and pub["generated_at"]
    env.client.post("/portal/auth/login", json={"email": "admin@test.uz", "password": "parol-12345"}, headers=CSRF)
    env.client.put("/portal/admin/samples", json=[sample], headers=CSRF)
    assert len(env.client.get("/portal/samples").json()["items"]) == 1   # replaced
