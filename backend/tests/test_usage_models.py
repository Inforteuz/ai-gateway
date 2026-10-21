"""Panel: per-model usage and the speed metric cover every model."""
import time

from .conftest import CSRF, add_user


def _log(env, uid, model, pt, ct, cost, status=200, latency=1200):
    with env.db.tx() as c:
        c.execute("INSERT INTO usage_logs(request_id, user_id, key_id, model, endpoint, prompt_tokens, completion_tokens, cost,"
                  " status_code, stream, estimated, latency_ms, created_at) VALUES (?,?,NULL,?,'chat/completions',?,?,?,?,0,0,?,?)",
                  (f"r{time.time_ns()}", uid, model, pt, ct, cost, status, latency, int(time.time())))


def test_usage_by_model_groups_old_names_and_is_per_user(env):
    from .conftest import add_model
    add_model(env, "model-4.0", provider="cluster")
    uid = add_user(env, "u1@test.uz")
    other = add_user(env, "u2@test.uz")
    _log(env, uid, "model-1.0", 100, 50, 0)
    _log(env, uid, "model-1.0", 10, 5, 0)
    _log(env, uid, "model-4.0", 1000, 500, 7000)
    _log(env, other, "model-4.0", 9999, 9999, 99)   # another user — must not show up
    assert env.client.post("/portal/auth/login", json={"email": "u1@test.uz", "password": "parol-12345"}, headers=CSRF).status_code == 200
    items = {m["model"]: m for m in env.client.get("/portal/usage/models?days=30").json()["items"]}
    assert set(items) == {"model-1.0", "model-4.0"}
    assert items["model-1.0"]["requests"] == 2 and items["model-1.0"]["prompt_tokens"] == 110
    assert items["model-4.0"]["completion_tokens"] == 500 and items["model-4.0"]["cost"] == 7000


def test_speed_shown_for_every_model(env):
    from app.routes_account import _real_speed
    from .conftest import add_model
    add_model(env, "model-3.0")
    uid = add_user(env, "s@test.uz")
    for _ in range(6):
        _log(env, uid, "model-3.0", 10, 10, 0, latency=900)
    assert "model-3.0" in _real_speed(env.db)          # previously only models from a hand-written list were counted


def test_usage_by_model_hides_unknown_and_internal_names(env):
    from .conftest import add_model
    add_model(env, "model-4.0", provider="cluster")
    uid = add_user(env, "u3@test.uz")
    _log(env, uid, "model-4.0", 10, 10, 100)
    _log(env, uid, "gpt-4", 0, 0, 0, status=404)               # nonexistent model — rejected
    _log(env, uid, "nonexistent-benchmark-model", 0, 0, 0, status=404)
    _log(env, uid, "qwen3:14b", 30, 20, 0)                    # internal name — must not leak
    assert env.client.post("/portal/auth/login", json={"email": "u3@test.uz", "password": "parol-12345"}, headers=CSRF).status_code == 200
    items = {m["model"]: m for m in env.client.get("/portal/usage/models?days=30").json()["items"]}
    assert set(items) == {"model-4.0", "boshqa"}
    assert items["boshqa"]["prompt_tokens"] == 30


def test_empty_messages_rejected_before_model(env):
    from .conftest import add_key, bearer
    uid = add_user(env, "e@test.uz", balance_som=1000)
    key, _ = add_key(env, uid)
    r = env.client.post("/v1/chat/completions", json={"model": "model-1.0", "messages": []}, headers=bearer(key))
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_messages"


def test_public_health_down_after_consecutive_server_errors(env):
    from app import cluster_watch
    from .conftest import add_model
    add_model(env, "model-3.5", provider="cluster", upstream="deepseek")
    with env.db.tx() as c:
        c.execute("INSERT INTO model_health(upstream, state, since, last_check, detail) VALUES ('deepseek','up',1,2,NULL)")
    uid = add_user(env, "h@test.uz")
    for _ in range(3):
        _log(env, uid, "model-3.5", 0, 0, 0, status=502)
    st = {x["model"]: x["state"] for x in cluster_watch.public_status(env.db, "up")["items"]}
    assert st["model-3.5"] == "down"
    _log(env, uid, "model-3.5", 10, 10, 0, status=200)          # one success — "up" again
    st = {x["model"]: x["state"] for x in cluster_watch.public_status(env.db, "up")["items"]}
    assert st["model-3.5"] == "up"
