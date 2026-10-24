"""/portal/admin/*: users, balances, pricing, settings and statistics for administrators."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, EmailStr, Field

from . import audit, captcha, security, topup_telegram, topups
from .billing import BillingError, TASHKENT, credit, month_start_ts
from .common import ApiError, create_user, csrf_guard, get_db, require_admin
from .db import Database, now
from .routes_account import (daily_series, key_out, keys_with_spend, model_out, page_args, topup_out, upstream_online,
                             usage_out)

router = APIRouter(prefix="/portal/admin", dependencies=[Depends(csrf_guard), Depends(require_admin)], tags=["admin"])


# --- users -------------------------------------------------------------------------------------------------------------
def user_row_out(r) -> dict:
    return {
        "id": r["id"], "email": r["email"], "name": r["name"], "role": r["role"], "status": r["status"],
        "created_at": r["created_at"], "last_login_at": r["last_login_at"],
        "balance": r["balance"] or 0, "held": r["held"] or 0,
        "active_keys": r["active_keys"], "month_cost": r["month_cost"], "month_requests": r["month_requests"],
        "last_request_at": r["last_request_at"],
    }


USER_SELECT = """
SELECT u.*, w.balance, w.held,
  (SELECT COUNT(*) FROM api_keys k WHERE k.user_id=u.id AND k.status='active') AS active_keys,
  (SELECT COALESCE(SUM(cost),0) FROM usage_logs l WHERE l.user_id=u.id AND l.created_at>=:ms) AS month_cost,
  (SELECT COUNT(*) FROM usage_logs l WHERE l.user_id=u.id AND l.created_at>=:ms) AS month_requests,
  (SELECT MAX(created_at) FROM usage_logs l WHERE l.user_id=u.id) AS last_request_at
