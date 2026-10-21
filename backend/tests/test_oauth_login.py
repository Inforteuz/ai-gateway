"""Login via Google and Telegram: signature checks, account linking, limits."""
import base64
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qs, parse_qsl, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import oauth

from .conftest import CSRF, SOM, add_user

BOT_TOKEN = "123456:TEST-BOT-TOKEN"
CLIENT_ID = "client-123.apps.googleusercontent.com"
CLIENT_SECRET = "google-secret"


# --- common helpers -------------------------------------------------------------

def open_signup(env, bonus_som=10_000):
    env.db.set_setting("registration_open", "1")
    env.db.set_setting("signup_bonus_micro", str(bonus_som * SOM))


def enable_telegram(env, token=BOT_TOKEN):
    """Enables Telegram login (off by default: it needs `/setdomain`)."""
    env.db.set_setting("alert_telegram_token", token)
    env.db.set_setting("telegram_login_enabled", "1")


def logout(env):
    env.client.post("/portal/auth/logout", headers=CSRF)


def logged_in(env):
    return env.client.get("/portal/auth/me").json()["user"]


def count_users(env):
    return env.db.scalar("SELECT COUNT(*) FROM users")


def balance(env, uid):
    return env.db.scalar("SELECT balance FROM wallets WHERE user_id=?", (uid,))


def oauth_error(response):
    """The error code from the /login?oauth_error=... URL."""
    assert response.status_code == 303
    return parse_qs(urlparse(response.headers["location"]).query).get("oauth_error", [""])[0]


# --- Telegram helpers -----------------------------------------------------------

def tg_params(telegram_id="555001", *, token=BOT_TOKEN, age=0, **extra):
    """The fields the Telegram widget returns, with a real signature."""
    fields = {"id": telegram_id, "first_name": "Ali", "username": "ali_test",
              "auth_date": str(int(time.time()) - age), **extra}
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hashlib.sha256(token.encode()).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return fields


def tg_login(env, params):
    return env.client.get("/portal/auth/telegram/callback", params=params, follow_redirects=False)


# --- Google helpers -------------------------------------------------------------

def _seg(obj) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def id_token(**claims) -> str:
    body = {"iss": "https://accounts.google.com", "aud": CLIENT_ID, "sub": "google-sub-1",
            "email": "new-user@example.com", "email_verified": True, "name": "Yangi Foydalanuvchi",
            "exp": int(time.time()) + 300, **claims}
    return f"{_seg({'alg': 'RS256'})}.{_seg(body)}.imzo"


def configure_google(env, *, claims=None, status=200):
    """Stores the Google keys and mocks the token endpoint."""
    for k, v in {"google_client_id": CLIENT_ID, "google_client_secret": CLIENT_SECRET,
                 "site_base_url": "http://testserver"}.items():
        env.db.set_setting(k, v)
    state = {"claims": dict(claims or {}), "status": status, "nonce": None}
    fake = FastAPI()

    @fake.post("/token")
    async def token(request: Request):
        # `python-multipart` is not installed, so we parse the body ourselves
        state["form"] = dict(parse_qsl((await request.body()).decode()))
        if state["status"] != 200:
            return JSONResponse({"error": "invalid_grant"}, status_code=state["status"])
        # Real Google also echoes the `nonce` from the request.
        body = {"nonce": state["nonce"], **state["claims"]}
        return {"id_token": id_token(**body), "token_type": "Bearer"}

    env.app.state.notify_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=fake))
    env.goauth = state
    return state


def google_start(env):
    """Starts the flow and returns the `state` Google will send back."""
    r = env.client.get("/portal/auth/google/start", follow_redirects=False)
    assert r.status_code == 303
    url = urlparse(r.headers["location"])
    q = parse_qs(url.query)
    assert f"{url.scheme}://{url.netloc}{url.path}" == oauth.GOOGLE_AUTH_URL
    # From configuration (site_base_url set by configure_google), never from the request's Host header
    assert q["redirect_uri"] == ["http://testserver/portal/auth/google/callback"]
    assert q["code_challenge_method"] == ["S256"] and q["scope"] == ["openid email profile"]
    env.goauth["nonce"] = q["nonce"][0]
    return q["state"][0]


