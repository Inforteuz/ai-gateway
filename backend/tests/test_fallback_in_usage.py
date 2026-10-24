"""A request answered by the stand-in must not read as a failure.

Seen 2026-10-25 01:25: five `sable-4.0` requests hit the queue cap, all five
were retried on `sable-3.5` and answered, and the panel showed five red 503s
with the successes listed separately as if unrelated.
"""
from __future__ import annotations

from app.common import FALLBACK_SUFFIX

from .conftest import CSRF, add_key, add_user


def login(client, email, password="parol-12345"):
    assert client.post("/portal/auth/login", json={"email": email, "password": password},
                       headers=CSRF).status_code == 200


def _log(env, uid, key_id, request_id, model, status):
    with env.db.tx() as c:
        c.execute(
            """INSERT INTO usage_logs(request_id, user_id, key_id, model, endpoint, status_code,
                                      prompt_tokens, completion_tokens, cost, created_at, stream, estimated)
               VALUES (?,?,?,?,'chat/completions',?,10,5,0,1,0,0)""",
            (request_id, uid, key_id, model, status))


def test_failed_attempt_names_the_model_that_answered(env):
    uid = add_user(env, "a@test.uz", balance_som=100)
    _, key_id = add_key(env, uid)
    _log(env, uid, key_id, "req_aaa", "sable-4.0", 503)
    _log(env, uid, key_id, "req_aaa" + FALLBACK_SUFFIX, "sable-3.5", 200)
    login(env.client, "a@test.uz")

    rows = {r["request_id"]: r for r in env.client.get("/portal/usage").json()["items"]}
    assert rows["req_aaa"]["retried_on"] == "sable-3.5"
    # The retry itself is an ordinary successful row, not a failure of its own
    assert rows["req_aaa" + FALLBACK_SUFFIX]["retried_on"] is None


def test_a_failure_with_no_retry_stays_a_failure(env):
    uid = add_user(env, "b@test.uz", balance_som=100)
    _, key_id = add_key(env, uid)
    _log(env, uid, key_id, "req_bbb", "sable-4.0", 503)
    login(env.client, "b@test.uz")

    rows = {r["request_id"]: r for r in env.client.get("/portal/usage").json()["items"]}
    assert rows["req_bbb"]["retried_on"] is None


def test_a_retry_that_also_failed_is_not_reported_as_served(env):
    # Both attempts died: the request really did fail and must look like it.
    uid = add_user(env, "c@test.uz", balance_som=100)
    _, key_id = add_key(env, uid)
    _log(env, uid, key_id, "req_ccc", "sable-4.0", 503)
    _log(env, uid, key_id, "req_ccc" + FALLBACK_SUFFIX, "sable-3.5", 502)
    login(env.client, "c@test.uz")

    rows = {r["request_id"]: r for r in env.client.get("/portal/usage").json()["items"]}
    assert rows["req_ccc"]["retried_on"] is None


def test_another_users_retry_is_never_borrowed(env):
    # The lookup is scoped to the signed-in user; a colliding id must not leak
    # one account's model name into another's list.
    mine = add_user(env, "d@test.uz", balance_som=100)
    other = add_user(env, "e@test.uz", balance_som=100)
    _, my_key = add_key(env, mine)
    _, other_key = add_key(env, other)
    _log(env, mine, my_key, "req_ddd", "sable-4.0", 503)
    _log(env, other, other_key, "req_ddd" + FALLBACK_SUFFIX, "sable-3.5", 200)
    login(env.client, "d@test.uz")

    rows = {r["request_id"]: r for r in env.client.get("/portal/usage").json()["items"]}
    assert rows["req_ddd"]["retried_on"] is None