FROM users u LEFT JOIN wallets w ON w.user_id=u.id
"""


@router.get("/users")
def list_users(q: str = "", archived: bool = False, db: Database = Depends(get_db)):
    """Archived accounts are out of the way by default; `archived=1` lists only those."""
    rows = db.all(USER_SELECT + " WHERE (u.email ILIKE :q OR u.name ILIKE :q)"
                  + (" AND u.status = 'archived'" if archived else " AND u.status <> 'archived'")
                  + " ORDER BY u.id DESC LIMIT 500",
                  {"ms": month_start_ts(), "q": f"%{q.strip()}%"})
    return {"items": [user_row_out(r) for r in rows]}


class UserIn(BaseModel):
    email: EmailStr
    name: str = Field(default="", max_length=80)
    password: str | None = Field(default=None, min_length=8, max_length=200)
    role: str = Field(default="user", pattern="^(user|admin)$")
    initial_credit: int = Field(default=0, ge=0, description="micro-sum")


@router.post("/users")
def add_user(body: UserIn, admin=Depends(require_admin), db: Database = Depends(get_db)):
    password = body.password or security.new_temp_password()
    uid = create_user(db, email=body.email, name=body.name, password=password, role=body.role,
                      bonus_micro=body.initial_credit, admin_id=admin["id"])
    row = db.one(USER_SELECT + " WHERE u.id=:id", {"ms": month_start_ts(), "id": uid})
    # The password is shown only in this response (not returned if the admin supplied it).
    return {"item": user_row_out(row), "temp_password": None if body.password else password}


class UserPatch(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    status: str | None = Field(default=None, pattern="^(active|suspended)$")
    role: str | None = Field(default=None, pattern="^(user|admin)$")


def _admin_count(db: Database) -> int:
    return db.scalar("SELECT COUNT(*) FROM users WHERE role='admin' AND status='active'")


@router.patch("/users/{user_id}")
def patch_user(user_id: int, body: UserPatch, admin=Depends(require_admin), db: Database = Depends(get_db)):
    row = db.one("SELECT * FROM users WHERE id=?", (user_id,))
    if row is None:
        raise ApiError(404, "user_not_found", "Foydalanuvchi topilmadi.")
    new_status = body.status or row["status"]
    new_role = body.role or row["role"]
    losing_admin = row["role"] == "admin" and row["status"] == "active" and (new_role != "admin" or new_status != "active")
    if losing_admin and (user_id == admin["id"] or _admin_count(db) <= 1):
        raise ApiError(400, "last_admin", "Oxirgi faol administratorni o'chirib yoki to'xtatib bo'lmaydi.")
    with db.tx() as c:
        c.execute("UPDATE users SET name=?, status=?, role=? WHERE id=?",
                  (body.name.strip() if body.name is not None else row["name"], new_status, new_role, user_id))
        if new_status != "active":
            c.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    return {"item": user_row_out(db.one(USER_SELECT + " WHERE u.id=:id", {"ms": month_start_ts(), "id": user_id}))}


@router.post("/users/{user_id}/archive")
def archive_user(user_id: int, admin=Depends(require_admin), db: Database = Depends(get_db)):
    """Takes an account out of use without deleting it.

    The account is never removed: its ledger and usage rows are what the balance
    invariants rest on, and the app role has no DELETE on those tables by design. So
    the account is marked archived (gone from the list), every key it owns is revoked
    and its sessions are dropped - it cannot sign in or spend.
    """
    row = db.one("SELECT * FROM users WHERE id=?", (user_id,))
    if row is None:
        raise ApiError(404, "user_not_found", "Foydalanuvchi topilmadi.")
    if row["status"] == "archived":
        raise ApiError(400, "already_archived", "Bu akkaunt allaqachon arxivlangan.")
    if user_id == admin["id"]:
        raise ApiError(400, "self_archive", "O'z akkauntingizni arxivlab bo'lmaydi.")
    if row["role"] == "admin" and row["status"] == "active" and _admin_count(db) <= 1:
        raise ApiError(400, "last_admin", "Oxirgi faol administratorni arxivlab bo'lmaydi.")
    ts = now()
    with db.tx() as c:
        c.execute("UPDATE users SET status='archived' WHERE id=?", (user_id,))
        c.execute("UPDATE api_keys SET status='revoked', revoked_at=? WHERE user_id=? AND status='active'",
                  (ts, user_id))
        c.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    audit.record(db, admin=dict(admin), action="user_archive", target=f"user:{user_id}", ok=True,
                 detail=row["email"])
    return {"ok": True}


@router.post("/users/{user_id}/restore")
def restore_user(user_id: int, admin=Depends(require_admin), db: Database = Depends(get_db)):
    """Brings an archived account back. Its keys stay revoked - new ones are made as usual."""
    row = db.one("SELECT * FROM users WHERE id=?", (user_id,))
    if row is None:
        raise ApiError(404, "user_not_found", "Foydalanuvchi topilmadi.")
    if row["status"] != "archived":
        raise ApiError(400, "not_archived", "Bu akkaunt arxivlanmagan.")
    with db.tx() as c:
        c.execute("UPDATE users SET status='suspended' WHERE id=?", (user_id,))
    audit.record(db, admin=dict(admin), action="user_restore", target=f"user:{user_id}", ok=True,
                 detail=row["email"])
    return {"ok": True}


class CreditIn(BaseModel):
    amount: int = Field(description="micro-sum; negative = debit")
    kind: str = Field(default="topup", pattern="^(topup|adjustment|refund|bonus)$")
    note: str = Field(default="", max_length=300)


@router.post("/users/{user_id}/credit")
def credit_user(user_id: int, body: CreditIn, admin=Depends(require_admin), db: Database = Depends(get_db)):
    if db.one("SELECT 1 FROM users WHERE id=?", (user_id,)) is None:
        raise ApiError(404, "user_not_found", "Foydalanuvchi topilmadi.")
    if body.kind in ("topup", "bonus", "refund") and body.amount < 0:
        raise ApiError(400, "invalid_amount", "Bu tur uchun summa musbat bo'lishi kerak. Yechish uchun 'tuzatish' dan foydalaning.")
    try:
        balance = credit(db, user_id=user_id, amount=body.amount, kind=body.kind, note=body.note.strip(), admin_id=admin["id"])
    except BillingError as e:
        raise ApiError(e.status, e.code, e.message)
    return {"balance": balance}


@router.post("/users/{user_id}/reset-password")
def reset_password(user_id: int, db: Database = Depends(get_db)):
    if db.one("SELECT 1 FROM users WHERE id=?", (user_id,)) is None:
        raise ApiError(404, "user_not_found", "Foydalanuvchi topilmadi.")
    password = security.new_temp_password()
    with db.tx() as c:
        c.execute("UPDATE users SET password_hash=? WHERE id=?", (security.hash_password(password), user_id))
        c.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    return {"temp_password": password}


@router.get("/users/{user_id}")
def user_detail(user_id: int, db: Database = Depends(get_db)):
    row = db.one(USER_SELECT + " WHERE u.id=:id", {"ms": month_start_ts(), "id": user_id})
    if row is None:
        raise ApiError(404, "user_not_found", "Foydalanuvchi topilmadi.")
    usage = db.all("""SELECT u.*, k.name AS key_name FROM usage_logs u LEFT JOIN api_keys k ON k.id=u.key_id
                      WHERE u.user_id=? ORDER BY u.id DESC LIMIT 20""", (user_id,))
    ledger = db.all("SELECT * FROM ledger WHERE user_id=? ORDER BY id DESC LIMIT 20", (user_id,))
    return {
        "item": user_row_out(row),
        "keys": keys_with_spend(db, "user_id=?", (user_id,)),
        "usage": [usage_out(r) for r in usage],
        "ledger": [{"id": r["id"], "kind": r["kind"], "amount": r["amount"], "balance_after": r["balance_after"],
                    "note": r["note"], "created_at": r["created_at"]} for r in ledger],
    }


# --- keys ----------------------------------------------------------------------------------------------------------
class KeyAdminPatch(BaseModel):
    rpm_limit: int | None = Field(default=None, ge=1, le=10000)
    clear_rpm_limit: bool = False
    monthly_limit: int | None = Field(default=None, ge=0)
    clear_monthly_limit: bool = False
    free: bool | None = None
    reasoning_effort: str | None = Field(default=None, pattern="^(inherit|none|low|medium|high|model)$")


@router.patch("/keys/{key_id}")
def patch_key_admin(key_id: int, body: KeyAdminPatch, db: Database = Depends(get_db)):
    row = db.one("SELECT * FROM api_keys WHERE id=?", (key_id,))
    if row is None:
        raise ApiError(404, "key_not_found", "Kalit topilmadi.")
    rpm = None if body.clear_rpm_limit else (body.rpm_limit if body.rpm_limit is not None else row["rpm_limit"])
    monthly = None if body.clear_monthly_limit else (body.monthly_limit if body.monthly_limit is not None else row["monthly_limit"])
    free = int(body.free) if body.free is not None else row["free"]
    effort = row["reasoning_effort"] if body.reasoning_effort is None else (None if body.reasoning_effort == "inherit" else body.reasoning_effort)
    with db.tx() as c:
        c.execute("UPDATE api_keys SET rpm_limit=?, monthly_limit=?, free=?, reasoning_effort=? WHERE id=?", (rpm, monthly, free, effort, key_id))
    return {"item": key_out(db.one("SELECT * FROM api_keys WHERE id=?", (key_id,)))}


@router.post("/keys/{key_id}/revoke")
def revoke_key_admin(key_id: int, db: Database = Depends(get_db)):
    if db.one("SELECT 1 FROM api_keys WHERE id=?", (key_id,)) is None:
        raise ApiError(404, "key_not_found", "Kalit topilmadi.")
    with db.tx() as c:
        c.execute("UPDATE api_keys SET status='revoked', revoked_at=? WHERE id=? AND status='active'", (now(), key_id))
    return {"ok": True}


# --- top-up requests -------------------------------------------------------------------------------------------------
@router.get("/topups")
def list_topups(status: str = "pending", db: Database = Depends(get_db)):
    where, args = ("t.status=?", [status]) if status in ("pending", "approved", "rejected") else ("1=1", [])
    rows = db.all(f"""SELECT t.*, u.email, u.name FROM topup_requests t JOIN users u ON u.id=t.user_id
                      WHERE {where} ORDER BY t.id DESC LIMIT 200""", args)
    return {"items": [{**topup_out(r), "user_id": r["user_id"], "email": r["email"], "user_name": r["name"]} for r in rows],
            "pending": db.scalar("SELECT COUNT(*) FROM topup_requests WHERE status='pending'")}


class ResolveIn(BaseModel):
    action: str = Field(pattern="^(approve|reject)$")
    amount: int | None = Field(default=None, gt=0, description="amount to approve (micro-sum); empty = requested amount")
    admin_note: str = Field(default="", max_length=300)


@router.post("/topups/{topup_id}/resolve")
def resolve_topup(topup_id: int, body: ResolveIn, background: BackgroundTasks, request: Request,
                  admin=Depends(require_admin), db: Database = Depends(get_db)):
    # Same path as the Telegram button (topups.resolve): a double click never credits twice.
    topups.resolve(db, topup_id, approve=body.action == "approve", admin_id=admin["id"], amount=body.amount,
                   note=body.admin_note)
    background.add_task(topup_telegram.sync_message, request.app, topup_id)
    return {"ok": True}


# --- models and pricing ------------------------------------------------------------------------------------------------
LEVEL_PATTERN = "^(none|low|medium|high|model)$"


@router.get("/models")
def admin_models(db: Database = Depends(get_db)):
    hard_max = int(db.get_settings()["max_max_tokens"])
    return {"items": [model_out(r) | {"max_output_tokens": hard_max} for r in db.all("SELECT * FROM models ORDER BY id")]}


class ModelIn(BaseModel):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._:/-]+$")
    upstream_name: str = Field(min_length=1, max_length=120)
    display_name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=300)
    price_in_per_1m: int = Field(ge=0)
    price_out_per_1m: int = Field(ge=0)
    context_window: int = Field(default=32768, ge=256)
    enabled: bool = True
    provider: str = Field(default="local", pattern="^(local|external|cluster)$")
    refine: bool = False
    listed: bool = True
    default_reasoning: str | None = Field(default=None, pattern=LEVEL_PATTERN)
    fallback_price_from: str | None = Field(default=None, max_length=80)
    fallback_to: str | None = Field(default=None, max_length=80)
    allow_thinking: bool = True


CLUSTER_CHANNELS = {"auto", "chatgpt", "deepseek", "qwen", "gemini", "genspark", "deepai", "zai", "grok", "chatbotapp", "chatbotchatapp"}


def _check_model_rules(db: Database, provider: str, refine: bool, fallback: str | None, own_id: str,
                       upstream_name: str | None = None, stand_in: str | None = None) -> None:
    if refine and provider != "local":
        raise ApiError(400, "invalid_model", "Tekshiruvli rejim faqat mahalliy model uchun.")
    if provider == "cluster" and upstream_name and upstream_name.lower() not in CLUSTER_CHANNELS:
        raise ApiError(400, "invalid_model",
                       f"Cluster uchun 'upstream_name' quyidagilardan biri bo'lishi kerak: {', '.join(sorted(CLUSTER_CHANNELS))}.")
    if fallback and (fallback == own_id or db.one("SELECT 1 FROM models WHERE id=?", (fallback,)) is None):
        raise ApiError(400, "invalid_model", "'Narx manbai' mavjud boshqa model bo'lishi kerak.")
    if stand_in and (stand_in == own_id or db.one("SELECT 1 FROM models WHERE id=?", (stand_in,)) is None):
        raise ApiError(400, "invalid_model", "'Zaxira model' mavjud boshqa model bo'lishi kerak.")


@router.post("/models")
def add_model(body: ModelIn, db: Database = Depends(get_db)):
    if db.one("SELECT 1 FROM models WHERE id=?", (body.id,)):
        raise ApiError(409, "model_exists", "Bunday model allaqachon bor.")
    _check_model_rules(db, body.provider, body.refine, body.fallback_price_from, body.id, body.upstream_name,
                       body.fallback_to)
    with db.tx() as c:
        c.execute("""INSERT INTO models(id, upstream_name, display_name, description, price_in_per_1m, price_out_per_1m,
                         context_window, enabled, created_at, provider, refine, listed, default_reasoning, fallback_price_from,
                         fallback_to, allow_thinking)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (body.id, body.upstream_name, body.display_name, body.description, body.price_in_per_1m,
                   body.price_out_per_1m, body.context_window, int(body.enabled), now(), body.provider, int(body.refine),
                   int(body.listed), body.default_reasoning, body.fallback_price_from, body.fallback_to,
                   int(body.allow_thinking)))
    return {"ok": True}


