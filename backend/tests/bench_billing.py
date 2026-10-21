"""Billing throughput under the global write lock (AG-09). Not part of the normal suite:

    python -m pytest -q -s tests/bench_billing.py

Each worker loops reserve -> settle on ONE wallet (worst case: every write contends for the same
rows and the global advisory lock). Measured: TPS, p50/p95/p99 of reserve+settle, time spent
waiting for a pool connection and for the advisory lock. Afterwards the billing invariants
(app/integrity.py) must hold. The 2-process case runs the same load from two OS processes, each
with its own connection pool, like two API replicas.
"""
import multiprocessing as mp
import statistics
import threading
import time
from contextlib import contextmanager

import pytest

from app import billing, integrity
from app.db import WRITE_LOCK_KEY, Conn, Database
from .conftest import SOM, add_key, add_user, wallet

ROUNDS = 20
PRICE_IN, PRICE_OUT = 10_000 * SOM, 30_000 * SOM


def timed_tx_factory(waits):
    """Database.tx with the pool and lock waits measured (same semantics as the real one)."""
    @contextmanager
    def tx(self):
        outer = self._tx.get()
        if outer is not None:
            yield outer
            return
        t0 = time.perf_counter()
        with self._write_lock, self.pool.connection() as raw, raw.transaction():
            t1 = time.perf_counter()
            raw.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK_KEY,))
            t2 = time.perf_counter()
            waits.append((t1 - t0, t2 - t1))
            c = Conn(raw, self)
            token = self._tx.set(c)
            try:
                yield c
            finally:
                self._tx.reset(token)
    return tx


def one_request(db, uid, key_id, rid, owner):
    billing.reserve(db, request_id=rid, user_id=uid, key_id=key_id, model="model-1.0", amount=5 * SOM, tokens=150,
                    free=False, monthly_limit=None, owner=owner, ttl=3600)
    billing.settle(db, request_id=rid, user_id=uid, key_id=key_id, model="model-1.0", endpoint="chat/completions",
                   reserved=5 * SOM, free=False, price_in=PRICE_IN, price_out=PRICE_OUT, prompt_tokens=100,
                   completion_tokens=50, status_code=200, stream=False)


def load(db, uid, key_id, threads, tag, owner):
    latencies, errors = [], []
    lock = threading.Lock()
    barrier = threading.Barrier(threads)

    def worker(n):
        barrier.wait()
        for i in range(ROUNDS):
            t0 = time.perf_counter()
            try:
                one_request(db, uid, key_id, f"req_{tag}_{n}_{i}", owner)
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
    return latencies, errors, time.perf_counter() - t0


def pct(values, p):
    if not values:
        return 0.0
    values = sorted(values)
    return values[min(len(values) - 1, int(round(p / 100 * (len(values) - 1))))] * 1000


def check_invariants(env, uid, ops, start_balance):
    per_request = billing.cost_for(100, PRICE_IN) + billing.cost_for(50, PRICE_OUT)
    assert wallet(env, uid) == (start_balance - ops * per_request, 0)
    violations, info = integrity.run(lambda sql: env.db.all(sql))
    assert violations == {} and info["reservations_in_flight"] == 0


@pytest.mark.parametrize("threads", [1, 10, 25, 50, 100])
def test_billing_throughput_one_process(env, threads, monkeypatch):
    waits = []
    monkeypatch.setattr(Database, "tx", timed_tx_factory(waits))
    uid = add_user(env, balance_som=1_000_000)
    _, key_id = add_key(env, uid)
    start = wallet(env, uid)[0]
    lat, errors, elapsed = load(env.db, uid, key_id, threads, f"p1t{threads}", env.app.state.instance_id)
    pool_w = [w[0] for w in waits]
    lock_w = [w[1] for w in waits]
    print(f"\n1 process {threads:>3} threads: {len(lat)} req in {elapsed:.2f}s = {len(lat) / elapsed:6.0f} TPS | "
          f"p50 {pct(lat, 50):5.1f} p95 {pct(lat, 95):6.1f} p99 {pct(lat, 99):6.1f} ms | "
          f"pool wait p95 {pct(pool_w, 95):5.1f} ms | lock wait p95 {pct(lock_w, 95):5.2f} ms | errors {len(errors)}")
    assert not errors, errors[:3]
    check_invariants(env, uid, len(lat), start)


def _child(dsn, uid, key_id, threads, tag, out):
    db = Database(dsn, pool_max=10)
    billing.register_instance(db, tag)
    try:
        lat, errors, elapsed = load(db, uid, key_id, threads, tag, tag)
        out.put((lat, errors, elapsed))
    finally:
        db.close()


@pytest.mark.parametrize("threads", [10, 50])
def test_billing_throughput_two_processes(env, threads):
    """Two OS processes (like two API replicas) charge the same wallet at once."""
    uid = add_user(env, balance_som=1_000_000)
    _, key_id = add_key(env, uid)
    start = wallet(env, uid)[0]
    ctx = mp.get_context("spawn")
    out = ctx.Queue()
    dsn = env.cfg.database_url
    procs = [ctx.Process(target=_child, args=(dsn, uid, key_id, threads, f"proc{p}t{threads}", out)) for p in range(2)]
    t0 = time.perf_counter()
    [p.start() for p in procs]
    results = [out.get(timeout=300) for _ in procs]
    [p.join() for p in procs]
    elapsed = time.perf_counter() - t0
    lat = [x for r in results for x in r[0]]
    errors = [x for r in results for x in r[1]]
    print(f"\n2 processes x {threads:>3} threads: {len(lat)} req in {elapsed:.2f}s = {len(lat) / elapsed:6.0f} TPS | "
          f"p50 {pct(lat, 50):5.1f} p95 {pct(lat, 95):6.1f} p99 {pct(lat, 99):6.1f} ms | errors {len(errors)}")
    assert not errors, errors[:3]
    check_invariants(env, uid, len(lat), start)
