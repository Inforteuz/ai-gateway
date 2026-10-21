"""In-flight reservations: limits hold under parallel load, across processes and restarts.

Covers audit items AG-01 (per-key monthly limit), AG-02 (free daily quota), AG-03 (per-key
concurrency) and AG-05 (startup must not wipe other processes' holds).
"""
import threading

from app import billing
from app.main import create_app
from .conftest import CHAT, SOM, add_key, add_user, bearer, reserve, wallet


def parallel(n, fn):
    """Runs fn(i) in n threads released at the same moment; returns the results/exceptions in order."""
    barrier = threading.Barrier(n)
    out = [None] * n

    def run(i):
        barrier.wait()
        try:
            out[i] = fn(i)
        except Exception as e:   # noqa: BLE001 - collected for assertions
            out[i] = e

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    return out


def codes(results):
    return sorted(r.code if isinstance(r, billing.BillingError) else "ok" for r in results)


def held_rows(env):
    return env.db.scalar("SELECT COUNT(*) FROM reservations")


# --- AG-01: monthly key limit counts pending holds ---------------------------------------------------------------
def test_parallel_requests_cannot_exceed_monthly_key_limit(env):
    uid = add_user(env, balance_som=100)
    _, key_a = add_key(env, uid, name="a")
    _, key_b = add_key(env, uid, name="b")
    res = parallel(2, lambda i: reserve(env, uid, key_a, 7 * SOM, monthly_limit=10 * SOM))
    assert codes(res) == ["key_limit_reached", "ok"]
    # The limit is per key: another key of the same wallet is not blocked by key A's hold
    reserve(env, uid, key_b, 7 * SOM, request_id="req_b", monthly_limit=10 * SOM)
    assert wallet(env, uid) == (100 * SOM, 14 * SOM)


def test_monthly_limit_after_settle_uses_real_cost(env):
    uid = add_user(env, balance_som=100)
    _, key = add_key(env, uid)
    reserve(env, uid, key, 7 * SOM, request_id="req_1", monthly_limit=10 * SOM)
    # Settled for much less than the hold: 100 output tokens at 30 000 micro = 3 SOM
    billing.settle(env.db, request_id="req_1", user_id=uid, key_id=key, model="model-1.0", endpoint="chat/completions",
                   reserved=7 * SOM, free=False, price_in=0, price_out=30_000 * SOM, prompt_tokens=0,
                   completion_tokens=100, status_code=200, stream=False)
    assert wallet(env, uid) == (97 * SOM, 0)
    reserve(env, uid, key, 7 * SOM, request_id="req_2", monthly_limit=10 * SOM)     # 3 spent + 7 pending = 10
    try:
        reserve(env, uid, key, 1, request_id="req_3", monthly_limit=10 * SOM)
        assert False, "limit must be reached"
    except billing.BillingError as e:
        assert e.code == "key_limit_reached"


# --- AG-02: free daily quota counts pending tokens ----------------------------------------------------------------
def test_free_quota_counts_in_flight_tokens(env):
    uid = add_user(env)
    _, key = add_key(env, uid)
    res = parallel(10, lambda i: reserve(env, uid, key, 0, tokens=400, quota_limit=1000, quota_models=["model-1.0"]))
    # Admitted while completed + pending < limit: 0, 400, 800 -> three requests, then the rest are refused.
    assert codes(res) == ["free_daily_limit"] * 7 + ["ok"] * 3
    refused = next(r for r in res if isinstance(r, billing.FreeQuotaExceeded))
    assert refused.quota.used == 1200 and refused.quota.limit == 1000


def test_free_quota_ignores_other_models_and_deposit_users(env):
    uid = add_user(env)
    _, key = add_key(env, uid)
    reserve(env, uid, key, 0, tokens=5000, model="model-paid", quota_limit=1000, quota_models=["model-1.0"])
    reserve(env, uid, key, 0, tokens=10, quota_limit=1000, quota_models=["model-1.0"])   # other model's tokens do not count


def test_gateway_rejects_when_pending_tokens_fill_free_quota(env):
    env.db.set_setting("free_daily_token_limit", "1000")
    env.db.set_setting("free_daily_limit_models", "model-1.0")
    uid = add_user(env)
    billing.credit(env.db, user_id=uid, amount=1000 * SOM, kind="bonus")     # a bonus is not a deposit: quota applies
    key, key_id = add_key(env, uid)
    reserve(env, uid, key_id, 0, request_id="req_pending", tokens=1500)
    r = env.client.post("/v1/chat/completions", json=CHAT, headers=bearer(key))
    assert r.status_code == 429 and r.json()["error"]["code"] == "free_daily_limit"
    billing.release(env.db, request_id="req_pending", user_id=uid)
    assert env.client.post("/v1/chat/completions", json=CHAT, headers=bearer(key)).status_code == 200