# Sending these as null clears them; every other field keeps its value when omitted.
CLEARABLE = ("default_reasoning", "fallback_price_from", "fallback_to")


class ModelPatch(BaseModel):
    upstream_name: str | None = Field(default=None, min_length=1, max_length=120)
    display_name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=300)
    price_in_per_1m: int | None = Field(default=None, ge=0)
    price_out_per_1m: int | None = Field(default=None, ge=0)
    context_window: int | None = Field(default=None, ge=256)
    enabled: bool | None = None
    provider: str | None = Field(default=None, pattern="^(local|external|cluster)$")
    refine: bool | None = None
    listed: bool | None = None
    default_reasoning: str | None = Field(default=None, pattern=LEVEL_PATTERN)
    fallback_price_from: str | None = Field(default=None, max_length=80)
    fallback_to: str | None = Field(default=None, max_length=80)
    allow_thinking: bool | None = None


@router.patch("/models/{model_id:path}")
def patch_model(model_id: str, body: ModelPatch, db: Database = Depends(get_db)):
    row = db.one("SELECT * FROM models WHERE id=?", (model_id,))
    if row is None:
        raise ApiError(404, "model_not_found", "Model topilmadi.")
    given = body.model_dump(exclude_unset=True)
    new = {k: row[k] for k in row.keys()}
    for k, v in given.items():
        if v is None and k not in CLEARABLE:
            continue                       # an empty value clears only these fields
        new[k] = int(v) if isinstance(v, bool) else (v or None if k in CLEARABLE else v)
    _check_model_rules(db, new["provider"], bool(new["refine"]), new["fallback_price_from"], model_id,
                       new["upstream_name"], new["fallback_to"])
    with db.tx() as c:
        c.execute("""UPDATE models SET upstream_name=?, display_name=?, description=?, price_in_per_1m=?, price_out_per_1m=?,
                         context_window=?, enabled=?, provider=?, refine=?, listed=?, default_reasoning=?, fallback_price_from=?,
                         fallback_to=?, allow_thinking=?
                     WHERE id=?""",
                  (new["upstream_name"], new["display_name"], new["description"], new["price_in_per_1m"], new["price_out_per_1m"],
                   new["context_window"], new["enabled"], new["provider"], new["refine"], new["listed"],
                   new["default_reasoning"], new["fallback_price_from"], new["fallback_to"],
                   new["allow_thinking"], model_id))
    return {"ok": True}


