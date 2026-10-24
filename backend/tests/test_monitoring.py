import asyncio
import json

import httpx

from app import monitor
from app.failures import classify
from .conftest import CHAT, CSRF, SOM, add_key, add_user, bearer


def login_admin(env):
    add_user(env, "admin@test.uz", role="admin")
    env.client.post("/portal/auth/login", json={"email": "admin@test.uz", "password": "parol-12345"}, headers=CSRF)


def use_transport(env, handler):
    env.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))


def probe(env):
    return asyncio.run(monitor.probe(env.app.state))


# --- probe states ---------------------------------------------------------------------------------------------------
def test_probe_distinguishes_tunnel_from_ollama(env):
    assert probe(env)[0] == "up"  # the mock Ollama answers

    def refused(request):
        raise httpx.ConnectError("refused")
    use_transport(env, refused)
    assert probe(env)[0] == "tunnel_down"

    def closed(request):
        raise httpx.ReadError("closed by peer")
    use_transport(env, closed)
    assert probe(env)[0] == "ollama_down"

    use_transport(env, lambda request: httpx.Response(500))
    state, _, err = probe(env)
    assert state == "ollama_down" and "500" in err

    def slow(request):
        raise httpx.ReadTimeout("slow")
    use_transport(env, slow)
    assert probe(env)[0] == "ollama_down"


# --- incident lifecycle -------------------------------------------------------------------------------------------
def test_incident_opens_after_two_failures_and_closes_on_recovery(env):
    rec = lambda s, ts: monitor.record(env.app, s, 10, None, ts)  # noqa: E731
    assert rec("up", 1000) == []
    assert rec("tunnel_down", 1030) == []                      # a single failure is not an incident yet
    ev = rec("tunnel_down", 1060)
    assert [e["type"] for e in ev] == ["down"]
    inc = env.db.one("SELECT * FROM incidents")
    assert inc["started_at"] == 1030 and inc["ended_at"] is None and inc["reason"] == "tunnel_down"
    assert rec("tunnel_down", 1090) == []                      # not announced a second time
    assert rec("ollama_down", 1120) == []                      # the reason changed, not a new incident
    assert env.db.one("SELECT reason FROM incidents")["reason"] == "ollama_down"
    ev = rec("up", 1150)
    assert [e["type"] for e in ev] == ["recovered"]
    inc = env.db.one("SELECT * FROM incidents")
    assert inc["ended_at"] == 1150 and env.db.scalar("SELECT COUNT(*) FROM incidents") == 1
    assert env.app.state.health["since"] == 1150


def test_single_blip_does_not_create_incident(env):
    monitor.record(env.app, "up", 10, None, 1000)
    monitor.record(env.app, "ollama_down", None, "x", 1030)
    monitor.record(env.app, "up", 10, None, 1060)
    assert env.db.scalar("SELECT COUNT(*) FROM incidents") == 0


def test_state_restored_after_restart(env):
    for ts, s in [(1000, "up"), (1030, "tunnel_down"), (1060, "tunnel_down")]:
        monitor.record(env.app, s, None, None, ts)
    env.app.state.health.update(state="unknown", ts=0, since=0)
    monitor.load_state(env.app)
    assert env.app.state.health["state"] == "tunnel_down" and env.app.state.health["since"] == 1030


def test_uptime_and_timeline(env, monkeypatch):
    # Keep all samples in today's bucket, including when tests run just after midnight.
    t = monitor.day_start() + 12 * 3600
    monkeypatch.setattr(monitor, "now", lambda: t)
    for i in range(10):
        monitor.record(env.app, "up" if i < 8 else "tunnel_down", 5, None, t - 600 + i * 30)
    assert monitor.uptime_pct(env.db, t - 3600) == 80.0
    tl = monitor.timeline(env.db, 1, 10)
    assert {b["s"] for b in tl} <= {"up", "tunnel_down", "none"} and any(b["s"] == "tunnel_down" for b in tl)
    assert monitor.daily_uptime(env.db, 3)[-1]["uptime"] == 80.0


