"""AG-11: role checks and object ownership (IDOR) across the portal API."""
import re

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from .conftest import CHAT, CSRF, add_key, add_user, bearer


def login(client, email, password="parol-12345"):
    assert client.post("/portal/auth/login", json={"email": email, "password": password}, headers=CSRF).status_code == 200


def all_routes(routes):
    """APIRoutes, also inside included routers (newer FastAPI wraps them)."""
    for r in routes:
        if isinstance(r, APIRoute):
            yield r
        inner = getattr(r, "original_router", None)     # fastapi's _IncludedRouter
        if inner is not None:
            yield from all_routes(inner.routes)


def admin_routes(app):
    for r in all_routes(app.routes):
        if r.path.startswith("/portal/admin"):
            for method in r.methods - {"HEAD", "OPTIONS"}:
                yield method, re.sub(r"\{[^}]+\}", "1", r.path)


def test_every_admin_route_refuses_normal_users_and_anonymous(env):
    add_user(env, "user@test.uz")
    user = TestClient(env.app)
    login(user, "user@test.uz")
    anon = TestClient(env.app)
    routes = sorted(set(admin_routes(env.app)))
    assert len(routes) > 30                      # the check really covers the admin surface
    for method, path in routes:
        r = user.request(method, path, headers=CSRF, json={})
        assert r.status_code == 403, (method, path, r.status_code)
        r = anon.request(method, path, headers=CSRF, json={})
        assert r.status_code == 401, (method, path, r.status_code)


def test_state_changing_portal_routes_need_the_csrf_header(env):
    add_user(env, "user@test.uz")
    user = TestClient(env.app)
    login(user, "user@test.uz")
    r = user.post("/portal/keys", json={"name": "x"})          # no X-SF-CSRF
    assert r.status_code in (400, 403)
    assert user.post("/portal/keys", json={"name": "x"}, headers=CSRF).status_code == 200


def test_user_cannot_touch_another_users_keys(env):
    a = add_user(env, "a@test.uz")
    add_user(env, "b@test.uz")
    _, key_a = add_key(env, a)
    b = TestClient(env.app)
    login(b, "b@test.uz")
    assert b.patch(f"/portal/keys/{key_a}", json={"name": "stolen"}, headers=CSRF).status_code == 404
    assert b.post(f"/portal/keys/{key_a}/revoke", headers=CSRF).status_code == 404
    assert env.db.one("SELECT name, status FROM api_keys WHERE id=?", (key_a,))["status"] == "active"
    assert all(k["id"] != key_a for k in b.get("/portal/keys").json()["items"])


def test_user_cannot_read_another_users_usage_or_ledger(env):
    a = add_user(env, "a@test.uz", balance_som=1000)
    add_user(env, "b@test.uz")
    raw_a, key_a = add_key(env, a)
    assert env.client.post("/v1/chat/completions", json=CHAT, headers=bearer(raw_a)).status_code == 200
    b = TestClient(env.app)
    login(b, "b@test.uz")
    assert b.get("/portal/usage", params={"key_id": key_a}).json()["items"] == []
    assert b.get("/portal/usage").json()["items"] == []
    assert b.get("/portal/ledger").json()["items"] == []
    assert b.get("/portal/account/summary").json()["balance"] == 0


def test_logout_invalidates_the_session(env):
    add_user(env, "user@test.uz")
    c = TestClient(env.app)
    login(c, "user@test.uz")
    cookie = c.cookies.get("sf_session") or next(iter(c.cookies.values()))
    assert c.post("/portal/auth/logout", headers=CSRF).status_code == 200
    replay = TestClient(env.app)
    replay.cookies.set("sf_session", cookie)
    assert replay.get("/portal/auth/me").json()["user"] is None


def test_api_key_of_another_user_is_its_own_principal(env):
    """An API key only ever acts for its owner: B's key spends B's balance, not A's."""
    a = add_user(env, "a@test.uz", balance_som=100)
    b = add_user(env, "b@test.uz")
    raw_b, _ = add_key(env, b)
    r = env.client.post("/v1/chat/completions", json=CHAT, headers=bearer(raw_b))
    assert r.status_code == 402
    assert env.db.scalar("SELECT balance FROM wallets WHERE user_id=?", (a,)) == 100 * 1_000_000


def test_archive_hides_the_account_and_revokes_its_keys(env):
    """Accounts are archived, never deleted: the ledger behind them has to stay."""
    admin_mail = "arxiv-admin@test.uz"
    add_user(env, email=admin_mail, role="admin")
    uid = add_user(env, email="arxiv@test.uz")
    add_key(env, uid)
    login(env.client, admin_mail)

    def emails(query=""):
        return [u["email"] for u in env.client.get("/portal/admin/users" + query).json()["items"]]

    assert "arxiv@test.uz" in emails()

    assert env.client.post(f"/portal/admin/users/{uid}/archive", headers=CSRF).status_code == 200
    assert "arxiv@test.uz" not in emails()                     # gone from the list
    arch = env.client.get("/portal/admin/users?archived=1").json()["items"]
    assert [u["email"] for u in arch] == ["arxiv@test.uz"]
    assert arch[0]["active_keys"] == 0                         # its keys were revoked

    # the row itself survives, and archiving twice is refused
    assert env.client.get(f"/portal/admin/users/{uid}").status_code == 200
    assert env.client.post(f"/portal/admin/users/{uid}/archive", headers=CSRF).status_code == 400

    assert env.client.post(f"/portal/admin/users/{uid}/restore", headers=CSRF).status_code == 200
    assert "arxiv@test.uz" in emails()