@router.get("/upstream-models")
async def upstream_models(request: Request, db: Database = Depends(get_db)):
    """Models installed on the upstream Ollama host (for adding a new model)."""
    state = request.app.state
    try:
        r = await state.http.get(state.config.upstream_url + "/api/tags", timeout=5.0)
        r.raise_for_status()
        names = sorted(m["name"] for m in r.json().get("models", []))
    except Exception:
        raise ApiError(503, "model_offline", "Model serveriga ulanib bo'lmadi.", "server_error")
    known = {r["upstream_name"] for r in db.all("SELECT upstream_name FROM models WHERE provider='local'")}
    return {"items": [{"name": n, "added": n in known} for n in names]}


async def _external_models(request: Request, db: Database) -> list[str]:
    s = await run_in_threadpool(db.get_settings)
    base, key = (s.get("external_base_url") or "").rstrip("/"), s.get("external_api_key") or ""
    if not (base and key):
        raise ApiError(400, "external_not_configured", "Tashqi provayder sozlanmagan: manzil va kalitni kiriting.")
    try:
        r = await request.app.state.ext_http.get(f"{base}/models", headers={"Authorization": f"Bearer {key}", "User-Agent": "ai-gateway/1.0"},
                                                 timeout=15.0)
    except Exception as e:
        raise ApiError(502, "external_unreachable", f"Tashqi provayderga ulanib bo'lmadi ({type(e).__name__}).", "server_error")
    if r.status_code in (401, 403):
        raise ApiError(400, "external_bad_key", "Tashqi provayder kalitni qabul qilmadi.")
    if r.status_code != 200:
        raise ApiError(502, "external_error", f"Tashqi provayder javobi: HTTP {r.status_code}.", "server_error")
    try:
        return sorted(m["id"] for m in r.json()["data"])
    except Exception:
        raise ApiError(502, "external_error", "Tashqi provayder noto'g'ri javob qaytardi.", "server_error")


@router.get("/external/models")
async def external_models(request: Request, db: Database = Depends(get_db)):
    names = await _external_models(request, db)
    known = {r["upstream_name"] for r in db.all("SELECT upstream_name FROM models WHERE provider='external'")}
    return {"items": [{"name": n, "added": n in known} for n in names]}


@router.post("/external/test")
async def external_test(request: Request, db: Database = Depends(get_db)):
    names = await _external_models(request, db)
    refiner = db.get_settings().get("refiner_model", "")
    return {"ok": True, "models": len(names), "refiner_model": refiner, "refiner_found": refiner in names}


# --- AI cluster ---------------------------------------------------------------------------------------------
def _cluster_target(db: Database) -> tuple[str, dict]:
    """The cluster's CONTROL base URL (root) and headers.

    The `cluster_base_url` setting is stored as an OpenAI path (".../v1") because the
    gateway appends `/chat/completions` to it. Control endpoints (`/cluster/...`,
    `/v1/models`) are written from the root, so `/v1` is stripped. Without this every
    admin-panel cluster request went to `/v1/cluster/...` and got 404 (cluster_watch
    already did the same stripping).
    """
    s = db.get_settings()
    base = (s.get("cluster_base_url") or "").rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    if not base:
        raise ApiError(400, "cluster_not_configured", "Cluster sozlanmagan: manzilni kiriting.")
    headers = {"User-Agent": "ai-gateway/1.0"}
    key = s.get("cluster_api_key") or ""
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return base, headers


