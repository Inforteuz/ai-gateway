from .conftest import CHAT, CSRF, SOM, add_key, add_user, bearer, wallet


def login(client, email="a@test.uz", password="parol-12345"):
    return client.post("/portal/auth/login", json={"email": email, "password": password}, headers=CSRF)


def test_login_logout_and_session(env):
    add_user(env)
    assert env.client.get("/portal/auth/me").json()["user"] is None
    assert login(env.client, password="xato-parol").status_code == 401
    r = login(env.client)
    assert r.status_code == 200 and r.json()["user"]["email"] == "a@test.uz"
    assert "httponly" in r.headers["set-cookie"].lower() and "samesite=lax" in r.headers["set-cookie"].lower()
    assert env.client.get("/portal/auth/me").json()["user"]["role"] == "user"
    env.client.post("/portal/auth/logout", headers=CSRF)
    assert env.client.get("/portal/auth/me").json()["user"] is None
    assert env.client.get("/portal/account/summary").status_code == 401


def test_csrf_header_required_for_state_changes(env):
    add_user(env)
    assert env.client.post("/portal/auth/login", json={"email": "a@test.uz", "password": "parol-12345"}).status_code == 403
    login(env.client)
    assert env.client.post("/portal/keys", json={"name": "k"}).status_code == 403


def test_login_brute_force_lockout(env):
    add_user(env)
    for _ in range(8):
        assert login(env.client, password="x").status_code == 401
    r = login(env.client)  # even the correct password is no longer accepted
    assert r.status_code == 429


def test_unknown_email_does_not_leak(env):
    r1 = login(env.client, email="yoq@test.uz", password="whatever1")
    add_user(env)
    r2 = login(env.client, password="notogri-12")
    assert r1.status_code == r2.status_code == 401 and r1.json() == r2.json()


def test_registration_closed_by_default_then_open(env):
    body = {"email": "new@test.uz", "name": "Yangi", "password": "yangi-parol-1"}
    assert env.client.post("/portal/auth/register", json=body, headers=CSRF).status_code == 403
    env.db.set_setting("registration_open", "1")
    env.db.set_setting("signup_bonus_micro", str(5 * SOM))
    r = env.client.post("/portal/auth/register", json=body, headers=CSRF)
    assert r.status_code == 200
    uid = env.db.one("SELECT id FROM users WHERE email='new@test.uz'")["id"]
    assert wallet(env, uid) == (5 * SOM, 0)
    assert env.db.one("SELECT kind FROM ledger WHERE user_id=?", (uid,))["kind"] == "bonus"
    assert env.client.post("/portal/auth/register", json=body, headers=CSRF).status_code == 409


def test_key_lifecycle_full_key_shown_once(env):
    add_user(env, balance_som=1000)
    login(env.client)
    r = env.client.post("/portal/keys", json={"name": "Hamkor"}, headers=CSRF)
    full = r.json()["key"]
    assert full.startswith("sk-sf-") and len(full) > 40
    listing = env.client.get("/portal/keys").json()["items"]
    assert full not in str(listing) and listing[0]["prefix"] == full[:12] and listing[0]["last4"] == full[-4:]
    assert env.db.one("SELECT key_hash FROM api_keys")["key_hash"] != full  # only the hash is stored
    assert env.client.post("/v1/chat/completions", json=CHAT, headers=bearer(full)).status_code == 200
    kid = listing[0]["id"]
    env.client.post(f"/portal/keys/{kid}/revoke", headers=CSRF)
    assert env.client.post("/v1/chat/completions", json=CHAT, headers=bearer(full)).status_code == 401


def test_user_cannot_touch_other_users_keys(env):
    add_user(env, "a@test.uz")
    b = add_user(env, "b@test.uz")
    _, other_key = add_key(env, b)
    login(env.client)
    assert env.client.post(f"/portal/keys/{other_key}/revoke", headers=CSRF).status_code == 404
    assert env.client.patch(f"/portal/keys/{other_key}", json={"name": "x"}, headers=CSRF).status_code == 404
    assert env.client.get("/portal/keys").json()["items"] == []


def test_usage_and_summary_reflect_requests(env):
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    for _ in range(3):
        env.client.post("/v1/chat/completions", json=CHAT, headers=bearer(key))
    login(env.client)
    s = env.client.get("/portal/account/summary").json()
    assert s["month"]["requests"] == 3 and s["today"]["requests"] == 3
    assert s["balance"] == 1000 * SOM - 3 * 320_000
    u = env.client.get("/portal/usage").json()
    assert u["total"] == 3 and u["items"][0]["prompt_tokens"] == 11
    d = env.client.get("/portal/usage/daily?days=7").json()["items"]
    assert len(d) == 7 and d[-1]["requests"] == 3
    led = env.client.get("/portal/ledger").json()
    assert led["total"] == 4 and led["items"][0]["kind"] == "usage"  # 1 top-up + 3 charges


def test_admin_only_area(env):
    add_user(env, "a@test.uz")
    login(env.client)
    assert env.client.get("/portal/admin/users").status_code == 403
    assert env.client.get("/portal/admin/stats").status_code == 403


