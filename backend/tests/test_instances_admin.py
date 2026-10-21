"""Managing cluster browser instances from the admin panel, and VNC.

Key guarantees:
  * a non-admin user cannot reach any endpoint;
  * chat cleanup takes ONLY chats from the gateway's registry — it never touches
    the account owner's own chats (not in the registry, old, pinned);
  * manual cleanup is refused for adapters without a cleanup policy;
  * the VNC password is never sent to the browser, tickets are single-use;
  * every action is written to the audit log, and the app cannot delete it.
"""
import asyncio
import json
import threading
import time
import types
from contextlib import asynccontextmanager

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from app import vnc
from cluster import instances as inst
from cluster import server as srv
from cluster import settings as ccfg
from cluster.adapters import chatgpt_adapter as cga
from cluster.adapters.chatgpt_adapter import ChatGPTAdapter
from cluster.adapters.deepseek_adapter import DeepSeekAdapter
from cluster.adapters.gemini_adapter import GeminiAdapter
from cluster.adapters.grok_adapter import GrokAdapter
from cluster.adapters.qwen_adapter import QwenAdapter
from cluster.pool import ClusterPool, WorkerTab

from .conftest import CSRF, add_user

VNC_PW = "vnc-pw-7"


# --- backend: helpers -----------------------------------------------------------

def login(env, email):
    r = env.client.post("/portal/auth/login", json={"email": email, "password": "parol-12345"}, headers=CSRF)
    assert r.status_code == 200, r.text


def as_admin(env):
    add_user(env, "admin@test.uz", role="admin")
    login(env, "admin@test.uz")


def fake_cluster(env, handler):
    env.db.set_setting("cluster_base_url", "http://cluster")
    env.db.set_setting("cluster_api_key", "cl-key")
    calls = []

    def wrap(request: httpx.Request):
        calls.append((request.method, request.url.path, request.content, request.headers.get("authorization")))
        return handler(request)
    env.app.state.cluster_http = httpx.AsyncClient(transport=httpx.MockTransport(wrap))
    return calls


def ok_cluster(request: httpx.Request):
    path = request.url.path
    if path == "/cluster/instances":
        return httpx.Response(200, json={"ts": 1, "instances": [{"instance": 0, "port": 9222, "state": "ok"}]})
    if path.endswith("/cleanup"):
        dry = json.loads(request.content or b"{}").get("dry_run", True)
        return httpx.Response(200, json={"ok": True, "dry_run": dry,
                                         "summary": "quruq sinov: 2 ta" if dry else "2 ta chat o'chirildi"})
    return httpx.Response(200, json={"ok": True, "summary": "sessiya joyida"})


def audit_rows(env):
    rows = env.db.all("SELECT admin_email, action, target, ok, detail FROM admin_audit ORDER BY id")
    return [{**dict(r), "ok": bool(r["ok"])} for r in rows]


