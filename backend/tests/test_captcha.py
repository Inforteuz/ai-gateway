"""Captcha (Google reCAPTCHA v3): login, registration and password reset.

Google's siteverify API is mocked with `httpx.MockTransport` — the tests never
touch the network. The keys are fake and do not look like real ones.
"""
from dataclasses import replace
from urllib.parse import parse_qs

import httpx
import pytest

from app import captcha

from .conftest import CSRF, add_user

SITE_KEY = "test-site-key"
SECRET = "test-secret-value"
GOOD = "token-ok"


@pytest.fixture
def cf(env):
    """Enables captcha and mocks Google. `cf[...]` controls the response.

    By default the `action` in the response matches the endpoint the request was
    sent to (as a real browser does); `cf["action"]` forces a different one.
    """
    env.db.set_setting("recaptcha_site_key", SITE_KEY)
    env.db.set_setting("recaptcha_secret", SECRET)
    state = {"mode": "ok", "calls": [], "action": None, "score": 0.9, "hostname": "example.com",
             "expect": "login"}

    def handler(request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        state["calls"].append(form)
        mode = state["mode"]
        if mode == "timeout":
            raise httpx.ConnectTimeout("timeout", request=request)
        if mode == "500":
            return httpx.Response(500, text="oops")
        if mode == "html":
            return httpx.Response(200, text="<html>")
        if mode == "bad_secret":
            return httpx.Response(200, json={"success": False, "error-codes": ["invalid-input-secret"]})
        ok = form.get("secret") == SECRET and form.get("response") == GOOD
        if not ok:
            return httpx.Response(200, json={"success": False, "error-codes": ["invalid-input-response"]})
        body = {"success": True, "challenge_ts": "2026-10-20T10:00:00Z", "hostname": state["hostname"],
                "action": state["action"] if state["action"] is not None else state["expect"]}
        if state["score"] is not None:
            body["score"] = state["score"]
        return httpx.Response(200, json=body)

    env.app.state.captcha_http = httpx.Client(transport=httpx.MockTransport(handler))
    env.cf = state
    return state


def login(env, token=None, password="parol-12345", email="a@test.uz"):
    _expect(env, "login")
    body = {"email": email, "password": password}
    if token is not None:
        body["captcha_token"] = token
    return env.client.post("/portal/auth/login", json=body, headers=CSRF)


def _expect(env, action):
    st = getattr(env, "cf", None)
    if st is not None:
        st["expect"] = action


def register(env, token=None, email="yangi@test.uz"):
    _expect(env, "register")
    env.db.set_setting("registration_open", "1")
    body = {"email": email, "password": "parol-12345", "name": "Yangi"}
    if token is not None:
        body["captcha_token"] = token
    return env.client.post("/portal/auth/register", json=body, headers=CSRF)


def forgot(env, token=None, email="a@test.uz"):
    _expect(env, "password_reset")
    body = {"email": email}
    if token is not None:
        body["captcha_token"] = token
    return env.client.post("/portal/auth/password/forgot", json=body, headers=CSRF)


def code(r):
    return r.json()["error"]["code"]


# --- disabled -------------------------------------------------------------------

def test_disabled_without_keys(env):
    """No keys, no captcha: existing flows work unchanged."""
    add_user(env)
    assert env.client.get("/portal/auth/providers").json()["captcha"] is None
    assert login(env).status_code == 200
    assert register(env).status_code == 200
    assert forgot(env, email="yoq@test.uz").json()["ok"] is True


def test_only_one_key_keeps_it_disabled(env):
    env.db.set_setting("recaptcha_site_key", SITE_KEY)
    add_user(env)
    assert env.client.get("/portal/auth/providers").json()["captcha"] is None
    assert login(env).status_code == 200


def test_admin_switch_disables(env, cf):
    env.db.set_setting("captcha_enabled", "0")
    add_user(env)
    assert login(env).status_code == 200
    assert cf["calls"] == []


def test_env_kill_switch_disables(env, cf):
    """SF_CAPTCHA=0: the admin can still log in when Google is down."""
    env.app.state.config = replace(env.app.state.config, captcha_enabled=False)
    add_user(env)
    assert login(env).status_code == 200
    assert cf["calls"] == []


def test_env_keys_take_priority(env, cf):
    env.db.set_setting("recaptcha_secret", "eski-sir")
    env.app.state.config = replace(env.app.state.config, recaptcha_site_key="env-site", recaptcha_secret=SECRET)
    add_user(env)
    assert env.client.get("/portal/auth/providers").json()["captcha"]["site_key"] == "env-site"
    assert login(env, GOOD).status_code == 200
    assert cf["calls"][0]["secret"] == SECRET


# --- enabled: login -------------------------------------------------------------

def test_providers_exposes_only_public_key(env, cf):
    data = env.client.get("/portal/auth/providers").json()
    assert data["captcha"] == {"site_key": SITE_KEY, "login_after_fails": 0}
    assert SECRET not in env.client.get("/portal/auth/providers").text


def test_login_requires_token(env, cf):
    add_user(env)
    r = login(env)
    assert r.status_code == 400 and code(r) == "captcha_required"
    assert cf["calls"] == []            # an empty token is not sent to Google
    assert env.client.get("/portal/auth/me").json()["user"] is None


def test_login_with_valid_token(env, cf):
    add_user(env)
    r = login(env, GOOD)
    assert r.status_code == 200
    call = cf["calls"][0]
    assert call["secret"] == SECRET and call["response"] == GOOD and call["remoteip"]


def test_login_with_invalid_token(env, cf):
    add_user(env)
    r = login(env, "soxta")
    assert r.status_code == 400 and code(r) == "captcha_failed"
    assert env.client.get("/portal/auth/me").json()["user"] is None


def test_captcha_checked_before_password(env, cf):
    """Without a valid captcha the password is not checked and no failed attempt is counted."""
    add_user(env)
    r = login(env, "soxta", password="notogri")
    assert code(r) == "captcha_failed"
    assert env.app.state.auth_limiter.count(("email", "a@test.uz")) == 0


def test_too_long_token_rejected_by_schema(env, cf):
    add_user(env)
    assert login(env, "x" * (captcha.TOKEN_MAX + 1)).status_code == 422
    assert cf["calls"] == []


@pytest.mark.parametrize("mode", ["timeout", "500", "html", "bad_secret"])
def test_provider_failure_rejects(env, cf, mode, caplog):
    """If Google does not answer or the config is wrong, the request is rejected (fail-closed)."""
    add_user(env)
    cf["mode"] = mode
    r = login(env, GOOD)
    assert r.status_code == 503 and code(r) == "captcha_unavailable"
    assert "vaqtincha" in r.json()["error"]["message"]
    assert env.client.get("/portal/auth/me").json()["user"] is None
    assert SECRET not in caplog.text and GOOD not in caplog.text


def test_action_mismatch_rejected(env, cf):
    """A token issued on the registration page cannot be used to log in."""
    add_user(env)
    cf["action"] = "register"
    assert code(login(env, GOOD)) == "captcha_failed"
    cf["action"] = "login"
    assert login(env, GOOD).status_code == 200
    cf["action"] = ""                   # no action at all — rejected too
    assert code(login(env, GOOD)) == "captcha_failed"


def test_forgot_uses_password_reset_action(env, cf):
    add_user(env)
    cf["action"] = "login"
    assert code(forgot(env, GOOD)) == "captcha_failed"
    cf["action"] = "password_reset"
    assert forgot(env, GOOD).status_code == 200


@pytest.mark.parametrize("score,ok", [(0.9, True), (0.5, True), (0.4, False), (0.0, False)])
def test_score_threshold(env, cf, score, ok):
    add_user(env)
    cf["score"] = score
    r = login(env, GOOD)
    if ok:
        assert r.status_code == 200
    else:
        assert r.status_code == 400 and code(r) == "captcha_low_score"
        assert "shubhali" in r.json()["error"]["message"]
        assert env.client.get("/portal/auth/me").json()["user"] is None


def test_score_threshold_is_configurable(env, cf):
    add_user(env)
    cf["score"] = 0.6
    env.db.set_setting("captcha_min_score", "0.7")
    assert code(login(env, GOOD)) == "captcha_low_score"
    env.db.set_setting("captcha_min_score", "0.3")
    assert login(env, GOOD).status_code == 200


def test_missing_score_means_misconfigured_key(env, cf):
    """A v2 key entered by mistake returns no score — not the user's fault, so 503."""
    add_user(env)
    cf["score"] = None
    assert code(login(env, GOOD)) == "captcha_unavailable"


def test_foreign_hostname_rejected(env, cf):
    add_user(env)
    cf["hostname"] = "evil.example"
    assert code(login(env, GOOD)) == "captcha_failed"
    cf["hostname"] = "api.example.com"
    assert login(env, GOOD).status_code == 200


def test_hostname_list_configurable(env, cf):
    add_user(env)
    cf["hostname"] = "localhost"
    assert code(login(env, GOOD)) == "captcha_failed"
    env.db.set_setting("captcha_hostnames", "example.com,localhost")
    assert login(env, GOOD).status_code == 200
    env.db.set_setting("captcha_hostnames", "")      # empty — not checked
    cf["hostname"] = "boshqa.uz"
    assert login(env, GOOD).status_code == 200


def test_rate_limit_checked_before_captcha(env, cf):
    """A blocked IP cannot make us send requests to Google."""
    add_user(env)
    for _ in range(8):
        env.app.state.auth_limiter.hit(("email", "a@test.uz"))
    assert login(env, GOOD).status_code == 429
    assert cf["calls"] == []


# --- login: after N failures ----------------------------------------------------

def test_login_after_fails_mode(env, cf):
    env.db.set_setting("captcha_login_after_fails", "3")
    add_user(env)
    assert env.client.get("/portal/auth/providers").json()["captcha"]["login_after_fails"] == 3
    # No captcha at first
    for _ in range(3):
        assert code(login(env, password="notogri")) == "invalid_credentials"
    assert cf["calls"] == []
    # After 3 failures it is required, even with the correct password
    assert code(login(env)) == "captcha_required"
    assert login(env, GOOD).status_code == 200


def test_login_after_fails_counts_per_email_across_ips(env, cf):
    """Distributed attack: different IPs, same email — captcha is still required."""
    env.db.set_setting("captcha_login_after_fails", "2")
    add_user(env)
    for _ in range(2):
        env.app.state.auth_limiter.hit(("email", "a@test.uz"))
    assert code(login(env)) == "captcha_required"


# --- registration ---------------------------------------------------------------

def test_register_requires_captcha_even_in_after_fails_mode(env, cf):
    env.db.set_setting("captcha_login_after_fails", "3")
    r = register(env)
    assert r.status_code == 400 and code(r) == "captcha_required"
    assert env.db.scalar("SELECT COUNT(*) FROM users") == 0
    assert code(register(env, "soxta")) == "captcha_failed"
    assert register(env, GOOD).status_code == 200
    assert env.db.scalar("SELECT COUNT(*) FROM users") == 1


def test_register_provider_down(env, cf):
    cf["mode"] = "timeout"
    assert code(register(env, GOOD)) == "captcha_unavailable"
    assert env.db.scalar("SELECT COUNT(*) FROM users") == 0


def test_closed_registration_does_not_call_provider(env, cf):
    r = env.client.post("/portal/auth/register", headers=CSRF,
                        json={"email": "x@test.uz", "password": "parol-12345", "captcha_token": GOOD})
    assert code(r) == "registration_closed"
    assert cf["calls"] == []


# --- password reset (via the Telegram bot) --------------------------------------

def test_forgot_requires_captcha(env, cf):
    add_user(env)
    r = forgot(env)
    assert r.status_code == 400 and code(r) == "captcha_required"
    assert env.db.scalar("SELECT COUNT(*) FROM password_resets") == 0
    assert code(forgot(env, "soxta")) == "captcha_failed"
    cf["mode"] = "timeout"
    assert code(forgot(env, GOOD)) == "captcha_unavailable"
    assert env.db.scalar("SELECT COUNT(*) FROM password_resets") == 0


def test_forgot_with_valid_captcha_same_answer(env, cf):
    """After the captcha the response is identical either way: it does not reveal whether the email exists."""
    add_user(env)
    a = forgot(env, GOOD)
    b = forgot(env, GOOD, email="yoq@test.uz")
    assert a.status_code == b.status_code == 200 and a.json() == b.json()


def test_reset_apply_and_oauth_not_gated(env, cf):
    """The token link and the Google/Telegram flows keep working without captcha."""
    r = env.client.post("/portal/auth/password/reset", headers=CSRF,
                        json={"token": "x" * 20, "password": "yangi-parol-1"})
    assert code(r) == "invalid_token"
    r = env.client.get("/portal/auth/telegram/callback", params={"id": "1"}, follow_redirects=False)
    assert r.status_code == 303 and "captcha" not in r.headers["location"]
    assert cf["calls"] == []


# --- admin settings -------------------------------------------------------------

def _admin(env):
    add_user(env, "admin@test.uz", role="admin")
    env.db.set_setting("captcha_enabled", "0")      # let the admin log in first
    assert login(env, email="admin@test.uz").status_code == 200


def test_admin_settings_never_return_secret(env, cf):
    _admin(env)
    s = env.client.get("/portal/admin/settings").json()
    assert s["recaptcha_secret_set"] is True and s["recaptcha_site_key"] == SITE_KEY
    assert "recaptcha_secret" not in s and SECRET not in str(s)
    assert s["captcha_min_score"] == 0.5 and s["captcha_hostnames"] == "example.com,api.example.com"  # set by fixture
    assert s["captcha_active"] is False


def test_admin_can_set_and_clear_secret(env, cf):
    _admin(env)
    r = env.client.put("/portal/admin/settings", headers=CSRF,
                       json={"recaptcha_secret": "", "captcha_enabled": True, "captcha_login_after_fails": 2,
                             "captcha_min_score": 0.7, "captcha_hostnames": " Example.com , localhost ,"})
    s = r.json()
    assert s["recaptcha_secret_set"] is True        # an empty field does not clear the secret
    assert s["captcha_active"] is True and s["captcha_login_after_fails"] == 2
    assert s["captcha_min_score"] == 0.7 and s["captcha_hostnames"] == "example.com,localhost"
    assert env.client.put("/portal/admin/settings", headers=CSRF,
                          json={"captcha_min_score": 1.5}).status_code == 422
    s = env.client.put("/portal/admin/settings", headers=CSRF, json={"clear_recaptcha_secret": True}).json()
    assert s["recaptcha_secret_set"] is False and s["captcha_active"] is False
    assert env.client.put("/portal/admin/settings", headers=CSRF,
                          json={"captcha_login_after_fails": 8}).status_code == 422