def test_admin_credit_topup_flow_and_suspend(env):
    add_user(env, "admin@test.uz", role="admin")
    uid = add_user(env, "u@test.uz")
    key, _ = add_key(env, uid)
    # the user submits a request
    login(env.client, "u@test.uz")
    assert env.client.post("/portal/topups", json={"amount": 100, "note": "x"}, headers=CSRF).status_code == 400  # too small
    r = env.client.post("/portal/topups", json={"amount": 50_000 * SOM, "note": "Payme: 14:20"}, headers=CSRF)
    tid = r.json()["item"]["id"]
    env.client.post("/portal/auth/logout", headers=CSRF)
    # the admin approves
    login(env.client, "admin@test.uz")
    pend = env.client.get("/portal/admin/topups").json()
    assert pend["pending"] == 1 and pend["items"][0]["email"] == "u@test.uz"
    assert env.client.post(f"/portal/admin/topups/{tid}/resolve", json={"action": "approve", "admin_note": "ok"}, headers=CSRF).status_code == 200
    assert wallet(env, uid)[0] == 50_000 * SOM
    assert env.client.post(f"/portal/admin/topups/{tid}/resolve", json={"action": "approve"}, headers=CSRF).status_code == 409  # not twice
    assert wallet(env, uid)[0] == 50_000 * SOM
    # manual adjustment
    r = env.client.post(f"/portal/admin/users/{uid}/credit", json={"amount": -1000 * SOM, "kind": "adjustment", "note": "xato"}, headers=CSRF)
    assert r.json()["balance"] == 49_000 * SOM
    assert env.client.post(f"/portal/admin/users/{uid}/credit", json={"amount": -5, "kind": "topup"}, headers=CSRF).status_code == 400
    # suspension
    env.client.patch(f"/portal/admin/users/{uid}", json={"status": "suspended"}, headers=CSRF)
    assert env.client.post("/v1/chat/completions", json=CHAT, headers=bearer(key)).status_code == 403


def test_cannot_remove_last_admin(env):
    aid = add_user(env, "admin@test.uz", role="admin")
    login(env.client, "admin@test.uz")
    r = env.client.patch(f"/portal/admin/users/{aid}", json={"status": "suspended"}, headers=CSRF)
    assert r.status_code == 400 and r.json()["error"]["code"] == "last_admin"


def test_admin_create_user_returns_temp_password_once(env):
    add_user(env, "admin@test.uz", role="admin")
    login(env.client, "admin@test.uz")
    r = env.client.post("/portal/admin/users", json={"email": "p@test.uz", "name": "Sherik", "initial_credit": 20_000 * SOM}, headers=CSRF)
    pw = r.json()["temp_password"]
    assert pw and len(pw) >= 12 and r.json()["item"]["balance"] == 20_000 * SOM
    env.client.post("/portal/auth/logout", headers=CSRF)
    assert login(env.client, "p@test.uz", pw).status_code == 200
    reset = None
    env.client.post("/portal/auth/logout", headers=CSRF)
    login(env.client, "admin@test.uz")
    pid = env.db.one("SELECT id FROM users WHERE email='p@test.uz'")["id"]
    reset = env.client.post(f"/portal/admin/users/{pid}/reset-password", headers=CSRF).json()["temp_password"]
    env.client.post("/portal/auth/logout", headers=CSRF)
    assert login(env.client, "p@test.uz", pw).status_code == 401 and login(env.client, "p@test.uz", reset).status_code == 200


def test_admin_model_price_change_applies_to_next_request(env):
    aid = add_user(env, "admin@test.uz", role="admin", balance_som=1000)
    key, _ = add_key(env, aid)
    login(env.client, "admin@test.uz")
    env.client.patch("/portal/admin/models/model-1.0", json={"price_in_per_1m": 20_000 * SOM, "price_out_per_1m": 60_000 * SOM}, headers=CSRF)
    env.client.post("/v1/chat/completions", json=CHAT, headers=bearer(key))
    assert env.db.one("SELECT cost FROM usage_logs")["cost"] == 2 * 320_000
    env.client.patch("/portal/admin/models/model-1.0", json={"enabled": False}, headers=CSRF)
    assert env.client.post("/v1/chat/completions", json=CHAT, headers=bearer(key)).status_code == 404


def test_public_endpoints_and_status(env):
    assert env.client.get("/portal/models").json()["items"][0]["id"] == "model-1.0"
    assert env.client.get("/portal/status").json()["model_online"] is True
    add_user(env, "admin@test.uz", role="admin")
    login(env.client, "admin@test.uz")
    st = env.client.get("/portal/admin/stats").json()
    assert st["model_online"] is True and len(st["daily"]) == 30
    names = [m["name"] for m in env.client.get("/portal/admin/upstream-models").json()["items"]]
    assert "llama3.2:3b" in names


def test_ledger_always_matches_wallet(env):
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    for _ in range(5):
        env.client.post("/v1/chat/completions", json=CHAT, headers=bearer(key))
    total = env.db.scalar("SELECT SUM(amount) FROM ledger WHERE user_id=?", (uid,))
    assert total == wallet(env, uid)[0]
    assert env.db.scalar("SELECT SUM(cost) FROM usage_logs WHERE user_id=?", (uid,)) == -env.db.scalar(
        "SELECT SUM(amount) FROM ledger WHERE user_id=? AND kind='usage'", (uid,))
