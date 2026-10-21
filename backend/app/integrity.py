"""Read-only billing integrity checks: python -m app.integrity

Invariants (every balance change writes a ledger row in the same transaction, every in-flight
request has one reservation row):

  balance_vs_ledger    wallets.balance == SUM(ledger.amount) per user
  last_balance_after   the newest ledger row's balance_after == wallets.balance
  held_vs_reservations wallets.held == SUM(reservations.amount) per user
  usage_vs_ledger      every usage ledger row equals -cost of its usage_logs row, and every
                       charged usage_logs row has exactly one ledger row
  settled_reservation  no reservation exists for a request that is already in usage_logs
  negative_held        held >= 0

Informational (not errors): users with a negative balance (allowed debt, see billing.settle),
stale reservations waiting for the sweeper.

Exit code 0 = all invariants hold, 1 = violations (details printed, no secrets).
Used by `manage.sh check-integrity`, `verify-backup` (on a scratch copy) and the tests.
"""
from __future__ import annotations

import sys

CHECKS: dict[str, str] = {
    "balance_vs_ledger": """
        SELECT w.user_id, w.balance, COALESCE(l.total, 0) AS ledger_total
        FROM wallets w LEFT JOIN (SELECT user_id, SUM(amount) AS total FROM ledger GROUP BY user_id) l
             ON l.user_id = w.user_id
        WHERE w.balance <> COALESCE(l.total, 0)""",
    "last_balance_after": """
        SELECT w.user_id, w.balance, last.balance_after
        FROM wallets w JOIN LATERAL (SELECT balance_after FROM ledger WHERE user_id = w.user_id
                                     ORDER BY id DESC LIMIT 1) last ON TRUE
        WHERE last.balance_after <> w.balance""",
    "held_vs_reservations": """
        SELECT w.user_id, w.held, COALESCE(r.total, 0) AS reserved
        FROM wallets w LEFT JOIN (SELECT user_id, SUM(amount) AS total FROM reservations GROUP BY user_id) r
             ON r.user_id = w.user_id
        WHERE w.held <> COALESCE(r.total, 0)""",
    "usage_vs_ledger": """
        SELECT u.id AS usage_id, u.cost, COALESCE(SUM(-l.amount), 0) AS ledger_amount, COUNT(l.id) AS rows
        FROM usage_logs u LEFT JOIN ledger l ON l.usage_id = u.id AND l.kind = 'usage'
        GROUP BY u.id, u.cost
        HAVING (u.cost > 0 AND (COUNT(l.id) <> 1 OR COALESCE(SUM(-l.amount), 0) <> u.cost))
            OR (u.cost = 0 AND COUNT(l.id) <> 0)""",
    "orphan_usage_ledger": """
        SELECT l.id, l.usage_id FROM ledger l
        WHERE l.kind = 'usage' AND (l.usage_id IS NULL OR NOT EXISTS (SELECT 1 FROM usage_logs u WHERE u.id = l.usage_id))""",
    "settled_reservation": """
        SELECT r.request_id FROM reservations r JOIN usage_logs u ON u.request_id = r.request_id""",
    "negative_held": "SELECT user_id, held FROM wallets WHERE held < 0",
}

INFO: dict[str, str] = {
    "negative_balance_users": "SELECT COUNT(*) FROM wallets WHERE balance < 0",
    "reservations_in_flight": "SELECT COUNT(*) FROM reservations",
    "reservations_expired": "SELECT COUNT(*) FROM reservations WHERE expires_at < EXTRACT(EPOCH FROM now())",
}


def run(execute) -> tuple[dict[str, list], dict[str, int]]:
    """`execute(sql)` returns a list of rows. Returns ({check: violating rows}, {info: value})."""
    violations = {name: list(execute(sql)) for name, sql in CHECKS.items()}
    info = {name: int(execute(sql)[0][0]) for name, sql in INFO.items()}
    return {k: v for k, v in violations.items() if v}, info


def main() -> int:
    from .config import Config
    from .db import from_config

    db = from_config(Config())
    try:
        violations, info = run(lambda sql: db.all(sql))
    finally:
        db.close()
    for name, value in info.items():
        print(f"info  {name}: {value}")
    for name, rows in violations.items():
        print(f"FAIL  {name}: {len(rows)} row(s); first: {[tuple(r) for r in rows[:5]]}")
    if not violations:
        print(f"OK    all {len(CHECKS)} invariants hold")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
