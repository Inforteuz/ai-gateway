"""Top-up requests in the owner's Telegram: a message with "Approve" / "Reject" buttons.

Flow:
  1. A user submits a request -> `notify_new` sends a message to the owner's chat (message id is stored).
  2. The owner presses a button -> Telegram sends a callback to `/portal/telegram/topup-webhook`.
  3. Checks: the secret header (X-Telegram-Bot-Api-Secret-Token), the presser is an allowed
     Telegram user_id, the message is in the configured chat. Then EXACTLY the same path as in the
     admin panel: `topups.resolve` (ledger, referral reward, idempotency).
  4. The message is updated with the status and the buttons are removed, also when resolved in the
     panel (`sync_message`).

The bot is the LOGIN bot (`telegram_login_token`), NOT the alert bot: the alert bot is consumed via
getUpdates by a separate agent, and setting a webhook on it would stop that consumer.

Callback data carries only the action and request id (`topup:approve:12`); the amount always comes
from the database. Only the type of httpx exceptions is logged, so the bot token never reaches the
log (their text contains the URL, i.e. the token).
"""
from __future__ import annotations

import hmac
import logging
import re
import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from . import audit, branding, topups
from .billing import TASHKENT
from .common import ApiError, csrf_guard, require_admin
from .db import Database
from .monitor import fmt_som

log = logging.getLogger("topup_telegram")

TELEGRAM_API = "https://api.telegram.org"
WEBHOOK_PATH = "/portal/telegram/topup-webhook"
SECRET_HEADER = "x-telegram-bot-api-secret-token"
CALLBACK_RE = re.compile(r"^topup:(approve|reject):(\d{1,12})$")

router = APIRouter(tags=["telegram"])
admin_router = APIRouter(prefix="/portal/admin/telegram", dependencies=[Depends(csrf_guard), Depends(require_admin)],
                         tags=["admin"])


# --- settings ----------------------------------------------------------------------------------------------------
def bot_token(s: dict) -> str:
    return (s.get("telegram_login_token") or "").strip()


def chat_id(s: dict) -> str:
    return (s.get("topup_tg_chat_id") or "").strip() or (s.get("alert_telegram_chat_id") or "").strip()


def admin_ids(s: dict) -> set[str]:
    """Telegram user_ids allowed to press the buttons. If empty, the owner of the private chat
    (in a private chat chat_id == user_id). When posting to a group the list must be explicit."""
    ids = {x.strip() for x in (s.get("topup_tg_admin_ids") or "").split(",") if x.strip()}
    if ids:
        return ids
    chat = chat_id(s)
    return {chat} if chat.isdigit() else set()


def enabled(s: dict) -> bool:
    return s.get("topup_tg_enabled") == "1" and bool(bot_token(s)) and bool(chat_id(s))


# --- Telegram API --------------------------------------------------------------------------------------------------
async def call(app, token: str, method: str, payload: dict) -> dict | None:
    """Bot API call. None on error; the token never reaches the log."""
    try:
        r = await app.state.notify_http.post(f"{TELEGRAM_API}/bot{token}/{method}", json=payload)
    except Exception as e:
        log.warning("telegram %s: connection failed (%s)", method, type(e).__name__)
        return None
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code != 200 or not data.get("ok"):
        log.warning("telegram %s: HTTP %s %s", method, r.status_code, str(data.get("description", ""))[:200])
        return None
    return data


# --- message text --------------------------------------------------------------------------------------------------
def _time(ts: int) -> str:
    return datetime.fromtimestamp(ts, TASHKENT).strftime("%d.%m.%Y %H:%M")


def _load(db: Database, topup_id: int):
    return db.one("""SELECT t.*, u.email, u.name, w.balance,
                            r.email AS resolver_email, r.name AS resolver_name
                     FROM topup_requests t JOIN users u ON u.id=t.user_id
                     LEFT JOIN wallets w ON w.user_id=t.user_id
                     LEFT JOIN users r ON r.id=t.resolved_by
                     WHERE t.id=?""", (topup_id,))


def render(row, who: str | None = None) -> str:
    """Plain text (no parse_mode): whatever the user's note contains, it is never parsed as markup."""
    lines = [f"💰 To'ldirish so'rovi #{row['id']}",
             f"Foydalanuvchi: {row['name'] or '—'} <{row['email']}> (id {row['user_id']})",
             f"Summa: {fmt_som(row['amount'])}",
             f"To'lov / izoh: {row['note'] or '—'}",
             f"Yuborilgan: {_time(row['created_at'])}",
             f"Hozirgi balans: {fmt_som(row['balance'] or 0)}"]
    if row["status"] != "pending":
        who = who or row["resolver_name"] or row["resolver_email"] or "Telegram orqali"
        mark = "✅ Tasdiqlandi" if row["status"] == "approved" else "❌ Rad etildi"
        lines += ["", f"{mark} — {who}, {_time(row['resolved_at'])}"]
        if row["admin_note"]:
            lines.append(f"Admin izohi: {row['admin_note']}")
    return "\n".join(lines)