def wait_for_audit(env, action, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline and not any(x["action"] == action for x in audit_rows(env)):
        time.sleep(0.05)


def with_vnc(env, password=VNC_PW):
    env.app.state.config = types.SimpleNamespace(**{**env.cfg.__dict__, "vnc_password": password,
                                                    "vnc_host": "127.0.0.1", "vnc_port": 1})


ENDPOINTS = [
    ("GET", "/portal/admin/instances"),
    ("GET", "/portal/admin/instances/audit"),
    ("POST", "/portal/admin/instances/0/probe"),
    ("POST", "/portal/admin/instances/0/reload"),
    ("POST", "/portal/admin/instances/0/cleanup"),
    ("POST", "/portal/admin/instances/0/vnc-ticket"),
]


# --- backend: permissions -------------------------------------------------------

def test_non_admin_cannot_reach_instance_endpoints(env):
    calls = fake_cluster(env, ok_cluster)
    with_vnc(env)
    for method, path in ENDPOINTS:            # not logged in
        assert env.client.request(method, path, headers=CSRF, json={}).status_code == 401, path
    add_user(env, "a@test.uz")
    login(env, "a@test.uz")
    for method, path in ENDPOINTS:            # regular user
        assert env.client.request(method, path, headers=CSRF, json={}).status_code == 403, path
    assert calls == []                        # nothing reached the cluster
    assert audit_rows(env) == []


def test_mutations_need_csrf_header(env):
    calls = fake_cluster(env, ok_cluster)
    as_admin(env)
    assert env.client.post("/portal/admin/instances/0/reload", json={}).status_code == 403
    assert calls == []


def test_vnc_websocket_rejects_non_admin_and_foreign_origin(env):
    with_vnc(env)
    as_admin(env)
    good = env.client.post("/portal/admin/instances/0/vnc-ticket", headers=CSRF)
    # (no cluster configured — the window is not focused, but a ticket is issued)
    assert good.status_code == 200
    ticket = good.json()["ticket"]
    # from another site (CSWSH)
    with pytest.raises(Exception):
        with env.client.websocket_connect(f"/portal/admin/vnc/ws?ticket={ticket}",
                                          headers={"origin": "https://evil.example"}) as ws:
            ws.receive_bytes()
    # with a regular user's session
    env.client.cookies.clear()
    add_user(env, "a@test.uz")
    login(env, "a@test.uz")
    with pytest.raises(Exception):
        with env.client.websocket_connect(f"/portal/admin/vnc/ws?ticket={ticket}",
                                          headers={"origin": "http://testserver"}) as ws:
            ws.receive_bytes()


# --- backend: actions and audit -------------------------------------------------

def test_list_and_probe_reload_are_audited(env):
    calls = fake_cluster(env, ok_cluster)
    as_admin(env)
    r = env.client.get("/portal/admin/instances")
    assert r.status_code == 200 and r.json()["instances"][0]["port"] == 9222
    assert r.json()["vnc_enabled"] is False
    assert env.client.post("/portal/admin/instances/0/probe", headers=CSRF).json()["ok"] is True
    assert env.client.post("/portal/admin/instances/5/reload", headers=CSRF).status_code == 200
    assert [c[1] for c in calls][-2:] == ["/cluster/instances/0/probe", "/cluster/instances/5/reload"]
    assert all(c[3] == "Bearer cl-key" for c in calls)
    rows = audit_rows(env)
    assert [(x["action"], x["target"], x["ok"]) for x in rows] == [("probe", "instance:0", True),
                                                                   ("reload", "instance:5", True)]
    assert rows[0]["admin_email"] == "admin@test.uz" and rows[0]["detail"] == "sessiya joyida"
    assert len(env.client.get("/portal/admin/instances/audit").json()["items"]) == 2


def test_cluster_errors_are_mapped_and_audited(env):
    def busy(request):
        return httpx.Response(409, json={"error": {"code": "busy", "message": "Instansiya hozir so'rov bajaryapti"}})
    fake_cluster(env, busy)
    as_admin(env)
    r = env.client.post("/portal/admin/instances/1/reload", headers=CSRF)
    assert r.status_code == 409 and r.json()["error"]["code"] == "busy"

    def boom(request):
        raise httpx.ConnectError("down")
    fake_cluster(env, boom)
    r = env.client.post("/portal/admin/instances/1/probe", headers=CSRF)
    assert r.status_code == 502 and r.json()["error"]["code"] == "cluster_unreachable"

    fake_cluster(env, lambda req: httpx.Response(401, json={}))
    assert env.client.post("/portal/admin/instances/1/probe", headers=CSRF).json()["error"]["code"] == "cluster_bad_key"

    rows = audit_rows(env)
    assert [(x["action"], x["ok"]) for x in rows] == [("reload", False), ("probe", False), ("probe", False)]
    assert rows[0]["detail"].startswith("busy:")


def test_bad_instance_number_is_rejected(env):
    calls = fake_cluster(env, ok_cluster)
    as_admin(env)
    assert env.client.post("/portal/admin/instances/-1/probe", headers=CSRF).status_code == 422
    assert env.client.post("/portal/admin/instances/999/probe", headers=CSRF).status_code == 422
    assert calls == []


def test_cleanup_dry_run_is_sync_and_real_run_is_background_audited(env):
    calls = fake_cluster(env, ok_cluster)
    as_admin(env)
    r = env.client.post("/portal/admin/instances/0/cleanup", headers=CSRF, json={})
    assert r.json()["dry_run"] is True                         # default — dry run
    r = env.client.post("/portal/admin/instances/0/cleanup", headers=CSRF, json={"dry_run": False})
    assert r.status_code == 200 and r.json()["started"] is True
    for _ in range(100):
        if len(audit_rows(env)) == 2:
            break
        time.sleep(0.05)
    rows = audit_rows(env)
    assert [(x["action"], x["ok"], x["detail"]) for x in rows] == [
        ("cleanup_dry", True, "quruq sinov: 2 ta"), ("cleanup", True, "2 ta chat o'chirildi")]
    assert [json.loads(c[2])["dry_run"] for c in calls] == [True, False]


def test_audit_log_is_append_only_for_app_role(env):
    fake_cluster(env, ok_cluster)
    as_admin(env)
    env.client.post("/portal/admin/instances/0/probe", headers=CSRF)
    with psycopg.connect(env.cfg.database_url) as c:
        for stmt in ("DELETE FROM admin_audit", "UPDATE admin_audit SET ok=0"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute(stmt)
            c.rollback()


# --- backend: VNC ----------------------------------------------------------------

def test_vnc_disabled_without_password(env):
    as_admin(env)
    r = env.client.post("/portal/admin/instances/0/vnc-ticket", headers=CSRF)
    assert r.status_code == 503 and r.json()["error"]["code"] == "vnc_disabled"


def test_des_matches_fips_vector():
    key, block = bytes.fromhex("133457799BBCDFF1"), bytes.fromhex("0123456789ABCDEF")
    assert vnc.des_encrypt_block(key, block).hex().upper() == "85E813540F0AB405"


def test_vnc_auth_response_matches_novnc():
    # Expected values come from noVNC 1.4.0 `core/des.js` (this is how the browser client computes them)
    ch = bytes(i * 7 % 256 for i in range(16))
    assert vnc.vnc_auth_response("vnc-pw-7", ch).hex() == "b0e909d70481b21220cb4a6296a1dcb6"
    assert vnc.vnc_auth_response("abc", ch).hex() == "6572217efb16662835bdee94bd94b74c"
    assert vnc.vnc_auth_response("longerthan8chars", ch).hex() == "4c911f2f0e8adfc13644cf8b2fac94b6"


def test_ticket_is_single_use_user_bound_and_expires(monkeypatch):
    s = vnc.TicketStore()
    t = s.issue(7, 0)
    assert s.consume(t, 8) is None              # another user — and now it is invalid
    assert s.consume(t, 7) is None
    t = s.issue(7, 3)
    assert s.consume(t, 7).instance == 3
    assert s.consume(t, 7) is None              # single use
    t = s.issue(7, 0)
    monkeypatch.setattr(vnc.time, "time", lambda: 10 ** 12)
    assert s.consume(t, 7) is None              # expired


class FakeVncServer:
    """Mimics x11vnc: requires VNC Authentication and checks the response with real DES."""

    CHALLENGE = bytes(range(16))

    def __init__(self, password):
        self.password, self.authed, self.got = password, False, b""
        self.loop = asyncio.new_event_loop()
        ready = threading.Event()

        async def start():
            self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
            self.port = self.server.sockets[0].getsockname()[1]
            ready.set()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        asyncio.run_coroutine_threadsafe(start(), self.loop)
        ready.wait(5)

    async def handle(self, reader, writer):
        writer.write(b"RFB 003.008\n")
        await reader.readexactly(12)
        writer.write(b"\x01\x02")                       # VNC Authentication only
        assert (await reader.readexactly(1)) == b"\x02"
        writer.write(self.CHALLENGE)
        resp = await reader.readexactly(16)
        self.authed = resp == vnc.vnc_auth_response(self.password, self.CHALLENGE)
        writer.write(b"\x00\x00\x00\x00" if self.authed else b"\x00\x00\x00\x01")
        await writer.drain()
        if not self.authed:
            writer.close()
            return
        self.got = await reader.readexactly(1)          # ClientInit
        writer.write(b"SERVERINIT")
        await writer.drain()
        await reader.read(10)

    def stop(self):
        self.loop.call_soon_threadsafe(self.server.close)
        self.loop.call_soon_threadsafe(self.loop.stop)


def test_vnc_proxy_end_to_end_password_stays_on_server(env):
    srv_ = FakeVncServer(VNC_PW)
    try:
        with_vnc(env)
        env.app.state.config.vnc_port = srv_.port
        fake_cluster(env, ok_cluster)
        as_admin(env)
        r = env.client.post("/portal/admin/instances/2/vnc-ticket", headers=CSRF)
        assert r.status_code == 200 and VNC_PW not in r.text
        ticket = r.json()["ticket"]
        seen = b""
        with env.client.websocket_connect(f"/portal/admin/vnc/ws?ticket={ticket}", subprotocols=["binary"],
                                          headers={"origin": "http://testserver"}) as ws:
            assert ws.accepted_subprotocol == "binary"
            seen += ws.receive_bytes()
            ws.send_bytes(b"RFB 003.008\n")
            seen += ws.receive_bytes()
            assert seen.endswith(b"\x01\x01")                      # the browser only sees "None"
            ws.send_bytes(b"\x01")
            seen += ws.receive_bytes()
            ws.send_bytes(b"\x01")                                 # ClientInit (shared)
            seen += ws.receive_bytes()
            # The browser disconnected: wait for the session to end (TestClient cancels the task on exit)
            ws.close(1000)
            wait_for_audit(env, "vnc_session")
        assert seen.endswith(b"\x00\x00\x00\x00SERVERINIT")
        assert srv_.authed and srv_.got == b"\x01"
        assert VNC_PW.encode() not in seen
        # the ticket does not work a second time
        with pytest.raises(Exception):
            with env.client.websocket_connect(f"/portal/admin/vnc/ws?ticket={ticket}",
                                              headers={"origin": "http://testserver"}) as ws:
                ws.receive_bytes()
        acts = [(x["action"], x["target"], x["ok"]) for x in audit_rows(env)]
        assert ("vnc_open", "instance:2", True) in acts and ("vnc_session", "instance:2", True) in acts
        assert all(VNC_PW not in x["detail"] for x in audit_rows(env))
    finally:
        srv_.stop()


def test_vnc_wrong_password_is_reported_not_leaked(env):
    srv_ = FakeVncServer("boshqa-parol")
    try:
        with_vnc(env)
        env.app.state.config.vnc_port = srv_.port
        as_admin(env)
        ticket = env.client.post("/portal/admin/instances/0/vnc-ticket", headers=CSRF).json()["ticket"]
        with pytest.raises(Exception):
            with env.client.websocket_connect(f"/portal/admin/vnc/ws?ticket={ticket}",
                                              headers={"origin": "http://testserver"}) as ws:
                for _ in range(4):
                    ws.receive_bytes()
        wait_for_audit(env, "vnc_session")
        row = [x for x in audit_rows(env) if x["action"] == "vnc_session"][0]
        assert row["ok"] is False and "autentifikatsiya" in row["detail"]
    finally:
        srv_.stop()


# --- cluster: state and actions --------------------------------------------------

class DummyWS:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def make_worker(port, adapter, model):
    w = WorkerTab(port, f"tab{port}", model, "ws://x", adapter)

    async def connect(open_timeout=10.0):
        return DummyWS()
    w.connect = connect
    return w


def make_pool(*workers, ports=(9222, 9223, 9224)):
    pool = ClusterPool(list(ports))
    pool.workers = list(workers)

    async def scan(force=False):
        return None
    pool.scan = scan
    return pool


@pytest.fixture
def cluster_app(monkeypatch):
    @asynccontextmanager
    async def nolife(_):
        yield
    monkeypatch.setattr(srv.app.router, "lifespan_context", nolife)
    monkeypatch.setattr(ccfg, "API_KEY", "")
    inst.LAST_ACTION.clear()
    inst._BUSY.clear()
    client = TestClient(srv.app)

    def use(pool):
        srv.app.state.pool = pool
        return client
    yield use
    inst.LAST_ACTION.clear()


def test_worker_state_classification():
    w = make_worker(9222, ChatGPTAdapter(), "chatgpt")
    assert inst.worker_state(w) == inst.STATE_OK
    w.apply_probe(False, "no_chat_input")
    assert inst.worker_state(w) == inst.STATE_CRASHED          # not a login issue — the page crashed
    w.apply_probe(True, "ok")
    assert inst.worker_state(w) == inst.STATE_OK and w.needs_attention is None and w.cooldown_until == 0
    w.apply_probe(False, "login_required")
    assert inst.worker_state(w) == inst.STATE_LOGIN
    w.apply_probe(True, "ok")
    w.cooldown_until = time.time() + 60
    assert inst.worker_state(w) == inst.STATE_RESTING


def test_snapshot_marks_down_and_missing_tabs(cluster_app):
    w = make_worker(9222, ChatGPTAdapter(), "chatgpt")
    pool = make_pool(w)
    pool.scan_errors = {9224: "URLError: refused"}
    data = cluster_app(pool).get("/cluster/instances").json()["instances"]
    by = {x["instance"]: x for x in data}
    assert by[0]["state"] == "ok" and by[0]["tabs"][0]["cleanup"]["supported"] is True
    assert by[1]["state"] == "no_tab"
    assert by[2]["state"] == "down" and "refused" in by[2]["last_error"]


def test_cluster_instance_endpoints_require_api_key(cluster_app, monkeypatch):
    client = cluster_app(make_pool())
    monkeypatch.setattr(ccfg, "API_KEY", "secret")
    for path in ("/cluster/instances/0/probe", "/cluster/instances/0/reload",
                 "/cluster/instances/0/cleanup", "/cluster/instances/0/focus"):
        assert client.post(path, json={}).status_code == 401
    assert client.get("/cluster/instances").status_code == 401


def test_probe_does_not_reload_page_and_unknown_instance_404(cluster_app):
    a = ChatGPTAdapter()
    seen = {}

    async def check_session(ws, timeout=5.0, recover=True):
        seen["recover"] = recover
        return False, "login_required"

    async def goto(ws, url):
        raise AssertionError("probe must not reload the page")
    a.check_session, a.goto = check_session, goto
    w = make_worker(9222, a, "chatgpt")
    client = cluster_app(make_pool(w))
    r = client.post("/cluster/instances/0/probe")
    assert r.status_code == 200 and r.json()["ok"] is False and seen["recover"] is False
    assert w.needs_attention == "login" and inst.LAST_ACTION[9222]["action"] == "probe"
    assert client.post("/cluster/instances/9/probe").status_code == 404


def test_reload_navigates_to_home_url_only(cluster_app):
    a = DeepSeekAdapter()
    urls = []

    async def goto(ws, url):
        urls.append(url)

    async def wait_for_input(ws, timeout=10.0, selector=None):
        return True

    async def check_session(ws, timeout=5.0, recover=True):
        return True, "ok"
    a.goto, a.wait_for_input, a.check_session = goto, wait_for_input, check_session
    client = cluster_app(make_pool(make_worker(9224, a, "deepseek")))
    r = client.post("/cluster/instances/2/reload")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert urls == ["https://chat.deepseek.com/"]


def test_busy_worker_returns_409(cluster_app, monkeypatch):
    w = make_worker(9222, ChatGPTAdapter(), "chatgpt")
    client = cluster_app(make_pool(w))
    orig = inst._Locked.__init__
    monkeypatch.setattr(inst._Locked, "__init__", lambda self, w, wait: orig(self, w, 0.05))

    async def hold():
        await w.lock.acquire()
    asyncio.run(hold())  # the lock is held by another request
    r = client.post("/cluster/instances/0/probe")
    assert r.status_code == 409 and r.json()["error"]["code"] == "busy"


def test_no_tab_and_browser_down_are_409(cluster_app):
    pool = make_pool()
    client = cluster_app(pool)
    assert client.post("/cluster/instances/0/reload").json()["error"]["code"] == "no_tab"
    pool.scan_errors = {9222: "refused"}
    assert client.post("/cluster/instances/0/reload").json()["error"]["code"] == "browser_down"


def test_cleanup_refused_for_adapter_without_policy(cluster_app):
    # Grok: the account holds the owner's own chats and has no cleanup policy — never clean
    a = GrokAdapter()

    async def never(*args, **kw):
        raise AssertionError("cleanup must not be called on an adapter without a policy")
    a.delete_old_chats = never
    client = cluster_app(make_pool(make_worker(9228, a, "grok"), ports=(9228,)))
    for dry in (True, False):
        r = client.post("/cluster/instances/6/cleanup", json={"dry_run": dry})
        assert r.status_code == 409 and r.json()["error"]["code"] == "cleanup_unsupported"


def test_policies_are_not_relaxed():
    assert ChatGPTAdapter.CLEANUP_POLICY == "registry"
    assert DeepSeekAdapter.CLEANUP_POLICY == "pin"
    assert QwenAdapter.CLEANUP_POLICY == "all"
    assert getattr(GrokAdapter, "CLEANUP_POLICY", None) is None and not GrokAdapter.caps.delete_chats


def test_deepseek_dry_run_lists_titles_and_never_pinned(cluster_app):
    a = DeepSeekAdapter()
    chats = [{"id": "p1", "title": "uArm robot (shaxsiy)", "protected": True},
             *[{"id": f"c{i}", "title": f"Sinov {i}", "protected": False} for i in range(5)]]

    async def list_chats(ws):
        return chats

    async def never(*args, **kw):
        raise AssertionError("a dry run must not delete")
    a.list_chats, a.delete_old_chats, a._delete_one = list_chats, never, never
    client = cluster_app(make_pool(make_worker(9224, a, "deepseek")))
    t = client.post("/cluster/instances/2/cleanup", json={"dry_run": True}).json()["tabs"][0]
    assert t["policy"] == "pin" and t["keep"] == ccfg.CHAT_CLEANUP_KEEP
    assert "uArm robot (shaxsiy)" in t["protect_titles"] and "uArm robot (shaxsiy)" not in t["delete_titles"]
    assert t["delete_titles"] == [f"Sinov {i}" for i in range(ccfg.CHAT_CLEANUP_KEEP, 5)]


class FakeQwenSidebar:
    """Mimics the measured Qwen UI: "Chat Menu" (DOM click) -> "Delete" -> confirm.

    `broken=True` — the UI changed: the menu has no "Delete" item.
    """

    def __init__(self, titles, broken=False):
        self.rows = [(f"id-{t}", t) for t in titles]
        self.broken = broken
        self.stage, self.target, self.deleted = None, None, []

    def _ids(self):
        return [i for i, _ in self.rows]

    @property
    def titles(self):
        return [t for _, t in self.rows]

    async def evaluate(self, ws, js, timeout=20.0, await_promise=False):
        if js == QwenAdapter._ROWS_JS:
            return [{"title": t, "active": n == 0} for n, (_, t) in enumerate(self.rows)]
        if js in (QwenAdapter._SCROLL_JS, "document.querySelectorAll('.chat-item-drag-link').length"):
            return len(self.rows)
        for n in range(len(self.rows) + 2):
            if js == QwenAdapter._row_id_js(n):
                return self.rows[n][0] if n < len(self.rows) else None
        for cid in self._ids():
            if js == QwenAdapter._open_menu_js(cid):
                self.stage, self.target = "menu", cid
                return True
            if js == QwenAdapter._has_id_js(cid):
                return True
        if "const want =" in js:                 # _has_id_js / _open_menu_js: unknown id
            return False
        if js == QwenAdapter._MENU_DELETE_JS:
            if self.stage != "menu" or self.broken:
                return False
            self.stage = "confirm"
            return True
        if js == QwenAdapter._CONFIRM_JS:
            if self.stage != "confirm":
                return False
            self.rows = [r for r in self.rows if r[0] != self.target]
            self.deleted.append(self.target[3:])
            self.stage = None
            return True
        if js == QwenAdapter._CANCEL_JS:
            self.stage = None
            return True
        raise AssertionError("unexpected JS: " + js[:80])

    async def key(self, *a, **kw):
        self.stage = None


@pytest.fixture
def qwen_dom(monkeypatch):
    from cluster.adapters import qwen_adapter as qa

    async def fast_sleep(_):
        return None
    monkeypatch.setattr(qa, "asyncio", types.SimpleNamespace(sleep=fast_sleep))

    def install(fake):
        monkeypatch.setattr(qa, "cdp", fake)
        return fake
    return install


def test_qwen_cleanup_deletes_all_oldest_first(qwen_dom, cluster_app):
    fake = qwen_dom(FakeQwenSidebar([f"chat {i}" for i in range(7)]))
    client = cluster_app(make_pool(make_worker(9223, QwenAdapter(), "qwen")))
    dry = client.post("/cluster/instances/1/cleanup", json={"dry_run": True}).json()["tabs"][0]
    assert dry["policy"] == "all" and dry["keep"] == 0 and dry["would_delete"] == 7
    assert dry["delete_titles"][0] == "chat 0" and fake.deleted == []
    r = client.post("/cluster/instances/1/cleanup", json={"dry_run": False}).json()
    assert r["tabs"][0]["deleted"] == 7 and fake.titles == []
    assert fake.deleted == [f"chat {i}" for i in reversed(range(7))]   # oldest first


def test_qwen_watchdog_keeps_newest_and_respects_batch(qwen_dom):
    fake = qwen_dom(FakeQwenSidebar([f"chat {i}" for i in range(10)]))
    n = asyncio.run(QwenAdapter().delete_old_chats(DummyWS(), keep=3, max_per_run=4))
    assert n == 4 and fake.titles == [f"chat {i}" for i in range(6)]
    n = asyncio.run(QwenAdapter().delete_old_chats(DummyWS(), keep=3))
    assert n == 3 and fake.titles == ["chat 0", "chat 1", "chat 2"]


def test_qwen_cleanup_stops_when_ui_changes(qwen_dom):
    fake = qwen_dom(FakeQwenSidebar(["a", "b", "c"], broken=True))
    assert asyncio.run(QwenAdapter().delete_old_chats(DummyWS(), keep=0)) == 0
    assert fake.titles == ["a", "b", "c"] and fake.stage is None


class FakeGeminiSidebar:
    """Mimics the measured Gemini UI: rows are visible with a wide viewport + open panel;
    "дополнительные параметры" (more options) -> "Удалить" (Delete) -> confirm. `broken=True` — no menu item."""

    def __init__(self, n, broken=False):
        self.rows = [f"/app/{i:04x}" for i in range(n)]
        self.broken = broken
        self.wide = self.panel = False
        self.stage, self.target, self.deleted = None, None, []

    async def call(self, ws, method, params=None, timeout=20.0):
        self.wide = method == "Emulation.setDeviceMetricsOverride"
        return {}

    async def evaluate(self, ws, js, timeout=20.0, await_promise=False):
        G = GeminiAdapter
        visible = self.wide and self.panel
        if js == G._COUNT_JS:
            return len(self.rows) if visible else 0
        if js == G._OPEN_PANEL_JS:
            self.panel = True
            return "opened"
        if js == G._CLOSE_PANEL_JS:
            self.panel = False
            return True
        if js == G._DRAWER_OPEN_JS:
            return self.panel and not self.wide
        if js == G._ROWS_JS:
            return [{"href": h, "title": h} for h in self.rows] if visible else []
        if js == G._SCROLL_JS:
            return len(self.rows) if visible else 0
        for h in self.rows:
            if js == G._open_menu_js(h):
                self.stage, self.target = "menu", h
                return True
            if js == G._has_href_js(h):
                return True
        if js.startswith("!!document.querySelector('[data-test-id=conversation] a[href="):
            return False
        if js == G._MENU_DELETE_JS:
            if self.stage != "menu" or self.broken:
                return False
            self.stage = "confirm"
            return True
        if js == G._CONFIRM_JS:
            if self.stage != "confirm":
                return False
            self.rows.remove(self.target)
            self.deleted.append(self.target)
            self.stage = None
            return True
        if js == G._CANCEL_JS:
            self.stage = None
            return True
        raise AssertionError("unexpected JS: " + js[:80])

    async def key(self, *a, **kw):
        self.stage = None


@pytest.fixture
def gemini_dom(monkeypatch):
    from cluster.adapters import gemini_adapter as ga

    async def fast_sleep(_):
        return None
    monkeypatch.setattr(ga, "asyncio", types.SimpleNamespace(sleep=fast_sleep))

    def install(fake):
        monkeypatch.setattr(ga, "cdp", fake)
        return fake
    return install


def test_gemini_cleanup_deletes_all_and_restores_view(gemini_dom, cluster_app):
    assert GeminiAdapter.caps.delete_chats and GeminiAdapter.CLEANUP_KEEP == 0
    fake = gemini_dom(FakeGeminiSidebar(5))
    client = cluster_app(make_pool(make_worker(9231, GeminiAdapter(), "gemini"), ports=(9231,)))
    dry = client.post("/cluster/instances/9/cleanup", json={"dry_run": True}).json()["tabs"][0]
    assert dry["policy"] == "all" and dry["keep"] == 0 and dry["would_delete"] == 5 and fake.deleted == []
    assert not fake.wide and not fake.panel                 # the view is restored after a dry run too
    r = client.post("/cluster/instances/9/cleanup", json={"dry_run": False}).json()
    assert r["tabs"][0]["deleted"] == 5 and fake.rows == []
    assert fake.deleted == [f"/app/{i:04x}" for i in reversed(range(5))]   # oldest first
    assert not fake.wide and not fake.panel                 # no drawer covering the page in the narrow window


def test_gemini_cleanup_respects_batch_and_stops_when_ui_changes(gemini_dom):
    fake = gemini_dom(FakeGeminiSidebar(6))
    assert asyncio.run(GeminiAdapter().delete_old_chats(DummyWS(), keep=0, max_per_run=4)) == 4
    assert len(fake.rows) == 2
    fake = gemini_dom(FakeGeminiSidebar(3, broken=True))
    assert asyncio.run(GeminiAdapter().delete_old_chats(DummyWS(), keep=0)) == 0
    assert len(fake.rows) == 3 and fake.stage is None and not fake.wide and not fake.panel


# --- cluster: ChatGPT cleanup only takes registry chats ---------------------------

def chat_id(age_seconds):
    return f"{int(time.time()) - age_seconds:08x}-1111-2222-3333-444455556666"


@pytest.fixture
def chatgpt_sidebar(monkeypatch, tmp_path):
    """Fake sidebar: the delete flow is mocked at the `cdp` level."""
    ledger = tmp_path / "owned.json"
    monkeypatch.setattr(cga, "_LEDGER_PATH", ledger)

    async def fast_sleep(_):
        return None
    monkeypatch.setattr(cga, "asyncio", types.SimpleNamespace(sleep=fast_sleep))

    state = {"rows": [], "target": None, "deleted": []}

    def row(cid, pinned=False, pin_known=True, actions=True):
        return {"id": cid, "title": "t", "pinned": pinned, "pinKnown": pin_known, "hasActions": actions}

    async def open_sidebar(self, ws, max_rounds=24):
        return True

    async def read_rows(self, ws):
        return [dict(r) for r in state["rows"]]

    async def hover_click(ws, locator):
        state["target"] = json.loads(locator.split("const want = ", 1)[1].split(";", 1)[0])
        return True

    async def click_text(ws, texts, timeout=10.0):
        if "Delete chat" in texts and state["target"]:
            state["deleted"].append(state["target"])
            state["rows"] = [r for r in state["rows"] if r["id"] != state["target"]]
            state["target"] = None
        return True

    async def key(*a, **kw):
        return None
    monkeypatch.setattr(ChatGPTAdapter, "_open_sidebar", open_sidebar)
    monkeypatch.setattr(ChatGPTAdapter, "_read_rows", read_rows)
    monkeypatch.setattr(cga.cdp, "hover_click", hover_click)
    monkeypatch.setattr(cga.cdp, "click_text", click_text)
    monkeypatch.setattr(cga.cdp, "key", key)
    state["row"] = row
    state["ledger"] = ledger
    return state


def test_chatgpt_cleanup_deletes_only_registry_chats(chatgpt_sidebar, cluster_app):
    s, row = chatgpt_sidebar, chatgpt_sidebar["row"]
    ours_new = [chat_id(600 + i) for i in range(3)]                   # created by the gateway, recent
    ours_old = chat_id(30 * 24 * 3600)                                # in the registry, but 30 days old
    ours_pinned = chat_id(900)                                        # in the registry, but pinned
    ours_unknown_pin = chat_id(950)                                   # in the registry, pin state unreadable
    owner_today = chat_id(300)                                        # created by the owner TODAY, not in the registry
    owner_old = "67b44942-aaaa-bbbb-cccc-ddddeeeeffff"                # the owner's chat from 2025
    s["rows"] = [row(owner_today), *[row(c) for c in ours_new], row(ours_pinned, pinned=True),
                 row(ours_unknown_pin, pin_known=False), row(ours_old), row(owner_old)]
    s["ledger"].write_text(json.dumps({c: int(time.time()) for c in
                                       [*ours_new, ours_old, ours_pinned, ours_unknown_pin]}))

    client = cluster_app(make_pool(make_worker(9222, ChatGPTAdapter(), "chatgpt")))
    dry = client.post("/cluster/instances/0/cleanup", json={"dry_run": True}).json()
    # keep=3: all three recent chats are kept; a dry run deletes nothing
    assert dry["tabs"][0]["would_delete"] == 0 and dry["tabs"][0]["registry_size"] == 6 and s["deleted"] == []

    # a real delete with keep=0: only recent, unpinned chats from the registry
    ccfg_keep = ccfg.CHAT_CLEANUP_KEEP
    try:
        ccfg.CHAT_CLEANUP_KEEP = 0
        r = client.post("/cluster/instances/0/cleanup", json={"dry_run": False})
    finally:
        ccfg.CHAT_CLEANUP_KEEP = ccfg_keep
    assert r.status_code == 200 and r.json()["tabs"][0]["deleted"] == 3
    assert sorted(s["deleted"]) == sorted(ours_new)
    left = {x["id"] for x in s["rows"]}
    assert {owner_today, owner_old, ours_old, ours_pinned, ours_unknown_pin} <= left
    # deleted chats left the registry too, the rest are still there
    assert set(json.loads(s["ledger"].read_text())) == {ours_old, ours_pinned, ours_unknown_pin}


def test_chatgpt_cleanup_with_broken_registry_deletes_nothing(chatgpt_sidebar):
    s, row = chatgpt_sidebar, chatgpt_sidebar["row"]
    s["rows"] = [row(chat_id(60)), row(chat_id(120))]
    s["ledger"].write_text("{broken json")
    n = asyncio.run(ChatGPTAdapter().delete_old_chats(DummyWS(), keep=0))
    assert n == 0 and s["deleted"] == []