def google_callback(env, state, code="auth-code"):
    return env.client.get("/portal/auth/google/callback", params={"code": code, "state": state},
                          follow_redirects=False)


def google_login(env):
    """Full flow: start + callback."""
    return google_callback(env, google_start(env))


# --- button visibility ----------------------------------------------------------

def test_buttons_hidden_until_keys_are_set(env):
    assert env.client.get("/portal/auth/providers").json() == {"google": False, "telegram": "", "captcha": None}
    # A half-configured Google (client_id only) is not shown either
    env.db.set_setting("google_client_id", CLIENT_ID)
    assert env.client.get("/portal/auth/providers").json()["google"] is False
    env.db.set_setting("google_client_secret", CLIENT_SECRET)
    assert env.client.get("/portal/auth/providers").json()["google"] is True


def test_telegram_button_is_off_until_admin_enables_it(env):
    """Off by default: the code cannot tell whether `/setdomain` was set in BotFather."""
    env.db.set_setting("alert_telegram_token", BOT_TOKEN)
    env.db.set_setting("telegram_login_bot", "@gateway_bot")
    assert env.client.get("/portal/auth/providers").json()["telegram"] == ""
    env.db.set_setting("telegram_login_enabled", "1")
    assert env.client.get("/portal/auth/providers").json()["telegram"] == "gateway_bot"
    env.db.set_setting("telegram_login_enabled", "0")   # the admin turned it off again
    assert env.client.get("/portal/auth/providers").json()["telegram"] == ""


# --- Telegram -----------------------------------------------------------------

def test_telegram_login_creates_account_with_bonus(env):
    open_signup(env)
    enable_telegram(env)
    r = tg_login(env, tg_params())
    assert r.status_code == 303 and r.headers["location"] == "/app"
    user = logged_in(env)
    assert user["email"] == "tg555001@telegram.invalid" and user["name"] == "Ali"
    row = env.db.one("SELECT telegram_id, password_hash, signup_ip FROM users WHERE id=?", (user["id"],))
    assert row["telegram_id"] == "555001" and row["signup_ip"]
    # No password set: a password login attempt can never succeed
    assert not row["password_hash"].startswith("scrypt$")
    assert balance(env, user["id"]) == 10_000 * SOM


def test_telegram_signature_must_match_bot_token(env):
    open_signup(env)
    enable_telegram(env)
    # 1) Signed with a different token
    assert oauth_error(tg_login(env, tg_params(token="999:BOSHQA"))) == "telegram_bad_signature"
    # 2) A field was changed after signing
    tampered = tg_params() | {"first_name": "Admin"}
    assert oauth_error(tg_login(env, tampered)) == "telegram_bad_signature"
    # 3) No signature at all
    no_hash = {k: v for k, v in tg_params().items() if k != "hash"}
    assert oauth_error(tg_login(env, no_hash)) == "telegram_bad_data"
    assert count_users(env) == 0 and logged_in(env) is None


def test_telegram_stale_auth_date_is_rejected(env):
    open_signup(env)
    enable_telegram(env)
    assert oauth_error(tg_login(env, tg_params(age=oauth.TELEGRAM_MAX_AGE + 60))) == "telegram_expired"
    assert count_users(env) == 0


def test_telegram_callback_is_refused_when_not_configured(env):
    open_signup(env)
    # No token
    assert oauth_error(tg_login(env, tg_params())) == "telegram_not_configured"
    # Token present but not enabled by the admin — the callback must not work either
    env.db.set_setting("alert_telegram_token", BOT_TOKEN)
    assert oauth_error(tg_login(env, tg_params())) == "telegram_not_configured"


def test_telegram_second_login_reuses_same_account(env):
    open_signup(env)
    enable_telegram(env)
    tg_login(env, tg_params())
    first = logged_in(env)["id"]
    logout(env)
    tg_login(env, tg_params())
    assert logged_in(env)["id"] == first and count_users(env) == 1
    assert balance(env, first) == 10_000 * SOM   # the bonus is not granted twice