def keyboard(topup_id: int) -> dict:
    return {"inline_keyboard": [[{"text": "✅ Tasdiqlash", "callback_data": f"topup:approve:{topup_id}"},
                                 {"text": "❌ Rad etish", "callback_data": f"topup:reject:{topup_id}"}]]}


# --- sending and updating ------------------------------------------------------------------------------------------
async def notify_new(app, topup_id: int) -> None:
    """Notifies about a new request (background task: does not delay the user's response)."""
    db: Database = app.state.db
    s = await run_in_threadpool(db.get_settings)
    if not enabled(s):
        return
    row = await run_in_threadpool(_load, db, topup_id)
    if row is None or row["status"] != "pending":
        return
    chat = chat_id(s)
    data = await call(app, bot_token(s), "sendMessage",
                      {"chat_id": chat, "text": render(row), "reply_markup": keyboard(topup_id),
                       "disable_web_page_preview": True})
    if data is None:
        return
    await _ensure_tracked(db, topup_id, chat, data["result"].get("message_id"))
    # If it was resolved in the panel while the message was being sent, do not leave the buttons.
    row = await run_in_threadpool(_load, db, topup_id)
    if row is not None and row["status"] != "pending":
        await sync_message(app, topup_id)


async def sync_message(app, topup_id: int, who: str | None = None) -> None:
    """Updates a resolved request's message with its status and removes the buttons."""
    db: Database = app.state.db
    row = await run_in_threadpool(_load, db, topup_id)
    if row is None or not row["tg_message_id"] or row["status"] == "pending":
        return
    s = await run_in_threadpool(db.get_settings)
    token = bot_token(s)
    if not token:
        return
    await call(app, token, "editMessageText",
               {"chat_id": row["tg_chat_id"], "message_id": row["tg_message_id"], "text": render(row, who),
                "reply_markup": {"inline_keyboard": []}, "disable_web_page_preview": True})


# --- webhook --------------------------------------------------------------------------------------------------------
def _display_name(user: dict) -> str:
    name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x) or "—"
    return f"{name} (@{user['username']})" if user.get("username") else name


def _linked_admin(db: Database, telegram_id: str):
    """The admin linked to this Telegram account (if any), for `resolved_by` and the ledger `admin_id`."""
    return db.one("SELECT id, email FROM users WHERE telegram_id=? AND role='admin' AND status='active'",
                  (telegram_id,))


async def handle_callback(app, s: dict, cq: dict) -> None:
    db: Database = app.state.db
    token = bot_token(s)

    async def answer(text: str, alert: bool = False):
        await call(app, token, "answerCallbackQuery",
                   {"callback_query_id": cq.get("id"), "text": text, "show_alert": alert})

    sender = cq.get("from") or {}
    from_id = str(sender.get("id", ""))
    msg = cq.get("message") or {}
    msg_chat = str((msg.get("chat") or {}).get("id", ""))
    m = CALLBACK_RE.match(str(cq.get("data") or ""))
    if m is None:
        await answer("Noma'lum amal.")
        return
    if not enabled(s) or from_id not in admin_ids(s) or msg_chat != chat_id(s):
        log.warning("top-up button rejected: telegram user %s, chat %s", from_id or "?", msg_chat or "?")
        await answer("Sizda bu amal uchun ruxsat yo'q.", alert=True)
        return

    action, topup_id = m.group(1), int(m.group(2))
    approve = action == "approve"
    linked = await run_in_threadpool(_linked_admin, db, from_id)
    who = _display_name(sender)
    try:
        amount = await run_in_threadpool(topups.resolve, db, topup_id, approve=approve,
                                         admin_id=linked["id"] if linked else None, source="Telegram")
    except ApiError as e:
        if e.code == "already_resolved":
            await answer("Bu so'rov allaqachon ko'rib chiqilgan.", alert=True)
            # It may have been resolved in the panel: make the message show the real state.
            await _ensure_tracked(db, topup_id, msg_chat, msg.get("message_id"))
            await sync_message(app, topup_id)
        else:
            await answer(e.message, alert=True)
        return
    await run_in_threadpool(audit.record, db,
                            admin={"id": linked["id"] if linked else None, "email": f"telegram:{from_id}"},
                            action="topup.approve" if approve else "topup.reject", target=f"topup:{topup_id}",
                            ok=True, detail=f"{who}; {fmt_som(amount)}")
    await _ensure_tracked(db, topup_id, msg_chat, msg.get("message_id"))
    await sync_message(app, topup_id, who)
    await answer("✅ Tasdiqlandi, hisobga yozildi." if approve else "❌ Rad etildi.")


