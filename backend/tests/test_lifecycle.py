"""AG-10: every request ends exactly once — settled or released — whatever happens on the way.

After each scenario the billing invariants (app/integrity.py) must hold, no reservation may be
left and the per-key slot must be free.
"""
import asyncio
import dataclasses

import pytest

from app import billing, integrity
from app.gateway import janitor_limit, reservation_ttl
from .conftest import CHAT, SOM, add_key, add_user, bearer, wallet


def consistent(env):
    violations, info = integrity.run(lambda sql: env.db.all(sql))
    assert violations == {}, violations
    assert info["reservations_in_flight"] == 0
    assert env.app.state.inflight == {}


def paid_user(env, som=1000):
    uid = add_user(env, balance_som=som)
    key, key_id = add_key(env, uid)
    return uid, key, key_id


def usage(env, uid):
    return env.db.all("SELECT status_code, cost, error FROM usage_logs WHERE user_id=? ORDER BY id", (uid,))


def test_ok_request_charges_once(env):
    uid, key, _ = paid_user(env)
    assert env.client.post("/v1/chat/completions", json=CHAT, headers=bearer(key)).status_code == 200
    rows = usage(env, uid)
    assert len(rows) == 1 and rows[0]["status_code"] == 200 and rows[0]["cost"] > 0
    assert wallet(env, uid)[0] == 1000 * SOM - rows[0]["cost"]
    consistent(env)


# The gateway's contract: upstream 400 -> 400, upstream 429 -> 503 "model busy" (retry later), 5xx -> 502
@pytest.mark.parametrize("mode,status", [("400", 400), ("429", 503), ("500", 502)])
def test_upstream_errors_are_not_charged(env, mode, status):
    uid, key, _ = paid_user(env)
    env.up["mode"] = mode
    r = env.client.post("/v1/chat/completions", json=CHAT, headers=bearer(key))
    assert r.status_code == status
    assert [row["cost"] for row in usage(env, uid)] == [0]
    assert wallet(env, uid) == (1000 * SOM, 0)
    consistent(env)


def test_upstream_timeout_is_not_charged(env):
    uid, key, _ = paid_user(env)
    env.up["mode"] = "timeout"
    r = env.client.post("/v1/chat/completions", json=CHAT, headers=bearer(key))
    assert r.status_code == 504 and r.json()["error"]["code"] == "upstream_timeout"
    assert wallet(env, uid) == (1000 * SOM, 0)
    consistent(env)


def test_stream_broken_before_first_byte_is_not_charged(env):
    uid, key, _ = paid_user(env)
    env.up["break_stream"] = True     # the in-process transport buffers, so this fails before any byte
    r = env.client.post("/v1/chat/completions", json={**CHAT, "stream": True}, headers=bearer(key))
    assert r.status_code == 502
    assert wallet(env, uid) == (1000 * SOM, 0)
    consistent(env)


def test_stream_broken_mid_way_bills_only_delivered_tokens(env):
    """Partial-stream policy: tokens already produced and sent to the client are paid for (estimated
    from the text when the provider sent no usage); the rest of the hold is released."""
    import json
    import httpx
    from app.gateway import Ticket, _stream_body
    from .conftest import reserve
    uid, _, key_id = paid_user(env, som=100)
    reserve(env, uid, key_id, 5 * SOM, request_id="req_brk")
    t = Ticket(state=env.app.state, request_id="req_brk", key={"id": key_id, "user_id": uid, "free": 0},
               model_id="model-1.0", endpoint="chat/completions", price_in=10_000 * SOM, price_out=30_000 * SOM,
               reserved=5 * SOM, stream=True, ip="1.1.1.1", t0=0.0, est_prompt=20)

    class BrokenResp:
        async def aiter_lines(self):
            yield "data: " + json.dumps({"choices": [{"delta": {"content": "x" * 40}}]})
            yield ""
            raise httpx.RemoteProtocolError("peer closed connection without sending complete message body")

        async def aclose(self):
            pass

    async def run():
        chunks = []
        try:
            async for chunk in _stream_body(BrokenResp(), t, False, "model-1.0"):
                chunks.append(chunk)
        except Exception:
            pass
        return chunks

    chunks = asyncio.run(run())
    assert any(b"xxxx" in (c if isinstance(c, bytes) else c.encode()) for c in chunks)
    log = env.db.one("SELECT * FROM usage_logs WHERE request_id='req_brk'")
    assert log is not None and log["completion_tokens"] > 0 and log["cost"] > 0
    assert wallet(env, uid) == (100 * SOM - log["cost"], 0)
    consistent(env)


def test_cancellation_after_admission_releases_the_hold(env, monkeypatch):
    """The request is cancelled right after reserve() succeeded in its thread (before a ticket exists)."""
    uid, key, _ = paid_user(env)
    original = billing.reserve

    def reserve_then_cancel(*a, **kw):
        original(*a, **kw)
        raise asyncio.CancelledError()

    monkeypatch.setattr(billing, "reserve", reserve_then_cancel)
    with pytest.raises(BaseException):
        env.client.post("/v1/chat/completions", json=CHAT, headers=bearer(key))
    assert wallet(env, uid) == (1000 * SOM, 0)
    consistent(env)


def test_rejected_requests_leave_nothing_behind(env):
    uid, key, _ = paid_user(env, som=0)
    r = env.client.post("/v1/chat/completions", json=CHAT, headers=bearer(key))
    assert r.status_code == 402
    consistent(env)


def test_reservation_outlives_any_live_request():
    """A valid request can never be released by the sweeper while it is still running: the janitor
    closes every ticket older than janitor_limit (checked every 60 s) and the lease is longer."""
    from app.config import Config
    cfg = Config()
    assert reservation_ttl(cfg) > janitor_limit(cfg) + 60


def test_parallel_paid_load_keeps_invariants(env):
    """100 parallel paid requests from one user through the HTTP API: balance - held never goes wrong."""
    import threading
    uid, key, _ = paid_user(env, som=100_000)
    env.app.state.config = dataclasses.replace(env.app.state.config, per_key_concurrent=1000)
    keys = [key] + [add_key(env, uid, name=f"k{i}")[0] for i in range(3)]
    barrier = threading.Barrier(100)
    statuses = []

    def run(i):
        barrier.wait()
        r = env.client.post("/v1/chat/completions", json={**CHAT, "max_tokens": 50}, headers=bearer(keys[i % 4]))
        statuses.append(r.status_code)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(100)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    charged = env.db.scalar("SELECT COALESCE(SUM(cost),0) FROM usage_logs WHERE user_id=?", (uid,))
    assert statuses.count(200) == env.db.scalar("SELECT COUNT(*) FROM usage_logs WHERE user_id=? AND status_code=200", (uid,))
    assert wallet(env, uid) == (100_000 * SOM - charged, 0)
    consistent(env)
