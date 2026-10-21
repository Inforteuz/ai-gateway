"""Top-up requests in Telegram: message, buttons, permissions, idempotency, races with the panel."""
import threading

import httpx
import pytest
from fastapi import FastAPI, Request

from app import topup_telegram, topups
from app.common import ApiError, create_user

from .conftest import CSRF, SOM, add_user
from .test_referral import code_of, rewards

TOKEN = "777:FAKE-TOPUP-BOT"     # fake; real tokens are never written into tests
OWNER = 900001                   # the owner's Telegram user_id (in a private chat the chat_id is the same)
STRANGER = 900002
SECRET = "test-webhook-secret"
HOOK = topup_telegram.WEBHOOK_PATH


@pytest.fixture
def tg(env):
    """Fake Bot API: every call is recorded in `calls`."""
    state = {"calls": [], "next_id": 500}
    fake = FastAPI()

    @fake.post("/bot{token}/{method}")
    async def method(token: str, method: str, request: Request):
        body = await request.json()
        state["calls"].append((token, method, body))
        if method == "sendMessage":
            state["next_id"] += 1
            return {"ok": True, "result": {"message_id": state["next_id"], "chat": {"id": int(body["chat_id"])}}}
        if method == "getMe":
            return {"ok": True, "result": {"username": "sf_testbot"}}
        if method == "getWebhookInfo":
            return {"ok": True, "result": {"url": state.get("url", ""), "pending_update_count": 0}}
        if method == "setWebhook":
            state["url"] = body["url"]
        return {"ok": True, "result": True}

    env.app.state.notify_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=fake),
                                                  base_url=topup_telegram.TELEGRAM_API)
    for k, v in {"telegram_login_token": TOKEN, "alert_telegram_chat_id": str(OWNER), "topup_tg_enabled": "1",
                 "topup_tg_webhook_secret": SECRET}.items():
        env.db.set_setting(k, v)
    state["of"] = lambda m: [b for _, name, b in state["calls"] if name == m]
    return state


def login(env, email):
    env.client.cookies.clear()
    r = env.client.post("/portal/auth/login", json={"email": email, "password": "parol-12345"}, headers=CSRF)
    assert r.status_code == 200, r.text


def request_topup(env, email="u@test.uz", som=50_000, note="Payme 14:20"):
    login(env, email)
    r = env.client.post("/portal/topups", json={"amount": som * SOM, "note": note}, headers=CSRF)
    assert r.status_code == 200, r.text
    env.client.cookies.clear()
    return r.json()["item"]["id"]


def press(env, tid, action="approve", *, user=OWNER, chat=OWNER, message_id=None, secret=SECRET, data=None):
    msg_id = message_id or env.db.scalar("SELECT tg_message_id FROM topup_requests WHERE id=?", (tid,)) or 1
    update = {"update_id": 1, "callback_query": {
        "id": "cq1", "from": {"id": user, "first_name": "Ega", "username": "owner"},
        "message": {"message_id": msg_id, "chat": {"id": chat, "type": "private"}},
        "data": data or f"topup:{action}:{tid}"}}
    headers = {"X-Telegram-Bot-Api-Secret-Token": secret} if secret is not None else {}
    return env.client.post(HOOK, json=update, headers=headers)


def bal(env, uid):
    return env.db.scalar("SELECT balance FROM wallets WHERE user_id=?", (uid,), default=0) or 0


def topup_ledger(env, uid):
    return env.db.all("SELECT * FROM ledger WHERE user_id=? AND kind='topup'", (uid,))


def status(env, tid):
    return env.db.scalar("SELECT status FROM topup_requests WHERE id=?", (tid,))


# --- message ---------------------------------------------------------------------------------------------------------
def test_new_request_sends_message_with_buttons_without_amount_in_callback(env, tg):
    add_user(env, "u@test.uz")
    tid = request_topup(env)
    (msg,) = tg["of"]("sendMessage")
    assert msg["chat_id"] == str(OWNER)
    assert "50 000 so'm" in msg["text"] and "Payme 14:20" in msg["text"] and "u@test.uz" in msg["text"]
    buttons = msg["reply_markup"]["inline_keyboard"][0]
    assert [b["callback_data"] for b in buttons] == [f"topup:approve:{tid}", f"topup:reject:{tid}"]
    assert all("50000" not in b["callback_data"] for b in buttons)
    assert env.db.scalar("SELECT tg_message_id FROM topup_requests WHERE id=?", (tid,)) == 501
    assert all(token == TOKEN for token, _, _ in tg["calls"])


def test_disabled_sends_nothing(env, tg):
    env.db.set_setting("topup_tg_enabled", "0")
    add_user(env, "u@test.uz")
    request_topup(env)
    assert tg["calls"] == []


