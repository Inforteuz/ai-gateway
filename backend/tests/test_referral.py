"""Referral program: linking (three paths), 5% reward, idempotency, non-deposit credits, settings,
admin tools and the v1 -> v2 migration."""
from pathlib import Path

import psycopg
import pytest
from psycopg import sql as pgsql

from app import billing, referral
from app.common import create_user
from app.db import SCHEMA_VERSION, apply_schema, schema_version

from .conftest import CSRF, SOM, TEST_APP_ROLE, add_user
from .test_oauth_login import configure_google, enable_telegram, google_login, logged_in, tg_login, tg_params


def login(env, email, password="parol-12345"):
    r = env.client.post("/portal/auth/login", json={"email": email, "password": password}, headers=CSRF)
    assert r.status_code == 200, r.text


def logout(env):
    env.client.post("/portal/auth/logout", headers=CSRF)
    env.client.cookies.clear()


def open_signup(env, bonus_som=0):
    env.db.set_setting("registration_open", "1")
    env.db.set_setting("signup_bonus_micro", str(bonus_som * SOM))
    env.db.set_setting("max_accounts_per_ip", "0")   # the tests open many accounts from one IP


def register(env, email, *, ref=None, cookie_ref=None, device=None):
    if cookie_ref:
        env.client.cookies.set(referral.REF_COOKIE, cookie_ref)
    if device:
        env.client.cookies.set(referral.DEVICE_COOKIE, device)
    body = {"email": email, "password": "parol-12345", "name": "x"}
    if ref is not None:
        body["ref"] = ref
    r = env.client.post("/portal/auth/register", json=body, headers=CSRF)
    assert r.status_code == 200, r.text
    return r.json()["user"]["id"]


def code_of(env, uid):
    return referral.ensure_code(env.db, uid)


def link_of(env, uid):
    return env.db.one("SELECT referrer_id, method, flags FROM referrals WHERE referee_id=?", (uid,))


def deposit(env, uid, som=None, *, micro=None, kind="topup"):
    return billing.credit(env.db, user_id=uid, amount=micro if micro is not None else som * SOM, kind=kind, note="t")


def bal(env, uid):
    return env.db.scalar("SELECT balance FROM wallets WHERE user_id=?", (uid,))


def rewards(env, referrer_id):
    return env.db.all("SELECT * FROM referral_rewards WHERE referrer_id=? ORDER BY id", (referrer_id,))


def linked_pair(env):
    """A (referrer) and B (referred by A) — via create_user, without HTTP."""
    a = add_user(env, "a@test.uz")
    b = create_user(env.db, email="b@test.uz", name="b", password="parol-12345", referral_code=code_of(env, a))
    return a, b


# --- code ---------------------------------------------------------------------------------------------------------
def test_every_user_gets_unique_code_and_old_accounts_get_one_lazily(env):
    a = add_user(env, "a@test.uz")
    b = add_user(env, "b@test.uz")
    ca, cb = (env.db.scalar("SELECT ref_code FROM users WHERE id=?", (u,)) for u in (a, b))
    assert ca and cb and ca != cb and len(ca) == referral.CODE_LEN
    assert set(ca) <= set(referral.CODE_ALPHABET)
    # An account created before referrals (no code): gets one on first request and keeps it
    with env.db.tx() as c:
        c.execute("UPDATE users SET ref_code=NULL WHERE id=?", (a,))
    first = code_of(env, a)
    assert first and code_of(env, a) == first