def test_telegram_respects_signup_bonus_limit_per_ip(env):
    """The new path also goes through `abuse.signup_bonus`: one IP, one bonus."""
    open_signup(env)
    enable_telegram(env)
    tg_login(env, tg_params("111"))
    first = logged_in(env)["id"]
    logout(env)
    tg_login(env, tg_params("222"))
    second = logged_in(env)["id"]
    assert second != first
    assert balance(env, first) == 10_000 * SOM and balance(env, second) == 0


def test_telegram_respects_max_accounts_per_ip(env):
    open_signup(env, bonus_som=0)
    enable_telegram(env)
    env.db.set_setting("max_accounts_per_ip", "2")
    for tid in ("1", "2"):
        tg_login(env, tg_params(tid))
        logout(env)
    assert oauth_error(tg_login(env, tg_params("3"))) == "too_many_accounts"
    assert count_users(env) == 2


def test_telegram_cannot_open_account_when_registration_closed(env):
    enable_telegram(env)
    assert oauth_error(tg_login(env, tg_params())) == "registration_closed"
    assert count_users(env) == 0


def test_telegram_login_works_for_linked_account_when_registration_closed(env):
    open_signup(env, bonus_som=0)
    enable_telegram(env)
    tg_login(env, tg_params())
    uid = logged_in(env)["id"]
    logout(env)
    env.db.set_setting("registration_open", "0")
    assert tg_login(env, tg_params()).headers["location"] == "/app"
    assert logged_in(env)["id"] == uid


def test_telegram_login_refused_for_suspended_account(env):
    open_signup(env, bonus_som=0)
    enable_telegram(env)
    tg_login(env, tg_params())
    uid = logged_in(env)["id"]
    logout(env)
    with env.db.tx() as c:
        c.execute("UPDATE users SET status='suspended' WHERE id=?", (uid,))
    assert oauth_error(tg_login(env, tg_params())) == "account_suspended"
    assert logged_in(env) is None


# --- Google -------------------------------------------------------------------

def test_google_login_creates_account_and_sends_pkce_verifier(env):
    open_signup(env)
    state = configure_google(env)
    r = google_login(env)
    assert r.status_code == 303 and r.headers["location"] == "/app"
    user = logged_in(env)
    assert user["email"] == "new-user@example.com" and user["name"] == "Yangi Foydalanuvchi"
    assert env.db.one("SELECT google_sub FROM users WHERE id=?", (user["id"],))["google_sub"] == "google-sub-1"
    assert balance(env, user["id"]) == 10_000 * SOM
    # The code exchange sent both the PKCE verifier and the client_secret
    form = state["form"]
    assert form["code"] == "auth-code" and form["code_verifier"] and form["client_secret"] == CLIENT_SECRET
    assert form["grant_type"] == "authorization_code"


def test_google_second_login_reuses_same_account(env):
    open_signup(env)
    configure_google(env)
    google_login(env)
    first = logged_in(env)["id"]
    logout(env)
    google_login(env)
    assert logged_in(env)["id"] == first and count_users(env) == 1
    assert balance(env, first) == 10_000 * SOM


def test_google_links_to_existing_account_with_same_email(env):
    uid = add_user(env, email="existing@example.com")
    open_signup(env)
    configure_google(env, claims={"email": "existing@example.com"})
    assert google_login(env).headers["location"] == "/app"
    assert logged_in(env)["id"] == uid and count_users(env) == 1
    assert env.db.one("SELECT google_sub FROM users WHERE id=?", (uid,))["google_sub"] == "google-sub-1"
    assert balance(env, uid) == 0   # linking is not a signup, no bonus
    # Pre-registration takeover guard: the password set before Google verified the email no longer works
    logout(env)
    assert env.client.post("/portal/auth/login", json={"email": "existing@example.com", "password": "parol-12345"},
                           headers=CSRF).status_code == 401