# --- webhook security ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("secret", [None, "", "noto'g'ri"])
def test_webhook_requires_secret_header(env, tg, secret):
    uid = add_user(env, "u@test.uz")
    tid = request_topup(env)
    assert press(env, tid, secret=secret).status_code == 403
    assert status(env, tid) == "pending" and bal(env, uid) == 0


def test_webhook_closed_when_no_secret_configured(env, tg):
    env.db.set_setting("topup_tg_webhook_secret", "")
    uid = add_user(env, "u@test.uz")
    tid = request_topup(env)
    assert press(env, tid, secret="").status_code == 403
    assert status(env, tid) == "pending" and bal(env, uid) == 0


def test_unauthorized_user_is_rejected(env, tg):
    uid = add_user(env, "u@test.uz")
    tid = request_topup(env)
    assert press(env, tid, user=STRANGER, chat=STRANGER).status_code == 200
    assert status(env, tid) == "pending" and bal(env, uid) == 0
    (ans,) = tg["of"]("answerCallbackQuery")
    assert ans["show_alert"] and "ruxsat" in ans["text"]
    assert tg["of"]("editMessageText") == []


def test_owner_pressing_in_other_chat_is_rejected(env, tg):
    uid = add_user(env, "u@test.uz")
    tid = request_topup(env)
    press(env, tid, chat=-100123)
    assert status(env, tid) == "pending" and bal(env, uid) == 0


def test_explicit_admin_list_replaces_chat_owner(env, tg):
    env.db.set_setting("topup_tg_admin_ids", str(STRANGER))
    uid = add_user(env, "u@test.uz")
    tid = request_topup(env)
    press(env, tid, user=OWNER)
    assert status(env, tid) == "pending"
    press(env, tid, user=STRANGER)
    assert status(env, tid) == "approved" and bal(env, uid) == 50_000 * SOM


def test_callback_with_extra_fields_is_ignored(env, tg):
    uid = add_user(env, "u@test.uz")
    tid = request_topup(env)
    press(env, tid, data=f"topup:approve:{tid}:999999999")
    assert status(env, tid) == "pending" and bal(env, uid) == 0


# --- approve / reject ------------------------------------------------------------------------------------------------
def test_approve_credits_once_and_updates_message(env, tg):
    uid = add_user(env, "u@test.uz")
    tid = request_topup(env)
    assert press(env, tid).status_code == 200
    assert press(env, tid).status_code == 200          # second press
    assert status(env, tid) == "approved"
    assert bal(env, uid) == 50_000 * SOM and len(topup_ledger(env, uid)) == 1
    assert "Telegram" in topup_ledger(env, uid)[0]["note"]
    edit = tg["of"]("editMessageText")[0]
    assert edit["message_id"] == 501 and edit["reply_markup"] == {"inline_keyboard": []}
    assert "✅ Tasdiqlandi" in edit["text"] and "@owner" in edit["text"]
    answers = [a["text"] for a in tg["of"]("answerCallbackQuery")]
    assert "allaqachon" in answers[1]
    audit = env.db.all("SELECT * FROM admin_audit WHERE target=?", (f"topup:{tid}",))
    assert [(a["action"], a["admin_email"]) for a in audit] == [("topup.approve", f"telegram:{OWNER}")]


def test_reject_credits_nothing(env, tg):
    uid = add_user(env, "u@test.uz")
    tid = request_topup(env)
    press(env, tid, "reject")
    press(env, tid, "approve")                         # cannot approve after a reject
    assert status(env, tid) == "rejected" and bal(env, uid) == 0 and topup_ledger(env, uid) == []
    assert "❌ Rad etildi" in tg["of"]("editMessageText")[0]["text"]


def test_linked_admin_account_is_recorded_as_resolver(env, tg):
    admin = add_user(env, "admin@test.uz", role="admin")
    with env.db.tx() as c:
        c.execute("UPDATE users SET telegram_id=? WHERE id=?", (str(OWNER), admin))
    uid = add_user(env, "u@test.uz")
    tid = request_topup(env)
    press(env, tid)
    assert env.db.scalar("SELECT resolved_by FROM topup_requests WHERE id=?", (tid,)) == admin
    assert topup_ledger(env, uid)[0]["admin_id"] == admin


def test_telegram_approval_pays_referral_bonus_like_panel(env, tg):
    a = add_user(env, "a@test.uz")
    create_user(env.db, email="u@test.uz", name="u", password="parol-12345", referral_code=code_of(env, a))
    tid = request_topup(env, som=200_000)
    press(env, tid)
    press(env, tid)
    assert len(rewards(env, a)) == 1 and bal(env, a) == 10_000 * SOM