# --- linking: three paths ----------------------------------------------------------------------------------------
def test_password_signup_links_via_body_and_via_cookie(env):
    open_signup(env)
    a = add_user(env, "a@test.uz")
    code = code_of(env, a)
    b = register(env, "b@test.uz", ref=code.lower())          # case does not matter
    assert dict(link_of(env, b)) == {"referrer_id": a, "method": "password", "flags": ""}
    logout(env)
    c = register(env, "c@test.uz", cookie_ref=code)           # cookie only (restored from localStorage)
    assert link_of(env, c)["referrer_id"] == a
    # B (not the referrer) and C share an IP: when B refers someone, it matches the referrer's session IP
    logout(env)
    d = register(env, "d@test.uz", ref=code_of(env, b))
    assert dict(link_of(env, d)) == {"referrer_id": b, "method": "password", "flags": "same_ip"}
    # The used cookie is cleared
    assert referral.REF_COOKIE not in env.client.cookies


def test_bad_or_unknown_code_does_not_block_signup(env):
    open_signup(env)
    add_user(env, "a@test.uz")
    for i, bad in enumerate(["NOPE2345", "<script>", "x" * 30, ""]):
        logout(env)
        uid = register(env, f"u{i}@test.uz", ref=bad[:32])
        assert link_of(env, uid) is None


def test_google_signup_keeps_ref_cookie_through_redirect(env):
    open_signup(env)
    a = add_user(env, "a@test.uz")
    configure_google(env)
    env.client.cookies.set(referral.REF_COOKIE, code_of(env, a))
    env.client.cookies.set(referral.DEVICE_COOKIE, "dev-google-12345")
    r = google_login(env)
    assert r.status_code == 303 and r.headers["location"] == "/app"
    uid = logged_in(env)["id"]
    assert link_of(env, uid)["referrer_id"] == a and link_of(env, uid)["method"] == "google"
    assert env.db.scalar("SELECT signup_device FROM users WHERE id=?", (uid,)) == "dev-google-12345"


def test_telegram_signup_links_via_cookie(env):
    open_signup(env)
    enable_telegram(env)
    a = add_user(env, "a@test.uz")
    env.client.cookies.set(referral.REF_COOKIE, code_of(env, a))
    r = tg_login(env, tg_params("777001"))
    assert r.status_code == 303 and r.headers["location"] == "/app"
    uid = logged_in(env)["id"]
    assert link_of(env, uid)["referrer_id"] == a and link_of(env, uid)["method"] == "telegram"


def test_existing_account_is_never_relinked(env):
    """An existing user logging in with a ref cookie (password/Google) is not linked; a linked user never moves to another referrer."""
    open_signup(env)
    a, b = linked_pair(env)
    x = add_user(env, "x@test.uz")
    env.client.cookies.set(referral.REF_COOKIE, code_of(env, x))
    login(env, "b@test.uz")
    # Linking Google to an EXISTING account (same email) is not a new referral link either
    configure_google(env, claims={"email": "a@test.uz", "sub": "g-a"})
    env.client.cookies.set(referral.REF_COOKIE, code_of(env, x))
    google_login(env)
    assert link_of(env, a) is None and link_of(env, b)["referrer_id"] == a
    # Enforced in the database too: the app role cannot change/delete a link or add a second one
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with env.db.tx() as c:
            c.execute("UPDATE referrals SET referrer_id=? WHERE referee_id=?", (x, b))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with env.db.tx() as c:
            c.execute("DELETE FROM referrals WHERE referee_id=?", (b,))
    with pytest.raises(psycopg.errors.UniqueViolation):
        with env.db.tx() as c:
            c.execute("INSERT INTO referrals(referee_id, referrer_id, method, created_at) VALUES (?,?,'password',1)", (b, x))


def test_self_referral_is_impossible(env):
    a = add_user(env, "a@test.uz")
    with env.db.tx() as c:
        assert referral.on_signup_tx(c, user_id=a, code=code_of(env, a), method="password", ip=None, device=None) is None
    assert link_of(env, a) is None
    with pytest.raises(psycopg.errors.CheckViolation):
        with env.db.tx() as c:
            c.execute("INSERT INTO referrals(referee_id, referrer_id, method, created_at) VALUES (?,?,'password',1)", (a, a))


