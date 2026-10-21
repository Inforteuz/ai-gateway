"""Billing throughput under the global write lock (AG-09). Not part of the normal suite:

    python -m pytest -q -s tests/bench_billing.py

Each thread loops reserve -> settle on ONE wallet (worst case: every write contends) and the
invariants are checked afterwards: held == 0, no reservation left, balance == start - charged,
ledger sum == charged.
"""
import statistics
import threading
import time

import pytest

from app import billing
from .conftest import SOM, add_key, add_user, reserve, wallet

ROUNDS = 20


@pytest.mark.parametrize("threads", [10, 50, 100])
def test_billing_throughput(env, threads):
    uid = add_user(env, balance_som=1_000_000)
    _, key_id = add_key(env, uid)
    start_balance = wallet(env, uid)[0]
    latencies, errors = [], []
    lock = threading.Lock()
    barrier = threading.Barrier(threads)

    def worker(n):
        barrier.wait()
        for i in range(ROUNDS):
            rid = f"req_{threads}_{n}_{i}"
            t0 = time.perf_counter()
            try:
                reserve(env, uid, key_id, 5 * SOM, request_id=rid)
                billing.settle(env.db, request_id=rid, user_id=uid, key_id=key_id, model="model-1.0",
                               endpoint="chat/completions", reserved=5 * SOM, free=False, price_in=10_000 * SOM,
                               price_out=30_000 * SOM, prompt_tokens=100, completion_tokens=50,
                               status_code=200, stream=False)
            except Exception as e:   # noqa: BLE001
                with lock:
                    errors.append(repr(e))
                continue
            with lock:
                latencies.append(time.perf_counter() - t0)

    t0 = time.perf_counter()
    ts = [threading.Thread(target=worker, args=(n,)) for n in range(threads)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    elapsed = time.perf_counter() - t0

    ops = len(latencies)
    p95 = statistics.quantiles(latencies, n=20)[18] * 1000 if len(latencies) >= 20 else 0
    print(f"\n{threads:>3} threads: {ops} reserve+settle in {elapsed:.2f}s = {ops / elapsed:.0f}/s, "
          f"p50 {statistics.median(latencies) * 1000:.0f} ms, p95 {p95:.0f} ms, errors {len(errors)}")

    per_request = billing.cost_for(100, 10_000 * SOM) + billing.cost_for(50, 30_000 * SOM)
    balance, held = wallet(env, uid)
    assert not errors, errors[:3]
    assert held == 0 and env.db.scalar("SELECT COUNT(*) FROM reservations") == 0
    assert balance == start_balance - ops * per_request
    ledger = env.db.scalar("SELECT COALESCE(SUM(amount),0) FROM ledger WHERE user_id=? AND kind='usage'", (uid,))
    assert ledger == -ops * per_request