# --- race with the admin panel ---------------------------------------------------------------------------------------
def test_panel_then_telegram_pays_once_and_message_follows_panel(env, tg):
    add_user(env, "admin@test.uz", role="admin")
    uid = add_user(env, "u@test.uz")
    tid = request_topup(env)
    login(env, "admin@test.uz")
    assert env.client.post(f"/portal/admin/topups/{tid}/resolve", json={"action": "approve"}, headers=CSRF).status_code == 200
    edit = tg["of"]("editMessageText")[0]                 # resolved in the panel — the message was updated too
    assert "✅ Tasdiqlandi" in edit["text"] and "admin" in edit["text"]
    press(env, tid)
    press(env, tid, "reject")
    assert status(env, tid) == "approved" and bal(env, uid) == 50_000 * SOM and len(topup_ledger(env, uid)) == 1


def test_telegram_then_panel_gets_409(env, tg):
    add_user(env, "admin@test.uz", role="admin")
    uid = add_user(env, "u@test.uz")
    tid = request_topup(env)
    press(env, tid)
    login(env, "admin@test.uz")
    assert env.client.post(f"/portal/admin/topups/{tid}/resolve", json={"action": "approve"}, headers=CSRF).status_code == 409
    assert len(topup_ledger(env, uid)) == 1


def test_concurrent_resolutions_pay_once(env):
    uid = add_user(env, "u@test.uz")
    with env.db.tx() as c:
        tid = c.execute("INSERT INTO topup_requests(user_id, amount, note, created_at) VALUES (?,?,?,1)",
                        (uid, 30_000 * SOM, "")).lastrowid
    results, start = [], threading.Barrier(6)

    def worker(approve):
        start.wait()
        try:
            topups.resolve(env.db, tid, approve=approve, admin_id=None)
            results.append("ok")
        except ApiError as e:
            results.append(e.code)

    threads = [threading.Thread(target=worker, args=(i % 2 == 0,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("ok") == 1 and results.count("already_resolved") == 5
    assert len(topup_ledger(env, uid)) == (1 if status(env, tid) == "approved" else 0)


# --- admin settings ---------------------------------------------------------------------------------------------------
def test_settings_never_return_secret_or_token(env, tg):
    add_user(env, "admin@test.uz", role="admin")
    login(env, "admin@test.uz")
    body = env.client.get("/portal/admin/settings").text
    assert SECRET not in body and TOKEN not in body
    r = env.client.put("/portal/admin/settings", json={"topup_tg_admin_ids": " 1, 2 ,", "topup_tg_chat_id": " -100 "},
                       headers=CSRF)
    assert r.status_code == 200 and r.json()["topup_tg_admin_ids"] == "1,2" and r.json()["topup_tg_chat_id"] == "-100"
    assert r.json()["topup_tg_webhook_set"] is True
    assert env.client.put("/portal/admin/settings", json={"topup_tg_admin_ids": "1; DROP"}, headers=CSRF).status_code == 422
    assert env.client.put("/portal/admin/settings", json={"topup_tg_webhook_secret": "x"}, headers=CSRF).status_code == 200
    assert env.db.get_settings()["topup_tg_webhook_secret"] == SECRET   # cannot be changed this way


def test_connect_webhook_sets_fresh_secret_and_only_callbacks(env, tg):
    add_user(env, "admin@test.uz", role="admin")
    login(env, "admin@test.uz")
    env.db.set_setting("site_base_url", "https://gateway.example/")
    r = env.client.post("/portal/admin/telegram/topup-webhook", headers=CSRF)
    assert r.status_code == 200, r.text
    (hook,) = tg["of"]("setWebhook")
    assert hook["url"] == "https://gateway.example" + HOOK and hook["allowed_updates"] == ["callback_query"]
    new_secret = env.db.get_settings()["topup_tg_webhook_secret"]
    assert hook["secret_token"] == new_secret and new_secret != SECRET and len(new_secret) >= 32
    assert new_secret not in r.text and r.json()["url"] == hook["url"] and r.json()["bot"] == "sf_testbot"
    uid = add_user(env, "u@test.uz")
    tid = request_topup(env)
    assert press(env, tid).status_code == 403                       # the old secret no longer works
    assert press(env, tid, secret=new_secret).status_code == 200
    assert bal(env, uid) == 50_000 * SOM


def test_connect_webhook_requires_https_site(env, tg):
    add_user(env, "admin@test.uz", role="admin")
    login(env, "admin@test.uz")
    env.db.set_setting("site_base_url", "http://localhost:8000")
    assert env.client.post("/portal/admin/telegram/topup-webhook", headers=CSRF).status_code == 400
    assert tg["of"]("setWebhook") == []