def test_disabled_program_links_nobody_and_pays_nothing(env):
    open_signup(env)
    a, b = linked_pair(env)
    env.db.set_setting("referral_enabled", "0")
    c = register(env, "c@test.uz", ref=code_of(env, a))
    assert link_of(env, c) is None
    deposit(env, b, 100_000)
    assert bal(env, a) == 0 and rewards(env, a) == []
    # Once re-enabled, the next deposit pays out (not the past ones)
    env.db.set_setting("referral_enabled", "1")
    deposit(env, b, 100_000)
    assert bal(env, a) == 5_000 * SOM


# --- reward calculation ------------------------------------------------------------------------------------------
def test_five_percent_of_every_deposit(env):
    a, b = linked_pair(env)
    deposit(env, b, 100_000)
    deposit(env, b, 40_000)
    assert bal(env, a) == 7_000 * SOM            # 5 000 + 2 000
    assert bal(env, b) == 140_000 * SOM          # nothing is taken from the referred user
    led = env.db.all("SELECT kind, amount, balance_after FROM ledger WHERE user_id=? ORDER BY id", (a,))
    assert [(r["kind"], r["amount"], r["balance_after"]) for r in led] == [
        ("referral", 5_000 * SOM, 5_000 * SOM), ("referral", 2_000 * SOM, 7_000 * SOM)]
    rows = rewards(env, a)
    assert [(r["status"], r["percent_bp"], r["deposit_amount"]) for r in rows] == [
        ("paid", 500, 100_000 * SOM), ("paid", 500, 40_000 * SOM)]