@router.get("/cluster/status")
async def cluster_status(request: Request, db: Database = Depends(get_db)):
    base, headers = await run_in_threadpool(_cluster_target, db)
    try:
        r = await request.app.state.cluster_http.get(f"{base}/cluster/status", headers=headers, timeout=10.0)
    except Exception as e:
        raise ApiError(502, "cluster_unreachable", f"Cluster shlyuziga ulanib bo'lmadi ({type(e).__name__}).", "server_error")
    if r.status_code in (401, 403):
        raise ApiError(400, "cluster_bad_key", "Cluster shlyuzi kalitni qabul qilmadi.")
    if r.status_code != 200:
        raise ApiError(502, "cluster_error", f"Cluster javobi: HTTP {r.status_code}.", "server_error")
    return r.json()


@router.post("/cluster/refresh")
async def cluster_refresh(request: Request, db: Database = Depends(get_db)):
    base, headers = await run_in_threadpool(_cluster_target, db)
    try:
        r = await request.app.state.cluster_http.post(f"{base}/cluster/refresh", headers=headers, timeout=15.0)
    except Exception as e:
        raise ApiError(502, "cluster_unreachable", f"Cluster shlyuziga ulanib bo'lmadi ({type(e).__name__}).", "server_error")
    if r.status_code != 200:
        raise ApiError(502, "cluster_error", f"Cluster javobi: HTTP {r.status_code}.", "server_error")
    return r.json()


@router.get("/cluster/models")
async def cluster_models(request: Request, db: Database = Depends(get_db)):
    """Channels available in the cluster, so the admin can see which ones can be added as models."""
    base, headers = await run_in_threadpool(_cluster_target, db)
    try:
        r = await request.app.state.cluster_http.get(f"{base}/v1/models", headers=headers, timeout=10.0)
    except Exception as e:
        raise ApiError(502, "cluster_unreachable", f"Cluster shlyuziga ulanib bo'lmadi ({type(e).__name__}).", "server_error")
    if r.status_code in (401, 403):
        raise ApiError(400, "cluster_bad_key", "Cluster shlyuzi kalitni qabul qilmadi.")
    if r.status_code != 200:
        raise ApiError(502, "cluster_error", f"Cluster javobi: HTTP {r.status_code}.", "server_error")
    data = r.json().get("data") or []
    known = {row["upstream_name"] for row in db.all("SELECT upstream_name FROM models WHERE provider='cluster'")}
    return {"items": [{"name": m["id"], "active_workers": m.get("active_workers", 0),
                       "available_workers": m.get("available_workers", 0), "added": m["id"] in known} for m in data]}


@router.post("/cluster/probe")
async def cluster_probe(request: Request, db: Database = Depends(get_db)):
    """Checks the cluster workers' sessions (sends no prompt, spends no quota)."""
    base, headers = await run_in_threadpool(_cluster_target, db)
    try:
        r = await request.app.state.cluster_http.post(f"{base}/cluster/probe", headers=headers, timeout=90.0)
    except Exception as e:
        raise ApiError(502, "cluster_unreachable", f"Cluster shlyuziga ulanib bo'lmadi ({type(e).__name__}).", "server_error")
    if r.status_code != 200:
        raise ApiError(502, "cluster_error", f"Cluster javobi: HTTP {r.status_code}.", "server_error")
    return r.json()


@router.get("/cluster/metrics")
async def cluster_metrics_proxy(request: Request, db: Database = Depends(get_db)):
    """Cluster request statistics: latency histogram, success rate, per-model breakdown."""
    base, headers = await run_in_threadpool(_cluster_target, db)
    try:
        r = await request.app.state.cluster_http.get(f"{base}/cluster/metrics", headers=headers, timeout=10.0)
    except Exception as e:
        raise ApiError(502, "cluster_unreachable", f"Cluster shlyuziga ulanib bo'lmadi ({type(e).__name__}).", "server_error")
    if r.status_code != 200:
        raise ApiError(502, "cluster_error", f"Cluster javobi: HTTP {r.status_code}.", "server_error")
    return r.json()


@router.get("/cluster/proxies")
async def cluster_proxies_status(request: Request, db: Database = Depends(get_db)):
    """Cluster proxy pool status."""
    base, headers = await run_in_threadpool(_cluster_target, db)
    try:
        r = await request.app.state.cluster_http.get(f"{base}/cluster/proxies", headers=headers, timeout=10.0)
    except Exception as e:
        raise ApiError(502, "cluster_unreachable", f"Cluster shlyuziga ulanib bo'lmadi ({type(e).__name__}).", "server_error")
    if r.status_code != 200:
        raise ApiError(502, "cluster_error", f"Cluster javobi: HTTP {r.status_code}.", "server_error")
    return r.json()


class ProxyRefreshIn(BaseModel):
    url: str | None = Field(default=None, max_length=500)


@router.post("/cluster/proxies/refresh")
async def cluster_proxies_refresh(body: ProxyRefreshIn, request: Request, db: Database = Depends(get_db)):
    """Reloads the cluster proxy list from a URL. If the URL is empty the cluster uses the value from its own env."""
    base, headers = await run_in_threadpool(_cluster_target, db)
    try:
        r = await request.app.state.cluster_http.post(
            f"{base}/cluster/proxies/refresh", headers=headers,
            json={"url": body.url} if body.url else {}, timeout=30.0,
        )
    except Exception as e:
        raise ApiError(502, "cluster_unreachable", f"Cluster shlyuziga ulanib bo'lmadi ({type(e).__name__}).", "server_error")
    if r.status_code != 200:
        raise ApiError(502, "cluster_error", f"Cluster javobi: HTTP {r.status_code}.", "server_error")
    return r.json()