# --- classification and reports -----------------------------------------------------------------------------------
def test_classify():
    assert classify(200, None) is None and classify(200, "client_disconnected") is None
    assert classify(503, "upstream_unavailable") == "platform" and classify(502, "upstream_500") == "platform"
    assert classify(402, "insufficient_balance") == "billing" and classify(429, "rate_limit_exceeded") == "limits"
    assert classify(404, "model_not_found") == "client" and classify(400, "upstream_400") == "client"
    assert classify(499, "cancelled") == "cancelled"


def test_failed_and_rejected_requests_are_recorded_with_category(env):
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid, rpm=3)
    use_transport(env, lambda r: (_ for _ in ()).throw(httpx.ConnectError("x")))
    assert env.client.post("/v1/chat/completions", json=CHAT, headers=bearer(key)).status_code == 503          # platform
    assert env.client.post("/v1/chat/completions", json={**CHAT, "model": "x"}, headers=bearer(key)).status_code == 404  # client
    for _ in range(6):
        env.client.post("/v1/chat/completions", json={**CHAT, "model": "x"}, headers=bearer(key))              # rpm exceeded
    cats = {r["category"]: r["n"] for r in env.db.all("SELECT category, COUNT(*) n FROM usage_logs GROUP BY category")}
    assert cats["platform"] == 1 and cats["client"] == 2 and cats["limits"] == 1   # repeated 429s are recorded once
    assert env.db.scalar("SELECT COUNT(*) FROM ledger WHERE kind='usage'") == 0


def test_admin_monitoring_and_failures_endpoints(env):
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    login_admin(env)
    t = monitor.now()
    monitor.record(env.app, "tunnel_down", None, "x", t - 120)
    monitor.record(env.app, "tunnel_down", None, "x", t - 90)       # the incident opens, started_at = t-120
    use_transport(env, lambda r: (_ for _ in ()).throw(httpx.ConnectError("x")))
    for _ in range(2):
        env.client.post("/v1/chat/completions", json=CHAT, headers=bearer(key))   # 2 failed requests
    env.app.state.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=__import__("tests.conftest", fromlist=["x"]).make_upstream(env.up)), base_url="http://upstream")
    env.client.post("/v1/chat/completions", json=CHAT, headers=bearer(key))       # 1 successful
    r = env.client.get("/portal/admin/monitoring?days=7").json()
    inc = r["incidents"][0]
    assert inc["ongoing"] and inc["reason"] == "tunnel_down" and inc["failed_requests"] == 2 and inc["affected_users"] == 1
    q = r["quality"]
    assert q["requests"] == 3 and q["succeeded"] == 1 and q["failed"] == 2
    assert q["reliability"] == round(100 * 1 / 3, 2) and q["latency_p50"] is not None
    assert next(c for c in q["by_category"] if c["category"] == "platform")["count"] == 2
    f = env.client.get("/portal/admin/failures?category=platform").json()
    assert f["total"] == 2 and f["items"][0]["reason"] == "Model serveri ulanmagan" and f["items"][0]["email"] == "a@test.uz"
    assert next(c for c in f["counts"] if c["category"] == "platform")["count"] == 2
    assert env.client.get("/portal/admin/failures?category=billing").json()["total"] == 0


def test_public_status_history(env):
    env.db.set_setting("status_public_since", "")   # no start date: the full 30 days
    t = monitor.now()
    monitor.record(env.app, "up", 5, None, t - 100)
    r = env.client.get("/portal/status/history").json()
    assert len(r["daily"]) == 30 and r["uptime"]["h24"] == 100.0 and r["incidents"] == []
    s = env.client.get("/portal/status").json()
    assert s["model_online"] is True and s["label"] == "Ishlayapti"


def test_csv_export_is_safe_and_admin_only(env):
    uid = add_user(env, balance_som=1000)
    key, key_id = add_key(env, uid, name='=HYPERLINK("http://evil")')
    env.client.post("/v1/chat/completions", json=CHAT, headers=bearer(key))
    assert env.client.get("/portal/admin/reports/usage.csv").status_code == 401
    login_admin(env)
    r = env.client.get("/portal/admin/reports/usage.csv?days=7")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    text = r.content.decode("utf-8-sig")
    assert "So'rov ID" in text and "'=HYPERLINK" in text and ",=HYPERLINK" not in text
    assert env.client.get("/portal/admin/reports/usage.csv?only_failed=true").content.decode("utf-8-sig").count("\n") == 1  # header row only


