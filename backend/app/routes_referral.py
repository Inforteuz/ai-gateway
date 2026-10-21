"""Referral program: /portal/referral (user) and /portal/admin/referrals/* (administrator)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from . import oauth, referral
from .common import ApiError, csrf_guard, get_db, require_admin, require_user
from .db import Database
from .routes_account import page_args

router = APIRouter(prefix="/portal", dependencies=[Depends(csrf_guard)], tags=["referral"])
admin_router = APIRouter(prefix="/portal/admin/referrals", dependencies=[Depends(csrf_guard), Depends(require_admin)],
                         tags=["admin"])


def _program_out(db: Database) -> dict:
    enabled, bp = referral.program(db.get_settings())
    return {"enabled": enabled, "percent": referral.bp_text(bp)}


# --- user --------------------------------------------------------------------------------------------------------
@router.get("/referral")
def my_referral(request: Request, user=Depends(require_user), db: Database = Depends(get_db)):
    code = referral.ensure_code(db, user["id"])
    uid = user["id"]
    rewards = db.all("""SELECT r.created_at, r.amount, r.deposit_amount, r.percent_bp, u.email
                        FROM referral_rewards r JOIN users u ON u.id = r.referee_id
                        WHERE r.referrer_id=? AND r.status='paid' ORDER BY r.id DESC LIMIT 20""", (uid,))
    return {
        **_program_out(db),
        "code": code,
        "link": f"{oauth.site_base(db, request)}/login?ref={code}",
        "invited": db.scalar("SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (uid,)),
        # invitees who made at least one deposit
        "depositors": db.scalar("SELECT COUNT(DISTINCT referee_id) FROM referral_rewards WHERE referrer_id=?", (uid,)),
        "earned": db.scalar("SELECT COALESCE(SUM(amount),0) FROM referral_rewards WHERE referrer_id=? AND status='paid'", (uid,)),
        "rewards": [{"created_at": r["created_at"], "amount": r["amount"], "deposit_amount": r["deposit_amount"],
                     "percent": referral.bp_text(r["percent_bp"]), "referee": referral.mask_email(r["email"])}
                    for r in rewards],
    }


# --- administrator -----------------------------------------------------------------------------------------------
# Number of accounts opened from the same IP/device: a fraud signal for a referred account.
LINK_SELECT = """
SELECT f.referee_id, f.referrer_id, f.method, f.flags, f.created_at,
       e.email AS referee_email, e.signup_ip AS referee_ip, e.status AS referee_status,
       o.email AS referrer_email, o.referral_paused AS referrer_paused, o.status AS referrer_status,
       (SELECT COUNT(*) FROM users x WHERE e.signup_ip IS NOT NULL AND x.signup_ip = e.signup_ip) AS ip_accounts,
       (SELECT COUNT(*) FROM users x WHERE e.signup_device IS NOT NULL AND x.signup_device = e.signup_device) AS device_accounts,
       (SELECT COALESCE(SUM(amount),0) FROM ledger l WHERE l.user_id = f.referee_id AND l.kind='topup' AND l.amount>0) AS deposits,
       (SELECT COALESCE(SUM(amount),0) FROM referral_rewards r WHERE r.referee_id = f.referee_id AND r.status='paid') AS rewarded
FROM referrals f JOIN users e ON e.id = f.referee_id JOIN users o ON o.id = f.referrer_id
"""


def _link_out(r) -> dict:
    flags = [f for f in (r["flags"] or "").split(",") if f]
    if r["ip_accounts"] > 1:
        flags.append("multi_ip")
    if r["device_accounts"] > 1:
        flags.append("multi_device")
    return {
        "referee_id": r["referee_id"], "referee_email": r["referee_email"], "referee_ip": r["referee_ip"],
        "referee_status": r["referee_status"],
        "referrer_id": r["referrer_id"], "referrer_email": r["referrer_email"],
        "referrer_paused": bool(r["referrer_paused"]), "referrer_status": r["referrer_status"],
        "method": r["method"], "created_at": r["created_at"], "flags": flags,
        "ip_accounts": r["ip_accounts"], "device_accounts": r["device_accounts"],
        "deposits": r["deposits"], "rewarded": r["rewarded"],
    }


@admin_router.get("")
def admin_referrals(q: str = "", flagged: bool = False, db: Database = Depends(get_db)):
    rows = db.all(LINK_SELECT + " WHERE (e.email ILIKE :q OR o.email ILIKE :q) ORDER BY f.created_at DESC LIMIT 500",
                  {"q": f"%{q.strip()}%"})
    items = [_link_out(r) for r in rows]
    if flagged:
        items = [i for i in items if i["flags"]]
    return {
        **_program_out(db),
        "totals": {
            "links": db.scalar("SELECT COUNT(*) FROM referrals"),
            "referrers": db.scalar("SELECT COUNT(DISTINCT referrer_id) FROM referrals"),
            "paid": db.scalar("SELECT COALESCE(SUM(amount),0) FROM referral_rewards WHERE status='paid'"),
            "paid_count": db.scalar("SELECT COUNT(*) FROM referral_rewards WHERE status='paid'"),
            "skipped_count": db.scalar("SELECT COUNT(*) FROM referral_rewards WHERE status='skipped'"),
        },
        "items": items,
    }


@admin_router.get("/rewards")
def admin_rewards(limit: int = 50, offset: int = 0, db: Database = Depends(get_db)):
    limit, offset = page_args(limit, offset)
    rows = db.all("""SELECT r.*, o.email AS referrer_email, e.email AS referee_email
                     FROM referral_rewards r JOIN users o ON o.id = r.referrer_id JOIN users e ON e.id = r.referee_id
                     ORDER BY r.id DESC LIMIT ? OFFSET ?""", (limit, offset))
    return {"total": db.scalar("SELECT COUNT(*) FROM referral_rewards"),
            "items": [{"id": r["id"], "created_at": r["created_at"], "referrer_id": r["referrer_id"],
                       "referrer_email": r["referrer_email"], "referee_id": r["referee_id"],
                       "referee_email": r["referee_email"], "deposit_amount": r["deposit_amount"],
                       "percent": referral.bp_text(r["percent_bp"]), "amount": r["amount"],
                       "status": r["status"], "reason": r["reason"]} for r in rows]}


class ProgramIn(BaseModel):
    enabled: bool | None = None
    percent: str | None = Field(default=None, max_length=8, pattern=r"^\s*\d{1,2}([.,]\d{1,2})?\s*$")


@admin_router.put("/settings")
def admin_program(body: ProgramIn, db: Database = Depends(get_db)):
    if body.percent is not None:
        bp = referral.parse_bp(body.percent)
        if bp is None or not 0 < bp <= referral.MAX_PERCENT_BP:
            raise ApiError(400, "invalid_percent", "Foiz 0,01 dan 50 gacha bo'lishi kerak.")
        db.set_setting("referral_percent", referral.bp_text(bp))
    if body.enabled is not None:
        db.set_setting("referral_enabled", "1" if body.enabled else "0")
    return _program_out(db)


class PauseIn(BaseModel):
    paused: bool


@admin_router.post("/users/{user_id}/pause")
def admin_pause(user_id: int, body: PauseIn, db: Database = Depends(get_db)):
    """Pauses or resumes reward payouts to this user (as a referrer)."""
    with db.tx() as c:
        n = c.execute("UPDATE users SET referral_paused=? WHERE id=?", (int(body.paused), user_id)).rowcount
    if not n:
        raise ApiError(404, "user_not_found", "Foydalanuvchi topilmadi.")
    return {"user_id": user_id, "paused": body.paused}