# --- quality samples ----------------------------------------------------------------------------------------------------
class SampleIn(BaseModel):
    category: str = Field(min_length=1, max_length=40)
    prompt: str = Field(min_length=1, max_length=2000)
    fast_model: str = Field(max_length=80)
    fast_answer: str = Field(max_length=20000)
    fast_ms: int | None = Field(default=None, ge=0)
    pro_model: str = Field(max_length=80)
    pro_answer: str = Field(max_length=20000)
    pro_ms: int | None = Field(default=None, ge=0)
    pro_verified: bool = False


@router.put("/samples")
def put_samples(items: list[SampleIn], db: Database = Depends(get_db)):
    """Replaces all samples. Texts must come from real requests (generated by a script), never edited by hand."""
    if len(items) > 20:
        raise ApiError(400, "too_many_samples", "Namunalar soni 20 tadan oshmasligi kerak.")
    ts = now()
    with db.tx() as c:
        c.execute("DELETE FROM samples")
        for i, s in enumerate(items):
            c.execute("""INSERT INTO samples(position, category, prompt, fast_model, fast_answer, fast_ms, pro_model, pro_answer, pro_ms,
                             pro_verified, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                      (i, s.category, s.prompt, s.fast_model, s.fast_answer, s.fast_ms, s.pro_model, s.pro_answer, s.pro_ms, int(s.pro_verified), ts))
    return {"ok": True, "count": len(items)}


# --- settings ----------------------------------------------------------------------------------------------------------
class SettingsIn(BaseModel):
    registration_open: bool | None = None
    free_mode: bool | None = None
    signup_bonus_micro: int | None = Field(default=None, ge=0)
    default_rpm: int | None = Field(default=None, ge=1, le=10000)
    free_daily_token_limit: int | None = Field(default=None, ge=0, le=1_000_000_000)   # 0 = unlimited
    free_daily_limit_models: str | None = Field(default=None, max_length=400)
    default_max_tokens: int | None = Field(default=None, ge=1, le=131072)
    max_max_tokens: int | None = Field(default=None, ge=1, le=131072)
    min_topup_micro: int | None = Field(default=None, ge=0)
    payment_info: str | None = Field(default=None, max_length=1500)
    default_reasoning_effort: str | None = Field(default=None, pattern="^(none|low|medium|high)?$")
    alert_telegram_token: str | None = Field(default=None, max_length=200)
    # Temporarily unreliable models (comma-separated): shown to users as
    # "maintenance in progress"
    maintenance_models: str | None = Field(default=None, max_length=400)
    # Sunset date for legacy model names (model-1.0 … 4.0) (YYYY-MM-DD); empty = not announced (app/directions.py)
    legacy_models_sunset: str | None = Field(default=None, pattern=r"^(\d{4}-\d{2}-\d{2})?$")
    # Date the public status page starts counting from (YYYY-MM-DD); empty = no limit
    status_public_since: str | None = Field(default=None, pattern=r"^(\d{4}-\d{2}-\d{2})?$")
    clear_alert_token: bool = False
    alert_telegram_chat_id: str | None = Field(default=None, max_length=64)
    external_base_url: str | None = Field(default=None, max_length=200, pattern=r"^(https://[^\s]+)?$")
    external_api_key: str | None = Field(default=None, max_length=300)
    clear_external_api_key: bool = False
    refiner_model: str | None = Field(default=None, max_length=240)
    refine_timeout: int | None = Field(default=None, ge=3, le=120)
    cluster_base_url: str | None = Field(default=None, max_length=200, pattern=r"^(https?://[^\s]+)?$")
    cluster_api_key: str | None = Field(default=None, max_length=300)
    clear_cluster_api_key: bool = False
    # --- Sign-in via Google and Telegram ---
    site_base_url: str | None = Field(default=None, max_length=200, pattern=r"^(https?://[^\s]+)?$")
    google_client_id: str | None = Field(default=None, max_length=300)
    google_client_secret: str | None = Field(default=None, max_length=300)
    telegram_login_token: str | None = Field(default=None, max_length=200)
    telegram_login_bot: str | None = Field(default=None, max_length=64)
    telegram_login_enabled: str | None = Field(default=None, max_length=1)
    clear_google_client_secret: bool = False
    telegram_login_enabled: bool | None = None
    telegram_login_bot: str | None = Field(default=None, max_length=64, pattern=r"^@?[A-Za-z0-9_]{0,64}$")
    # --- Captcha (Google reCAPTCHA v3) ---
    captcha_enabled: bool | None = None
    cluster_fallback_enabled: bool | None = None
    # Fast path (app/fastpath.py): simple messages go to the fast model on the GPU
    fastpath_enabled: bool | None = None
    fastpath_model: str | None = Field(default=None, max_length=80, pattern=r"^[A-Za-z0-9._:/-]*$")
    recaptcha_site_key: str | None = Field(default=None, max_length=100, pattern=r"^\s*[A-Za-z0-9_-]*\s*$")
    recaptcha_secret: str | None = Field(default=None, max_length=200)
    clear_recaptcha_secret: bool = False
    captcha_min_score: float | None = Field(default=None, ge=0.0, le=1.0)
    captcha_hostnames: str | None = Field(default=None, max_length=400, pattern=r"^[A-Za-z0-9.,\s-]*$")
    # Login locks for 15 minutes on the 8th failure per email (routes_auth); keep this below that
    captcha_login_after_fails: int | None = Field(default=None, ge=0, le=7)
    # --- Top-up requests in Telegram (topup_telegram.py); the webhook secret is not changed here ---
    topup_tg_enabled: bool | None = None
    topup_tg_chat_id: str | None = Field(default=None, pattern=r"^\s*(-?\d{1,20})?\s*$")
    topup_tg_admin_ids: str | None = Field(default=None, max_length=300, pattern=r"^[\d,\s]*$")


@router.get("/settings")
def get_settings(request: Request, db: Database = Depends(get_db)):
    s = db.get_settings()
    cfg = request.app.state.config
    text_keys = ("registration_open", "free_mode", "payment_info", "default_reasoning_effort", "alert_telegram_token", "alert_telegram_chat_id",
                 "last_daily_report", "external_base_url", "external_api_key", "refiner_model",
                 "cluster_base_url", "cluster_api_key",
                 "site_base_url", "google_client_id", "google_client_secret",
                 "telegram_login_token", "telegram_login_bot", "telegram_login_enabled",
                 "telegram_login_enabled", "telegram_login_bot",
                 "captcha_enabled", "recaptcha_site_key", "recaptcha_secret",
                 "captcha_min_score", "captcha_hostnames", "cluster_fallback_enabled",
                 "topup_tg_enabled", "topup_tg_chat_id", "topup_tg_admin_ids", "topup_tg_webhook_secret")
    return {"registration_open": s["registration_open"] == "1", "free_mode": s.get("free_mode") == "1", "payment_info": s.get("payment_info", ""),
            "default_reasoning_effort": s.get("default_reasoning_effort", ""),
            "alert_telegram_chat_id": s.get("alert_telegram_chat_id", ""),
            "alert_telegram_token_set": bool(s.get("alert_telegram_token")),   # the token itself is never returned
            "external_base_url": s.get("external_base_url", ""), "refiner_model": s.get("refiner_model", ""),
            "external_api_key_set": bool(s.get("external_api_key")),         # nor is the key itself
            "cluster_base_url": s.get("cluster_base_url", ""),
            "cluster_api_key_set": bool(s.get("cluster_api_key")),
            "maintenance_models": s.get("maintenance_models", ""),
            "site_base_url": s.get("site_base_url", ""),
            "google_client_id": s.get("google_client_id", ""),
            "google_client_secret_set": bool(s.get("google_client_secret")),  # the secret itself is never returned
            "telegram_login_token_set": bool(s.get("telegram_login_token")),
            "telegram_login_bot": s.get("telegram_login_bot", ""),
            "telegram_login_enabled": s.get("telegram_login_enabled", "0"),
            "telegram_login_enabled": s.get("telegram_login_enabled") == "1",
            "telegram_login_bot": s.get("telegram_login_bot", ""),
            "captcha_enabled": s.get("captcha_enabled", "1") == "1",
            "cluster_fallback_enabled": s.get("cluster_fallback_enabled") == "1",
            "fastpath_enabled": s.get("fastpath_enabled", "1") == "1",
            "recaptcha_site_key": s.get("recaptcha_site_key", ""),
            "recaptcha_secret_set": bool(s.get("recaptcha_secret")),   # the secret itself is never returned
            # keys in /etc/ai-gateway.env take precedence over the admin setting
            "recaptcha_env_keys": bool(cfg.recaptcha_site_key and cfg.recaptcha_secret),
            "captcha_min_score": captcha.min_score(s),
            "captcha_hostnames": s.get("captcha_hostnames", captcha.DEFAULT_HOSTNAMES),
            "captcha_env_disabled": not cfg.captcha_enabled,
            "captcha_active": captcha.settings(db, cfg) is not None,
            "topup_tg_enabled": s.get("topup_tg_enabled") == "1",
            "topup_tg_chat_id": s.get("topup_tg_chat_id", ""),
            "topup_tg_admin_ids": s.get("topup_tg_admin_ids", ""),
            "topup_tg_webhook_set": bool(s.get("topup_tg_webhook_secret")),   # the secret itself is never returned
            # Remaining settings: only values that REALLY are integers are
            # converted. A "not in the list means number" rule would make
            # every new text setting crash this page with a 500.
            **{k: _int_or_text(v) for k, v in s.items()
               if k not in text_keys and k != "maintenance_models"}}


def _int_or_text(value: str):
    """Returns the value as an int if it is an integer, otherwise unchanged."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


@router.put("/settings")
def put_settings(body: SettingsIn, request: Request, db: Database = Depends(get_db)):
    changes = body.model_dump(exclude_none=True)
    if "payment_info" in changes:
        changes["payment_info"] = changes["payment_info"].strip()
    if changes.pop("clear_alert_token", False):
        changes["alert_telegram_token"] = ""
    if changes.pop("clear_external_api_key", False):
        changes["external_api_key"] = ""
    if changes.pop("clear_cluster_api_key", False):
        changes["cluster_api_key"] = ""
    if changes.pop("clear_google_client_secret", False):
        changes["google_client_secret"] = ""
    if changes.pop("clear_recaptcha_secret", False):
        changes["recaptcha_secret"] = ""
    for k in ("recaptcha_site_key", "recaptcha_secret"):
        if k in changes:
            changes[k] = changes[k].strip()
    if "captcha_hostnames" in changes:
        changes["captcha_hostnames"] = ",".join(
            h.strip().lower() for h in changes["captcha_hostnames"].split(",") if h.strip())
    if changes.get("recaptcha_secret") == "" and "recaptcha_secret" in body.model_fields_set \
            and not body.clear_recaptcha_secret:
        changes.pop("recaptcha_secret")  # an empty field must not erase the stored secret
    for k in ("external_base_url", "external_api_key", "refiner_model", "cluster_base_url", "cluster_api_key",
              "site_base_url", "google_client_id", "google_client_secret"):
        if k in changes:
            changes[k] = changes[k].strip()
    if "free_daily_limit_models" in changes:
        changes["free_daily_limit_models"] = ",".join(m.strip() for m in changes["free_daily_limit_models"].split(",") if m.strip())
    if "topup_tg_chat_id" in changes:
        changes["topup_tg_chat_id"] = changes["topup_tg_chat_id"].strip()
    if "topup_tg_admin_ids" in changes:
        changes["topup_tg_admin_ids"] = ",".join(x.strip() for x in changes["topup_tg_admin_ids"].split(",") if x.strip())
    if "telegram_login_bot" in changes:
        changes["telegram_login_bot"] = changes["telegram_login_bot"].strip().lstrip("@")
    # An empty field must not erase the stored secret (same rule as the other keys).
    if changes.get("google_client_secret") == "" and "google_client_secret" in body.model_fields_set \
            and not body.clear_google_client_secret:
        changes.pop("google_client_secret")
    if changes.get("external_api_key") == "" and "external_api_key" in body.model_fields_set and not body.clear_external_api_key:
        changes.pop("external_api_key")  # an empty field must not erase the existing key
    if changes.get("cluster_api_key") == "" and "cluster_api_key" in body.model_fields_set and not body.clear_cluster_api_key:
        changes.pop("cluster_api_key")
    for k in ("alert_telegram_token", "alert_telegram_chat_id"):
        if k in changes:
            changes[k] = changes[k].strip()
    if changes.get("alert_telegram_token") == "" and "alert_telegram_token" in body.model_fields_set and not body.clear_alert_token:
        changes.pop("alert_telegram_token")  # an empty field must not erase the existing token
    if changes.get("default_max_tokens", 0) > changes.get("max_max_tokens", db.setting_int("max_max_tokens")):
        raise ApiError(400, "invalid_limits", "Standart max_tokens yuqori chegaradan oshmasligi kerak.")
    for k, v in changes.items():
        db.set_setting(k, "1" if v is True else "0" if v is False else str(v))
    return get_settings(request, db)


# --- statistics ----------------------------------------------------------------------------------------------------------
@router.get("/usage")
def all_usage(db: Database = Depends(get_db), limit: int = 50, offset: int = 0, user_id: int | None = None):
    limit, offset = page_args(limit, offset)
    where, args = ("u.user_id=?", [user_id]) if user_id else ("1=1", [])
    total = db.scalar(f"SELECT COUNT(*) FROM usage_logs u WHERE {where}", args)
    rows = db.all(f"""SELECT u.*, k.name AS key_name, us.email AS email FROM usage_logs u
                      LEFT JOIN api_keys k ON k.id=u.key_id LEFT JOIN users us ON us.id=u.user_id
                      WHERE {where} ORDER BY u.id DESC LIMIT ? OFFSET ?""", args + [limit, offset])
    return {"total": total, "items": [usage_out(r) | {"email": r["email"]} for r in rows]}


@router.get("/stats")
async def stats(request: Request, days: int = 30, db: Database = Depends(get_db)):
    days = max(1, min(days, 90))
    ms = month_start_ts()
    since = int(datetime.now(TASHKENT).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()) - (days - 1) * 86400
    total = db.one("""SELECT COUNT(*) n, COALESCE(SUM(cost),0) cost, COALESCE(SUM(prompt_tokens),0) pt,
                             COALESCE(SUM(completion_tokens),0) ct,
                             COALESCE(SUM(CASE WHEN status_code>=400 THEN 1 ELSE 0 END),0) errors,
                             COALESCE(AVG(CASE WHEN status_code=200 THEN latency_ms END),0) latency
                      FROM usage_logs WHERE created_at>=?""", (since,))
    top = db.all("""SELECT u.id, u.email, u.name, COUNT(*) n, COALESCE(SUM(l.cost),0) cost
                    FROM usage_logs l JOIN users u ON u.id=l.user_id WHERE l.created_at>=?
                    GROUP BY u.id ORDER BY cost DESC, n DESC LIMIT 8""", (since,))
    return {
        "days": days,
        "totals": {"requests": total["n"], "cost": total["cost"], "prompt_tokens": total["pt"],
                   "completion_tokens": total["ct"], "errors": total["errors"], "avg_latency_ms": int(total["latency"])},
        "balances": {"total": db.scalar("SELECT COALESCE(SUM(balance),0) FROM wallets"),
                     "month_cost": db.scalar("SELECT COALESCE(SUM(cost),0) FROM usage_logs WHERE created_at>=?", (ms,)),
                     "month_topups": db.scalar("SELECT COALESCE(SUM(amount),0) FROM ledger WHERE kind='topup' AND created_at>=?", (ms,))},
        "users": {"total": db.scalar("SELECT COUNT(*) FROM users"),
                  "active_period": db.scalar("SELECT COUNT(DISTINCT user_id) FROM usage_logs WHERE created_at>=?", (since,))},
        "pending_topups": db.scalar("SELECT COUNT(*) FROM topup_requests WHERE status='pending'"),
        "daily": daily_series(db, "1=1", [], days),
        "top_users": [{"id": r["id"], "email": r["email"], "name": r["name"], "requests": r["n"], "cost": r["cost"]} for r in top],
        "model_online": await upstream_online(request),
    }