# --- Telegram alerts ---------------------------------------------------------------------------------------------
def capture_notifications(env, status=200):
    sent = []

    def handler(request):
        sent.append((str(request.url), json.loads(request.content)))
        return httpx.Response(status, json={"ok": status == 200})
    env.app.state.notify_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return sent


def test_alert_settings_never_expose_token(env):
    login_admin(env)
    r = env.client.put("/portal/admin/settings", json={"alert_telegram_token": "123:SECRET", "alert_telegram_chat_id": "-100500"}, headers=CSRF).json()
    assert r["alert_telegram_token_set"] is True and r["alert_telegram_chat_id"] == "-100500" and "SECRET" not in json.dumps(r)
    assert "SECRET" not in env.client.get("/portal/admin/settings").text
    # saving another setting does not clear the token, neither does an empty field
    env.client.put("/portal/admin/settings", json={"default_rpm": 40, "alert_telegram_token": ""}, headers=CSRF)
    assert env.db.get_settings()["alert_telegram_token"] == "123:SECRET"
    env.client.put("/portal/admin/settings", json={"clear_alert_token": True}, headers=CSRF)
    assert env.db.get_settings()["alert_telegram_token"] == ""


def test_incident_alerts_and_test_message(env):
    login_admin(env)
    sent = capture_notifications(env)
    assert env.client.post("/portal/admin/alerts/test", headers=CSRF).status_code == 400   # not configured
    env.client.put("/portal/admin/settings", json={"alert_telegram_token": "123:SECRET", "alert_telegram_chat_id": "42"}, headers=CSRF)
    assert env.client.post("/portal/admin/alerts/test", headers=CSRF).status_code == 200
    assert sent[-1][0] == "https://api.telegram.org/bot123:SECRET/sendMessage" and sent[-1][1]["chat_id"] == "42"

    monitor.record(env.app, "tunnel_down", None, "x", 1000)
    events = monitor.record(env.app, "tunnel_down", None, "x", 1030)
    asyncio.run(monitor.announce(env.app, events[0]))
    assert "Model ulanmagan" in sent[-1][1]["text"] and "Kompyuter yoki internet" in sent[-1][1]["text"]
    events = monitor.record(env.app, "up", 5, None, 1930)
    asyncio.run(monitor.announce(env.app, events[0]))
    assert "Model qayta ishladi" in sent[-1][1]["text"] and "15 daqiqa" in sent[-1][1]["text"]


def test_failed_telegram_does_not_leak_token_or_break(env):
    login_admin(env)
    env.client.put("/portal/admin/settings", json={"alert_telegram_token": "123:SECRET", "alert_telegram_chat_id": "42"}, headers=CSRF)
    capture_notifications(env, status=401)
    r = env.client.post("/portal/admin/alerts/test", headers=CSRF)
    assert r.status_code == 400 and "SECRET" not in r.text


def test_daily_report_text_and_once_per_day(env):
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    env.client.post("/v1/chat/completions", json=CHAT, headers=bearer(key))
    text = monitor.build_daily_report(env.db, monitor.day_start())     # today: there is a request
    assert "kunlik hisobot" in text and "So'rovlar: 1, muvaffaqiyatli: 1" in text and "uzilishlar: 0" in text


def test_public_status_counts_from_launch_date(env):
    """Incidents from before the launch date are not shown on the public status page."""
    from datetime import datetime
    from app.billing import TASHKENT
    from app.db import now
    t = now()
    launch = datetime.fromtimestamp(t - 2 * 86400, TASHKENT).strftime("%Y-%m-%d")
    env.db.set_setting("status_public_since", launch)
    with env.db.tx() as c:
        c.execute("INSERT INTO health_checks(ts, state) VALUES (?, 'down')", (t - 10 * 86400,))
        c.execute("INSERT INTO health_checks(ts, state) VALUES (?, 'up')", (t - 3600,))
        c.execute("INSERT INTO incidents(started_at, ended_at, reason) VALUES (?,?, 'down')", (t - 10 * 86400, t - 10 * 86400 + 60))
    d = env.client.get("/portal/status/history").json()
    assert d["since_date"] == launch
    assert d["uptime"]["d30"] == 100.0 and d["incidents"] == []
    assert len(d["daily"]) == 3