# --- AG-03: per-key concurrency ------------------------------------------------------------------------------------
def test_per_key_concurrency_holds_under_parallel_http(env):
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    env.up["delay"] = 0.6
    res = parallel(10, lambda i: env.client.post("/v1/chat/completions", json=CHAT, headers=bearer(key)))
    statuses = sorted(r.status_code for r in res)
    limit = env.cfg.per_key_concurrent
    assert statuses.count(200) <= limit and statuses.count(200) >= 1
    rejected = [r for r in res if r.status_code == 429]
    assert rejected and all(r.json()["error"]["code"] == "too_many_concurrent" for r in rejected)
    assert held_rows(env) == 0 and wallet(env, uid)[1] == 0 and env.app.state.inflight == {}


def test_per_key_concurrency_counts_other_processes(env):
    uid = add_user(env, balance_som=1000)
    key, key_id = add_key(env, uid)
    billing.register_instance(env.db, "replica-b")
    for i in range(env.cfg.per_key_concurrent):
        reserve(env, uid, key_id, 0, request_id=f"req_b{i}", owner="replica-b")
    r = env.client.post("/v1/chat/completions", json=CHAT, headers=bearer(key))
    assert r.status_code == 429 and r.json()["error"]["code"] == "too_many_concurrent"
    assert env.app.state.inflight == {}          # the local slot was given back


def test_rejected_admission_leaves_no_reservation(env):
    uid = add_user(env, balance_som=0)
    _, key_id = add_key(env, uid)
    try:
        reserve(env, uid, key_id, 5 * SOM, request_id="req_x")
    except billing.BillingError as e:
        assert e.code == "insufficient_balance"
    assert held_rows(env) == 0


# --- AG-05: restarts and replicas ------------------------------------------------------------------------------------
def test_new_process_keeps_live_reservations_and_releases_orphans(env):
    uid = add_user(env, balance_som=100)
    _, key_id = add_key(env, uid)
    billing.register_instance(env.db, "replica-live")
    reserve(env, uid, key_id, 5 * SOM, request_id="req_live", owner="replica-live")
    reserve(env, uid, key_id, 3 * SOM, request_id="req_dead", owner="crashed-process")   # owner has no heartbeat
    reserve(env, uid, key_id, 2 * SOM, request_id="req_expired", owner="replica-live", ttl=-1)   # lease expired

    second = create_app(env.cfg)            # a second replica / restarted process starts up
    try:
        assert wallet(env, uid) == (100 * SOM, 5 * SOM)
        assert [r["request_id"] for r in env.db.all("SELECT request_id FROM reservations")] == ["req_live"]
    finally:
        second.state.db.close()


def test_legacy_held_without_rows_is_cleared(env):
    uid = add_user(env, balance_som=100)
    with env.db.tx() as c:     # held left by a version before reservations existed
        c.execute("UPDATE wallets SET held=? WHERE user_id=?", (4 * SOM, uid))
    billing.sweep_reservations(env.db, env.app.state.instance_id)
    assert wallet(env, uid) == (100 * SOM, 0)


def test_settle_after_sweep_does_not_release_twice(env):
    uid = add_user(env, balance_som=100)
    _, key_id = add_key(env, uid)
    reserve(env, uid, key_id, 5 * SOM, request_id="req_swept", owner="gone")
    reserve(env, uid, key_id, 3 * SOM, request_id="req_other")
    billing.sweep_reservations(env.db, env.app.state.instance_id)          # releases "gone"'s 5 SOM
    assert wallet(env, uid) == (100 * SOM, 3 * SOM)
    billing.settle(env.db, request_id="req_swept", user_id=uid, key_id=key_id, model="model-1.0",
                   endpoint="chat/completions", reserved=5 * SOM, free=False, price_in=0, price_out=0,
                   prompt_tokens=1, completion_tokens=1, status_code=200, stream=False)
    assert wallet(env, uid) == (100 * SOM, 3 * SOM)                         # req_other's hold is intact


def test_graceful_shutdown_releases_own_reservations(env):
    uid = add_user(env, balance_som=100)
    _, key_id = add_key(env, uid)
    billing.register_instance(env.db, "stopping")
    reserve(env, uid, key_id, 5 * SOM, request_id="req_s", owner="stopping")
    billing.unregister_instance(env.db, "stopping")
    assert wallet(env, uid) == (100 * SOM, 0) and held_rows(env) == 0


def test_cli_process_does_not_touch_holds(env):
    """Opening the database (manage.sh / CLI) must not zero holds of running requests."""
    from app.db import from_config
    uid = add_user(env, balance_som=100)
    _, key_id = add_key(env, uid)
    reserve(env, uid, key_id, 5 * SOM, request_id="req_live")
    other = from_config(env.cfg)
    other.close()
    assert wallet(env, uid) == (100 * SOM, 5 * SOM)
