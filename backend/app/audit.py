"""Admin action log: who did what, when, to which object, and the outcome.

The table is append-only — the app role has no UPDATE/DELETE on it (`db.NO_DELETE`,
`db.NO_UPDATE`), so the trail survives even a code bug or SQL injection.
"""
from __future__ import annotations

import logging

from .db import Database, now

log = logging.getLogger("audit")

_MAX_DETAIL = 500


def record(db: Database, *, admin: dict, action: str, target: str, ok: bool, detail: str = "",
           ip: str = "") -> None:
    """Adds an entry. A failed write does not stop the action, but an error is logged."""
    detail = (detail or "")[:_MAX_DETAIL]
    log.info("admin audit: %s %s %s ok=%s %s", admin.get("email"), action, target, ok, detail)
    try:
        with db.tx() as c:
            c.execute("INSERT INTO admin_audit(created_at, admin_id, admin_email, action, target, ok, detail, ip)"
                      " VALUES (?,?,?,?,?,?,?,?)",
                      (now(), admin.get("id"), admin.get("email") or "", action, target, 1 if ok else 0, detail, ip or ""))
    except Exception:
        log.exception("admin audit entry was not saved: %s %s", action, target)


def recent(db: Database, *, target_prefix: str = "", limit: int = 50) -> list[dict]:
    rows = db.all("SELECT id, created_at, admin_email, action, target, ok, detail FROM admin_audit"
                  " WHERE target LIKE ? ORDER BY id DESC LIMIT ?", (target_prefix + "%", limit))
    return [{**dict(r), "ok": bool(r["ok"])} for r in rows]