async def _ensure_tracked(db: Database, topup_id: int, chat: str, message_id) -> None:
    """If the message id was not stored (e.g. a disconnect before the sendMessage reply), take it from the callback."""
    if not isinstance(message_id, int):
        return

    def run():
        with db.tx() as c:
            c.execute("UPDATE topup_requests SET tg_chat_id=?, tg_message_id=? WHERE id=? AND tg_message_id IS NULL",
                      (chat, message_id, topup_id))
    await run_in_threadpool(run)


@router.post(WEBHOOK_PATH, include_in_schema=False)
async def webhook(request: Request):
    db: Database = request.app.state.db
    s = await run_in_threadpool(db.get_settings)
    secret = s.get("topup_tg_webhook_secret") or ""
    got = request.headers.get(SECRET_HEADER) or ""
    if not secret or not hmac.compare_digest(got.encode(), secret.encode()):
        log.warning("telegram webhook: secret header mismatch (%s)", request.client.host if request.client else "-")
        return JSONResponse({"ok": False}, status_code=403)
    try:
        update = await request.json()
    except ValueError:
        return {"ok": True}
    cq = update.get("callback_query") if isinstance(update, dict) else None
    if isinstance(cq, dict):
        await handle_callback(request.app, s, cq)
    # Always 200: otherwise Telegram keeps redelivering this update.
    return {"ok": True}


# --- admin: webhook connect / status / remove -----------------------------------------------------------------------
def _need_token(s: dict) -> str:
    token = bot_token(s)
    if not token:
        raise ApiError(400, "telegram_not_configured",
                       "Kirish boti tokeni (telegram_login_token) kiritilmagan — to'ldirish xabarlari shu bot orqali.")
    return token


@admin_router.get("/topup-webhook")
async def webhook_status(request: Request):
    db: Database = request.app.state.db
    s = await run_in_threadpool(db.get_settings)
    out = {"bot": None, "chat_id": chat_id(s), "admin_ids": sorted(admin_ids(s)), "enabled": enabled(s),
           "secret_set": bool(s.get("topup_tg_webhook_secret")), "url": "", "expected_url": "",
           "pending": None, "last_error": ""}
    token = bot_token(s)
    if not token:
        return out
    base = ((s.get("site_base_url") or "").strip() or branding.SITE_URL).rstrip("/") or str(request.base_url).rstrip("/")
    out["expected_url"] = base + WEBHOOK_PATH
    me = await call(request.app, token, "getMe", {})
    info = await call(request.app, token, "getWebhookInfo", {})
    if me:
        out["bot"] = me["result"].get("username")
    if info:
        r = info["result"]
        out.update(url=r.get("url", ""), pending=r.get("pending_update_count"),
                   last_error=r.get("last_error_message", ""))
    return out


@admin_router.post("/topup-webhook")
async def webhook_connect(request: Request, admin=Depends(require_admin)):
    """Sets the webhook with a fresh secret token. Only callbacks are requested."""
    db: Database = request.app.state.db
    s = await run_in_threadpool(db.get_settings)
    token = _need_token(s)
    base = (s.get("site_base_url") or "").rstrip("/")
    if not base.startswith("https://"):
        raise ApiError(400, "site_base_url_required", "Telegram faqat https manzilga yuboradi: \"Saytning tashqi manzili\" ni kiriting.")
    secret = secrets.token_urlsafe(32)
    # Save first, so a callback arriving right after a successful setWebhook also passes the check.
    await run_in_threadpool(db.set_setting, "topup_tg_webhook_secret", secret)
    data = await call(request.app, token, "setWebhook",
                      {"url": base + WEBHOOK_PATH, "secret_token": secret, "allowed_updates": ["callback_query"],
                       "drop_pending_updates": True})
    await run_in_threadpool(audit.record, db, admin=admin, action="telegram.webhook_set", target="telegram:topups",
                            ok=data is not None, detail=base + WEBHOOK_PATH)
    if data is None:
        raise ApiError(502, "telegram_error", "Telegram webhook'ni qabul qilmadi. Jurnalni tekshiring.")
    return await webhook_status(request)


@admin_router.post("/topup-webhook/remove")
async def webhook_remove(request: Request, admin=Depends(require_admin)):
    db: Database = request.app.state.db
    s = await run_in_threadpool(db.get_settings)
    token = _need_token(s)
    data = await call(request.app, token, "deleteWebhook", {})
    await run_in_threadpool(db.set_setting, "topup_tg_webhook_secret", "")
    await run_in_threadpool(audit.record, db, admin=admin, action="telegram.webhook_remove", target="telegram:topups",
                            ok=data is not None)
    return await webhook_status(request)
