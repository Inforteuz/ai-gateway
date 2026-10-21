"""Daily token limit for the free model (model-1.0): accounts without a deposit get at most 250 000 tokens a day."""
import uuid

import pytest

from app import billing
from app.db import now
from .conftest import CSRF, SOM, add_key, add_model, add_user, bearer

FREE = "model-1.0"
PAID = "model-2.0"
LIMIT = 250_000
REQ = {"model": FREE, "messages": [{"role": "user", "content": "Salom"}]}   # the mock model returns 11 + 7 = 18 tokens


@pytest.fixture
def free_env(env):
    # As in a typical deployment: model-1.0 is free. The limit settings keep their defaults.
    with env.db.tx() as c:
        c.execute("UPDATE models SET price_in_per_1m=0, price_out_per_1m=0 WHERE id=?", (FREE,))
    env.db.set_setting("free_daily_limit_models", FREE)
    add_model(env, PAID)
    return env


def seed(env, uid, tokens, *, model=FREE, status=200, ts=None):
    """Writes prior usage straight into the log (prompt + completion)."""
    with env.db.tx() as c:
        c.execute("""INSERT INTO usage_logs(request_id, user_id, model, endpoint, prompt_tokens, completion_tokens,
                         status_code, created_at) VALUES (?,?,?,'chat/completions',?,?,?,?)""",
                  ("req_" + uuid.uuid4().hex, uid, model, tokens // 2, tokens - tokens // 2, status, ts or now()))


def user_with_key(env, **kw):
    uid = add_user(env, **kw)
    key, _ = add_key(env, uid)
    return uid, key


def chat(env, key, model=FREE):
    return env.client.post("/v1/chat/completions", json={**REQ, "model": model}, headers=bearer(key))


def test_defaults():
    from app.config import DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS["free_daily_token_limit"] == "250000"
    assert DEFAULT_SETTINGS["free_daily_limit_models"] == ""      # installer sets it


def test_within_limit_passes(free_env):
    uid, key = user_with_key(free_env)
    seed(free_env, uid, 249_000)
    r = chat(free_env, key)
    assert r.status_code == 200
    assert free_env.up["calls"] == 1


def test_crossing_request_allowed_then_blocked(free_env):
    uid, key = user_with_key(free_env)
    seed(free_env, uid, LIMIT - 10)           # 249 990
    assert chat(free_env, key).status_code == 200   # +18 tokens: crosses the limit but is still allowed
    assert billing.free_tokens_used(free_env.db, uid, [FREE]) == LIMIT + 8

    r = chat(free_env, key)
    assert r.status_code == 429
    err = r.json()["error"]
    assert err["type"] == "rate_limit_error" and err["code"] == "free_daily_limit"
    assert "250 000" in err["message"] and "00:00" in err["message"] and "to'ldirsangiz" in err["message"]
    expected = billing.next_day_ts() - now()
    assert abs(int(r.headers["retry-after"]) - expected) <= 2
    assert 0 < int(r.headers["retry-after"]) <= 86400
    assert free_env.up["calls"] == 1          # never reached the model


def test_blocked_request_never_falls_back_to_paid_model(free_env):
    # There is a signup bonus (enough for paid models) — but a blocked request must not switch to another model.
    uid, key = user_with_key(free_env)
    billing.credit(free_env.db, user_id=uid, amount=1000 * SOM, kind="bonus", note="Boshlang'ich mablag'")
    seed(free_env, uid, LIMIT)
    r = chat(free_env, key)
    assert r.status_code == 429 and r.json()["error"]["code"] == "free_daily_limit"
    assert free_env.up.get("calls", 0) == 0
    assert free_env.db.scalar("SELECT COUNT(*) FROM usage_logs WHERE user_id=? AND status_code=200 AND model<>?", (uid, FREE)) == 0
    assert free_env.db.scalar("SELECT COUNT(*) FROM ledger WHERE user_id=? AND kind='usage'", (uid,)) == 0
    # the rejected request shows up as 429 in the report and does not count towards usage
    log = free_env.db.one("SELECT * FROM usage_logs WHERE user_id=? AND status_code=429", (uid,))
    assert log["error"] == "free_daily_limit" and log["model"] == FREE


def test_failed_requests_not_counted(free_env):
    uid, key = user_with_key(free_env)
    for status in (499, 502, 429, 400):
        seed(free_env, uid, LIMIT, status=status)
    assert billing.free_tokens_used(free_env.db, uid, [FREE]) == 0
    assert chat(free_env, key).status_code == 200


@pytest.mark.parametrize("kind", ["bonus", "adjustment", "refund"])
def test_non_deposit_credits_keep_limit(free_env, kind):
    uid, key = user_with_key(free_env)
    billing.credit(free_env.db, user_id=uid, amount=5000 * SOM, kind=kind, note="test")
    seed(free_env, uid, LIMIT)
    assert chat(free_env, key).status_code == 429


def test_deposit_removes_limit(free_env):
    uid, key = user_with_key(free_env)
    seed(free_env, uid, LIMIT + 50_000)
    assert chat(free_env, key).status_code == 429
    billing.credit(free_env.db, user_id=uid, amount=10_000 * SOM, kind="topup", note="To'ldirish so'rovi #1")
    r = chat(free_env, key)
    assert r.status_code == 200 and r.json()["model"] == FREE
    assert billing.free_quota(free_env.db, user_id=uid, settings=free_env.db.get_settings()) is None


def test_approved_topup_request_counts_as_deposit(free_env):
    add_user(free_env, "admin@test.uz", role="admin")
    uid, key = user_with_key(free_env)
    seed(free_env, uid, LIMIT)
    with free_env.db.tx() as c:
        tid = c.execute("INSERT INTO topup_requests(user_id, amount, created_at) VALUES (?,?,?)",
                        (uid, 10_000 * SOM, now())).lastrowid
    free_env.client.post("/portal/auth/login", json={"email": "admin@test.uz", "password": "parol-12345"}, headers=CSRF)
    assert free_env.client.post(f"/portal/admin/topups/{tid}/resolve", json={"action": "approve"}, headers=CSRF).status_code == 200
    assert chat(free_env, key).status_code == 200


def test_new_tashkent_day_resets(free_env):
    uid, key = user_with_key(free_env)
    seed(free_env, uid, LIMIT * 3, ts=billing.day_start_ts() - 1)   # yesterday 23:59:59 (Tashkent)
    assert billing.free_tokens_used(free_env.db, uid, [FREE]) == 0
    assert chat(free_env, key).status_code == 200


def test_tashkent_day_boundaries():
    # 2026-10-20 18:59:59 UTC = 23:59:59 in Tashkent; midnight is at 19:00:00 UTC
    ts = 1792522799
    assert billing.next_day_ts(ts) == ts + 1
    assert billing.day_start_ts(ts + 1) == ts + 1
    assert billing.day_start_ts(ts) == ts + 1 - 86400


def test_other_models_unaffected(free_env):
    uid, key = user_with_key(free_env, balance_som=0)
    billing.credit(free_env.db, user_id=uid, amount=1000 * SOM, kind="bonus", note="sovg'a")
    seed(free_env, uid, LIMIT * 2)
    assert chat(free_env, key, PAID).status_code == 200     # paid model: no limit
    # usage of another model does not count towards the free quota
    uid2, key2 = user_with_key(free_env, email="b@test.uz")
    seed(free_env, uid2, LIMIT * 2, model=PAID)
    assert chat(free_env, key2).status_code == 200


def test_limit_is_per_user_across_keys(free_env):
    uid, key = user_with_key(free_env)
    key2, _ = add_key(free_env, uid, name="ikkinchi")
    seed(free_env, uid, LIMIT)
    assert chat(free_env, key).status_code == 429
    assert chat(free_env, key2).status_code == 429
    _, other = user_with_key(free_env, email="b@test.uz")
    assert chat(free_env, other).status_code == 200


def test_zero_limit_disables(free_env):
    uid, key = user_with_key(free_env)
    free_env.db.set_setting("free_daily_token_limit", "0")
    seed(free_env, uid, LIMIT * 4)
    assert chat(free_env, key).status_code == 200


def test_admin_free_key_exempt(free_env):
    uid = add_user(free_env)
    key, _ = add_key(free_env, uid, free=1)
    seed(free_env, uid, LIMIT * 2)
    assert chat(free_env, key).status_code == 200


def test_balance_endpoint_shows_quota_only_without_deposit(free_env):
    uid, key = user_with_key(free_env)
    seed(free_env, uid, 1234)
    q = free_env.client.get("/v1/balance", headers=bearer(key)).json()["free_daily"]
    assert q == {"models": [FREE], "limit": LIMIT, "used": 1234, "remaining": LIMIT - 1234,
                 "resets_at": billing.next_day_ts()}
    billing.credit(free_env.db, user_id=uid, amount=10_000 * SOM, kind="topup")
    assert free_env.client.get("/v1/balance", headers=bearer(key)).json()["free_daily"] is None


def test_portal_summary_shows_quota(free_env):
    uid = add_user(free_env)
    seed(free_env, uid, 5000)
    free_env.client.post("/portal/auth/login", json={"email": "a@test.uz", "password": "parol-12345"}, headers=CSRF)
    q = free_env.client.get("/portal/account/summary").json()["free_daily"]
    assert q["used"] == 5000 and q["limit"] == LIMIT
    billing.credit(free_env.db, user_id=uid, amount=10_000 * SOM, kind="topup")
    assert free_env.client.get("/portal/account/summary").json()["free_daily"] is None


def test_admin_can_change_limit(free_env):
    add_user(free_env, "admin@test.uz", role="admin")
    free_env.client.post("/portal/auth/login", json={"email": "admin@test.uz", "password": "parol-12345"}, headers=CSRF)
    r = free_env.client.put("/portal/admin/settings", json={"free_daily_token_limit": 1000,
                                                             "free_daily_limit_models": " model-1.0 , model-0.5 "}, headers=CSRF)
    assert r.status_code == 200
    assert r.json()["free_daily_token_limit"] == 1000 and r.json()["free_daily_limit_models"] == "model-1.0,model-0.5"
    assert free_env.client.put("/portal/admin/settings", json={"free_daily_token_limit": -1}, headers=CSRF).status_code == 422

    uid, key = user_with_key(free_env, email="u@test.uz")
    seed(free_env, uid, 1000)
    assert chat(free_env, key).status_code == 429


def test_daily_sum_uses_index(free_env):
    def plan(sql, args):
        with free_env.db.tx() as c:                    # PG picks a seq scan on a tiny table — we only check the index exists
            c.execute("SET LOCAL enable_seqscan = off")
            return " ".join(r[0] for r in c.execute("EXPLAIN " + sql, args).fetchall())
    assert "idx_usage_user_model_time" in plan(
        """SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) FROM usage_logs
           WHERE user_id=? AND model IN (?) AND created_at>=? AND status_code=200""", (1, FREE, 0))
    assert "idx_ledger_user_kind" in plan("SELECT 1 FROM ledger WHERE user_id=? AND kind='topup' AND amount>0 LIMIT 1", (1,))
