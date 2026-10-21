"""Top-up request review: the admin panel and the Telegram button both go through this single path.

The status change and the credit (ledger, referral reward — inside `credit_tx`) happen in one transaction.
`db.tx()` serialises all writes (advisory lock) and the status is checked inside that critical section —
even if the panel and Telegram click at the same time only one succeeds, the other gets 409.
"""
from __future__ import annotations

from .billing import credit_tx
from .common import ApiError
from .db import Database, now


def resolve(db: Database, topup_id: int, *, approve: bool, admin_id: int | None, amount: int | None = None,
            note: str = "", source: str = "") -> int:
    """Approves or rejects the request. Returns the recorded amount (the requested amount when rejected).

    `source` is appended to the ledger note ("Telegram"); empty for the panel.
    """
    note = note.strip()
    with db.tx() as c:
        row = c.execute("SELECT * FROM topup_requests WHERE id=?", (topup_id,)).fetchone()
        if row is None:
            raise ApiError(404, "topup_not_found", "So'rov topilmadi.")
        if row["status"] != "pending":
            raise ApiError(409, "already_resolved", "Bu so'rov allaqachon ko'rib chiqilgan.")
        amount = amount or row["amount"]
        cur = c.execute("UPDATE topup_requests SET status=?, admin_note=?, resolved_at=?, resolved_by=?, amount=?"
                        " WHERE id=? AND status='pending'",
                        ("approved" if approve else "rejected", note, now(), admin_id, amount, topup_id))
        if cur.rowcount != 1:  # the write lock rules this out; still never credit twice
            raise ApiError(409, "already_resolved", "Bu so'rov allaqachon ko'rib chiqilgan.")
        if approve:
            extra = "; ".join(x for x in (source, note) if x)
            credit_tx(c, user_id=row["user_id"], amount=amount, kind="topup",
                      note=f"To'ldirish so'rovi #{topup_id}" + (f": {extra}" if extra else ""), admin_id=admin_id)
    return amount
