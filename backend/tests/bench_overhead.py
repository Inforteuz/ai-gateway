"""Gateway overhead per request with an instant in-process upstream (not part of the normal suite):

    python -m pytest -q -s tests/bench_overhead.py

Reports wall time per /v1/chat/completions call and how many SQL statements it issued. Model time is
excluded (the mock answers at once), so this is what the gateway itself adds.
"""
import statistics
import time

from .conftest import CHAT, add_key, add_user, bearer


def test_gateway_overhead(env, monkeypatch):
    import app.db as dbm
    uid = add_user(env, balance_som=10000)
    key, _ = add_key(env, uid, rpm=100000)
    statements = {"n": 0}
    original = dbm.Conn.execute

    def counted(self, *a, **k):
        statements["n"] += 1
        return original(self, *a, **k)

    monkeypatch.setattr(dbm.Conn, "execute", counted)
    env.client.post("/v1/chat/completions", json=CHAT, headers=bearer(key))      # warm up
    statements["n"] = 0
    lat = []
    for _ in range(25):
        t = time.perf_counter()
        assert env.client.post("/v1/chat/completions", json=CHAT, headers=bearer(key)).status_code == 200
        lat.append((time.perf_counter() - t) * 1000)
    print(f"\nper request: {statements['n'] / 25:.1f} SQL statements, "
          f"p50 {statistics.median(lat):.1f} ms, p95 {sorted(lat)[23]:.1f} ms (gateway + in-process mock)")