def test_google_link_revokes_sessions_of_a_pre_registered_account(env):
    """Someone registered the victim's email with their own password before the victim used Google."""
    attacker = TestClient(env.app)
    uid = add_user(env, email="victim@example.com", password="attacker-pass-1")
    open_signup(env)
    assert attacker.post("/portal/auth/login", json={"email": "victim@example.com", "password": "attacker-pass-1"},
                         headers=CSRF).status_code == 200
    configure_google(env, claims={"email": "victim@example.com"})
    assert google_login(env).headers["location"] == "/app" and logged_in(env)["id"] == uid
    assert attacker.get("/portal/auth/me").json()["user"] is None          # attacker's session is gone
    assert attacker.post("/portal/auth/login", json={"email": "victim@example.com", "password": "attacker-pass-1"},
                         headers=CSRF).status_code == 401


def test_google_link_keeps_admin_password(env):
    add_user(env, email="boss@example.com", role="admin")
    open_signup(env)
    configure_google(env, claims={"email": "boss@example.com"})
    assert google_login(env).headers["location"] == "/app"
    logout(env)
    assert env.client.post("/portal/auth/login", json={"email": "boss@example.com", "password": "parol-12345"},
                           headers=CSRF).status_code == 200


def test_google_email_with_another_linked_account_is_refused(env):
    open_signup(env)
    configure_google(env)
    google_login(env)              # new-user@example.com -> google-sub-1
    logout(env)
    env.goauth["claims"] = {"sub": "google-sub-2"}
    assert oauth_error(google_login(env)) == "already_linked"
    assert count_users(env) == 1


def test_google_state_mismatch_is_refused(env):
    open_signup(env)
    configure_google(env)
    google_start(env)
    assert oauth_error(google_callback(env, "boshqa-state")) == "google_state"
    env.client.cookies.clear()     # an attempt without the cookie fails too
    assert oauth_error(google_callback(env, "nimadir")) == "google_state"
    assert count_users(env) == 0


@pytest.mark.parametrize("claims, expected", [
    ({"aud": "boshqa-ilova"}, "google_bad_token"),
    ({"iss": "https://evil.example"}, "google_bad_token"),
    ({"exp": int(time.time()) - 10}, "google_expired"),
    ({"email_verified": False}, "google_no_email"),
    ({"email": ""}, "google_no_email"),
    ({"nonce": "yolg'on-nonce"}, "google_bad_token"),
    ({"sub": ""}, "google_bad_token"),
])
def test_google_id_token_claims_are_checked(env, claims, expected):
    open_signup(env)
    configure_google(env, claims=claims)
    assert oauth_error(google_login(env)) == expected
    assert count_users(env) == 0 and logged_in(env) is None


def test_google_token_endpoint_failure_is_reported(env):
    open_signup(env)
    configure_google(env, status=400)
    assert oauth_error(google_login(env)) == "google_rejected"


def test_google_start_without_keys_is_refused(env):
    assert env.client.get("/portal/auth/google/start", follow_redirects=False).status_code == 503


def test_google_cannot_open_account_when_registration_closed(env):
    configure_google(env)
    assert oauth_error(google_login(env)) == "registration_closed"
    assert count_users(env) == 0


def test_google_user_has_no_password_to_change(env):
    open_signup(env)
    configure_google(env)
    google_login(env)
    r = env.client.post("/portal/auth/password", json={"current": "nimadir", "new": "yangi-parol-123"}, headers=CSRF)
    assert r.status_code == 400 and r.json()["error"]["code"] == "no_password"


def test_google_cancelled_by_user(env):
    open_signup(env)
    configure_google(env)
    google_start(env)
    r = env.client.get("/portal/auth/google/callback", params={"error": "access_denied"}, follow_redirects=False)
    assert oauth_error(r) == "google_cancelled"


def test_forged_host_header_does_not_reach_redirect_uri_or_links(env):
    configure_google(env)
    r = env.client.get("/portal/auth/google/start", headers={"Host": "evil.example"}, follow_redirects=False)
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["redirect_uri"] == ["http://testserver/portal/auth/google/callback"]   # configured, not evil.example
    assert oauth.site_url({}) == "https://example.com"                                # SF_SITE_URL fallback
    assert oauth.site_url({"site_base_url": "https://panel.example/"}) == "https://panel.example"
