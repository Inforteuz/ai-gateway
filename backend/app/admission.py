"""Provider concurrency leases shared across API processes, tied to billing reservations."""
from __future__ import annotations
import asyncio
import time
from fastapi.concurrency import run_in_threadpool
from .db import now


def register(db, request_id, role, provider):
    with db.tx() as c:
        c.execute("""INSERT INTO inference_slots(request_id, role, provider, active, created_at)
            VALUES (?,?,?,0,?) ON CONFLICT (request_id, role) DO NOTHING""", (request_id, role, provider, now()))


def try_acquire(db, request_id, role, provider, limit):
    # Database.tx takes the same transaction-scoped advisory lock in all API
    # processes. COUNT + claim is atomic; there is no process-local capacity race.
    with db.tx() as c:
        row = c.execute("SELECT active FROM inference_slots WHERE request_id=? AND role=?", (request_id, role)).fetchone()
        if row is None:
            return False
        if row['active']:
            return True
        n = c.execute("""SELECT COUNT(*) FROM inference_slots s JOIN reservations r ON r.request_id=s.request_id
            WHERE s.provider=? AND s.active=1 AND r.expires_at>?""", (provider, now())).fetchone()[0]
        if n >= max(1, limit):
            return False
        c.execute("UPDATE inference_slots SET active=1 WHERE request_id=? AND role=?", (request_id, role))
        return True


def release(db, request_id, role):
    with db.tx() as c:
        c.execute("DELETE FROM inference_slots WHERE request_id=? AND role=?", (request_id, role))


async def wait(db, request_id, role, provider, limit, deadline):
    while time.monotonic() < deadline:
        if await run_in_threadpool(try_acquire, db, request_id, role, provider, limit):
            return True
        await asyncio.sleep(min(.1, max(0, deadline - time.monotonic())))
    return False


def snapshot(db):
    rows=db.all("""SELECT s.provider, s.active, COUNT(*) n FROM inference_slots s
        JOIN reservations r ON r.request_id=s.request_id WHERE r.expires_at>? GROUP BY s.provider, s.active""", (now(),))
    by_provider={}
    for row in rows:
        counts=by_provider.setdefault(row['provider'], {'active':0,'queued':0})
        counts['active' if row['active'] else 'queued']+=row['n']
    return by_provider