@pytest.mark.parametrize("amount, percent, expected", [
    (333_333_333, "5", 16_666_666),        # 16 666 666.65 -> rounded down
    (19, "5", 0),                          # 0.95 micro -> 0 (no row either)
    (1_000_001 * SOM, "2.5", 25_000_025_000_000 // 1000),
    (10_000 * SOM, "0.01", 1 * SOM),
])
def test_rounding_is_floor_to_micro(env, amount, percent, expected):
    a, b = linked_pair(env)
    env.db.set_setting("referral_percent", percent)
    deposit(env, b, micro=amount)
    assert bal(env, a) == expected
    assert referral.reward_for(amount, referral.percent_bp(percent)) == expected


def test_non_deposits_earn_nothing(env):
    """The signup bonus, admin gifts/adjustments, refunds and rewards themselves are not deposits."""
    a = add_user(env, "a@test.uz")
    b = create_user(env.db, email="b@test.uz", name="b", password="parol-12345",
                    referral_code=code_of(env, a), bonus_micro=10_000 * SOM)          # signup bonus
    c = create_user(env.db, email="c@test.uz", name="c", password="parol-12345", referral_code=code_of(env, b))
    for kind in ("bonus", "adjustment", "refund"):
        deposit(env, b, 50_000, kind=kind)
    deposit(env, b, micro=-1 * SOM, kind="adjustment")
    assert bal(env, a) == 0 and rewards(env, a) == []
    # C deposits -> B gets a reward; that reward does not chain up to B's referrer A
    deposit(env, c, 100_000)
    assert bal(env, b) == (10_000 + 150_000 - 1 + 5_000) * SOM and bal(env, a) == 0
    # A reward does not count as a deposit, also for B's free daily limit
    assert not billing.has_deposit(env.db, b)


def test_admin_credit_kinds_via_api(env):
    """Admin 'To'ldirish' (topup) is a deposit (per has_deposit); 'Sovg'a' (gift) / 'Tuzatish' (adjustment) are not."""
    add_user(env, "admin@test.uz", role="admin")
    a, b = linked_pair(env)
    login(env, "admin@test.uz")
    for kind in ("bonus", "adjustment"):
        assert env.client.post(f"/portal/admin/users/{b}/credit", json={"amount": 20_000 * SOM, "kind": kind},
                               headers=CSRF).status_code == 200
    assert bal(env, a) == 0
    assert env.client.post(f"/portal/admin/users/{b}/credit", json={"amount": 20_000 * SOM, "kind": "topup"},
                           headers=CSRF).status_code == 200
    assert bal(env, a) == 1_000 * SOM


# --- idempotency and atomicity -----------------------------------------------------------------------------------
def test_topup_request_approval_pays_once(env):
    add_user(env, "admin@test.uz", role="admin")
    a, b = linked_pair(env)
    login(env, "b@test.uz")
    env.client.post("/portal/topups", json={"amount": 200_000 * SOM, "note": ""}, headers=CSRF)
    tid = env.db.scalar("SELECT id FROM topup_requests WHERE user_id=?", (b,))
    logout(env)
    login(env, "admin@test.uz")
    url = f"/portal/admin/topups/{tid}/resolve"
    assert env.client.post(url, json={"action": "approve"}, headers=CSRF).status_code == 200
    assert env.client.post(url, json={"action": "approve"}, headers=CSRF).status_code == 409   # pressed again
    assert bal(env, a) == 10_000 * SOM and len(rewards(env, a)) == 1


def test_rejected_topup_pays_nothing(env):
    add_user(env, "admin@test.uz", role="admin")
    a, b = linked_pair(env)
    login(env, "b@test.uz")
    env.client.post("/portal/topups", json={"amount": 200_000 * SOM, "note": ""}, headers=CSRF)
    tid = env.db.scalar("SELECT id FROM topup_requests WHERE user_id=?", (b,))
    logout(env)
    login(env, "admin@test.uz")
    env.client.post(f"/portal/admin/topups/{tid}/resolve", json={"action": "reject"}, headers=CSRF)
    assert bal(env, a) == 0 and rewards(env, a) == []


def test_repeated_reward_call_for_same_deposit_is_noop(env):
    a, b = linked_pair(env)
    deposit(env, b, 100_000)
    dep_id = env.db.scalar("SELECT id FROM ledger WHERE user_id=? AND kind='topup'", (b,))
    for _ in range(3):   # duplicate webhook / retry
        with env.db.tx() as c:
            assert referral.on_deposit_tx(c, user_id=b, deposit_ledger_id=dep_id, amount=100_000 * SOM) == 0
    assert bal(env, a) == 5_000 * SOM and len(rewards(env, a)) == 1
    with pytest.raises(psycopg.errors.UniqueViolation):   # guaranteed at the database level
        with env.db.tx() as c:
            c.execute("""INSERT INTO referral_rewards(referrer_id, referee_id, deposit_ledger_id, deposit_amount,
                             percent_bp, amount, status, created_at) VALUES (?,?,?,1,500,1,'paid',1)""", (a, b, dep_id))


def test_reward_and_deposit_are_atomic(env, monkeypatch):
    a, b = linked_pair(env)

    def boom(*_a, **_k):
        raise RuntimeError("reward not written")
    monkeypatch.setattr(referral, "on_deposit_tx", boom)
    with pytest.raises(RuntimeError):
        deposit(env, b, 100_000)
    # The deposit was not written either — no half-done state
    assert bal(env, b) == 0 and env.db.scalar("SELECT COUNT(*) FROM ledger WHERE user_id=?", (b,)) == 0


# --- admin: pausing, settings, flags -----------------------------------------------------------------------------
def test_paused_or_suspended_referrer_is_skipped_not_paid(env):
    a, b = linked_pair(env)
    with env.db.tx() as c:
        c.execute("UPDATE users SET referral_paused=1 WHERE id=?", (a,))
    deposit(env, b, 100_000)
    with env.db.tx() as c:
        c.execute("UPDATE users SET referral_paused=0, status='suspended' WHERE id=?", (a,))
    deposit(env, b, 100_000)
    assert bal(env, a) == 0
    assert [(r["status"], r["reason"], r["reward_ledger_id"]) for r in rewards(env, a)] == [
        ("skipped", "paused", None), ("skipped", "suspended", None)]
    with env.db.tx() as c:
        c.execute("UPDATE users SET status='active' WHERE id=?", (a,))
    deposit(env, b, 100_000)
    assert bal(env, a) == 5_000 * SOM          # past ones are not paid retroactively


def test_admin_endpoints(env):
    add_user(env, "admin@test.uz", role="admin")
    a, b = linked_pair(env)
    deposit(env, b, 100_000)
    login(env, "a@test.uz")
    assert env.client.get("/portal/admin/referrals").status_code == 403
    logout(env)
    login(env, "admin@test.uz")
    r = env.client.get("/portal/admin/referrals").json()
    assert r["enabled"] is True and r["percent"] == "5"
    assert r["totals"] == {"links": 1, "referrers": 1, "paid": 5_000 * SOM, "paid_count": 1, "skipped_count": 0}
    item = r["items"][0]
    assert (item["referrer_email"], item["referee_email"], item["deposits"], item["rewarded"]) == (
        "a@test.uz", "b@test.uz", 100_000 * SOM, 5_000 * SOM)
    rw = env.client.get("/portal/admin/referrals/rewards").json()
    assert rw["total"] == 1 and rw["items"][0]["amount"] == 5_000 * SOM and rw["items"][0]["status"] == "paid"
    # Pausing per user
    assert env.client.post(f"/portal/admin/referrals/users/{a}/pause", json={"paused": True}, headers=CSRF).json()["paused"]
    assert env.client.get("/portal/admin/referrals").json()["items"][0]["referrer_paused"] is True
    assert env.client.post("/portal/admin/referrals/users/99999/pause", json={"paused": True}, headers=CSRF).status_code == 404
    # Settings
    put = lambda body: env.client.put("/portal/admin/referrals/settings", json=body, headers=CSRF)  # noqa: E731
    assert put({"percent": "7,5", "enabled": False}).json() == {"enabled": False, "percent": "7.5"}
    assert env.db.get_settings()["referral_percent"] == "7.5"
    for bad in ("0", "51", "abc", "5.555", "-1"):
        assert put({"percent": bad}).status_code in (400, 422), bad
    assert env.db.get_settings()["referral_percent"] == "7.5"


def test_fraud_flags_for_same_ip_and_device(env):
    add_user(env, "admin@test.uz", role="admin")
    open_signup(env)
    a = create_user(env.db, email="a@test.uz", name="a", password="parol-12345", signup_ip="10.0.0.1", device="dev-aaaa-1111")
    code = code_of(env, a)
    same = create_user(env.db, email="b@test.uz", name="b", password="parol-12345", signup_ip="10.0.0.1",
                       device="dev-aaaa-1111", referral_code=code)
    other = create_user(env.db, email="c@test.uz", name="c", password="parol-12345", signup_ip="10.0.0.9",
                        device="dev-cccc-3333", referral_code=code)
    assert link_of(env, same)["flags"] == "same_ip,same_device" and link_of(env, other)["flags"] == ""
    login(env, "admin@test.uz")
    items = {i["referee_email"]: i for i in env.client.get("/portal/admin/referrals").json()["items"]}
    assert items["b@test.uz"]["flags"] == ["same_ip", "same_device", "multi_ip", "multi_device"]
    assert items["b@test.uz"]["ip_accounts"] == 2 and items["c@test.uz"]["flags"] == []
    flagged = env.client.get("/portal/admin/referrals?flagged=true").json()["items"]
    assert [i["referee_email"] for i in flagged] == ["b@test.uz"]


# --- portal ------------------------------------------------------------------------------------------------------
def test_portal_page_data(env):
    env.db.set_setting("site_base_url", "https://gateway.example")
    a, b = linked_pair(env)
    create_user(env.db, email="c@test.uz", name="c", password="parol-12345", referral_code=code_of(env, a))
    deposit(env, b, 100_000)
    login(env, "a@test.uz")
    r = env.client.get("/portal/referral").json()
    code = code_of(env, a)
    assert r["code"] == code and r["link"] == f"https://gateway.example/login?ref={code}"
    assert (r["enabled"], r["percent"], r["invited"], r["depositors"], r["earned"]) == (True, "5", 2, 1, 5_000 * SOM)
    assert r["rewards"] == [{"created_at": r["rewards"][0]["created_at"], "amount": 5_000 * SOM,
                             "deposit_amount": 100_000 * SOM, "percent": "5", "referee": "b***@test.uz"}]
    # A referred user does not see the referrer on their page, only their own code
    logout(env)
    login(env, "b@test.uz")
    assert env.client.get("/portal/referral").json()["invited"] == 0
    assert env.client.get("/portal/ledger").status_code == 200


def test_mask_email():
    assert referral.mask_email("alisher@example.com") == "al***@example.com"
    assert referral.mask_email("tg123@telegram.invalid") == "Telegram foydalanuvchisi"


# --- migration v1 -> v2 --------------------------------------------------------------------------------------------
def test_migration_from_v1_keeps_data_and_enables_referrals(pg_dsn):
    """`dbadmin migrate` (apply_schema) applies safely to an existing v1 database."""
    v1 = (Path(__file__).parent / "data" / "schema_v1.sql").read_text(encoding="utf-8")
    name = "mig_" + __import__("secrets").token_hex(5)
    with psycopg.connect(pg_dsn, autocommit=True) as c:
        c.execute(pgsql.SQL("CREATE SCHEMA {}").format(pgsql.Identifier(name)))
        try:
            c.execute(pgsql.SQL("SET search_path = {}, public").format(pgsql.Identifier(name)))
            c.execute(v1)
            c.execute("INSERT INTO schema_meta(id, version) VALUES (1, 1)")
            uid = c.execute("INSERT INTO users(email, password_hash, created_at, signup_ip) "
                            "VALUES ('eski@test.uz', 'x', 1, '1.2.3.4') RETURNING id").fetchone()[0]
            c.execute("INSERT INTO ledger(user_id, kind, amount, balance_after, created_at) VALUES (%s,'topup',5,5,1)", (uid,))
            with pytest.raises(psycopg.errors.CheckViolation):
                c.execute("INSERT INTO ledger(user_id, kind, amount, balance_after, created_at) VALUES (%s,'referral',1,6,1)", (uid,))

            assert SCHEMA_VERSION >= 2
            assert apply_schema(c, TEST_APP_ROLE) == SCHEMA_VERSION
            assert apply_schema(c, TEST_APP_ROLE) == SCHEMA_VERSION   # re-running is safe
            row = c.execute("SELECT email, ref_code, referral_paused, signup_device FROM users").fetchone()
            assert row == ("eski@test.uz", None, 0, None)
            assert c.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 1
            c.execute("INSERT INTO ledger(user_id, kind, amount, balance_after, created_at) VALUES (%s,'referral',1,6,1)", (uid,))
            with pytest.raises(psycopg.errors.CheckViolation):
                c.execute("INSERT INTO ledger(user_id, kind, amount, balance_after, created_at) VALUES (%s,'boshqa',1,6,1)", (uid,))
            checks = c.execute("SELECT COUNT(*) FROM pg_constraint WHERE conrelid='ledger'::regclass AND contype='c'").fetchone()[0]
            assert checks == 1
            idx = {r[0] for r in c.execute("SELECT indexname FROM pg_indexes WHERE schemaname=%s", (name,))}
            assert {"idx_users_ref_code", "idx_referrals_referrer", "idx_ref_rewards_referrer"} <= idx
            assert schema_version(c) == SCHEMA_VERSION
        finally:
            c.execute("RESET search_path")
            c.execute(pgsql.SQL("DROP SCHEMA {} CASCADE").format(pgsql.Identifier(name)))
