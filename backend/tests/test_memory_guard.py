"""Renderer memory guard (cluster/memory_guard.py) and its admin panel.

Key guarantees:
  * the Chrome browser process is never touched — only tabs are opened/closed, and
    about:blank is always opened first (Chrome exits when its last tab closes);
  * a worker serving a live request (lock held) is not touched in the background;
  * if a previously healthy session ends up on login/captcha after recovery — it is
    blocked, not retried, and the API side alerts via Telegram;
  * threshold, interval and daily cap; state survives a restart;
  * every action is written to the audit log.
"""
import asyncio
import json
import time
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi.testclient import TestClient

from app import cluster_watch
from cluster import instances as inst
from cluster import memory_guard as mg
from cluster import server as srv
from cluster import settings as ccfg
from cluster.adapters.chatgpt_adapter import ChatGPTAdapter
from cluster.pool import ClusterPool, WorkerTab

from .conftest import CSRF, add_user


# --- helpers --------------------------------------------------------------------

class DummyWS:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def make_worker(port=9222, model="chatgpt", sessions=None):
    """`sessions` — results check_session returns in order (the last one repeats)."""
    a = ChatGPTAdapter()
    w = WorkerTab(port, f"tab{port}", model, "ws://x", a)
    w.checks = []
    seq = list(sessions or [(True, "ok")])

    async def connect(open_timeout=10.0):
        return DummyWS()

    async def check_session(ws, timeout=5.0, recover=True):
        assert recover is False, "the check must not reload the page itself"
        w.checks.append(w.tab_id)
        return seq.pop(0) if len(seq) > 1 else seq[0]

    async def wait_for_input(ws, timeout=10.0, selector=None):
        return True
    w.connect = connect
    a.check_session, a.wait_for_input = check_session, wait_for_input
    return w


def make_pool(*workers, ports=(9222, 9223)):
    pool = ClusterPool(list(ports))
    pool.workers = list(workers)
    pool.scans = 0

    async def scan(force=False):
        pool.scans += 1
    pool.scan = scan
    return pool


class FakeBrowser:
    def __init__(self, port, ops, fail_close=False, fail_create_url=False):
        self.port, self.ops = port, ops
        self.fail_close, self.fail_create_url = fail_close, fail_create_url
        self.n = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def create(self, url):
        if url != "about:blank" and self.fail_create_url:
            self.ops.append(("create_failed", url))
            raise RuntimeError("create")
        self.n += 1
        tid = "blank" if url == "about:blank" else f"new{self.n}"
        self.ops.append(("create", url))
        return tid

    async def close(self, tid):
        if self.fail_close and tid != "blank":
            raise RuntimeError("close")
        self.ops.append(("close", tid))

    def page_ws_url(self, tid):
        return f"ws://127.0.0.1:{self.port}/devtools/page/{tid}"


@pytest.fixture
def guard(monkeypatch, tmp_path):
    """Fake /proc measurements and browser. Controlled via `g.mem[instance] = MB`."""
    mg.reset_state()
    monkeypatch.setattr(ccfg, "MEM_GUARD_STATE_FILE", str(tmp_path / "guard.json"))
    monkeypatch.setattr(ccfg, "MEM_GUARD_THRESHOLD_MB", 900)
    monkeypatch.setattr(ccfg, "MEM_GUARD_MIN_GAP", 1800)
    monkeypatch.setattr(ccfg, "MEM_GUARD_MAX_PER_DAY", 6)
    monkeypatch.setattr(ccfg, "MEM_GUARD_PID_WAIT", 0.2)
    monkeypatch.setattr(mg, "POST_CHECK_DELAY", 0.0)

    class G:
        mem = {}
        ops = []
        alive = set()
        browser_kw = {}
    g = G()

    def read_renderers(proc=None):
        return {n: [mg.Renderer(pid=1000 + n, rss_mb=mb, swap_mb=0),
                    mg.Renderer(pid=2000 + n, rss_mb=5000, swap_mb=0, webui=True)]
                for n, mb in g.mem.items()}

    monkeypatch.setattr(mg, "read_renderers", read_renderers)
    monkeypatch.setattr(mg, "pid_alive", lambda pid, n, proc=None: pid in g.alive)
    monkeypatch.setattr(mg, "open_browser", lambda port: FakeBrowser(port, g.ops, **g.browser_kw))
    monkeypatch.setattr(mg, "read_system", lambda proc=None: {"swap_percent": 87.0, "swap_total_mb": 6143,
                                                               "swap_used_mb": 5344, "mem_total_mb": 7751,
                                                               "mem_available_mb": 900})
    yield g
    mg.reset_state()


def run(coro):
    return asyncio.run(coro)


# --- /proc measurements -----------------------------------------------------------

def _proc(tmp_path, pid, cmd, rss_kb, swap_kb, nul=True):
    d = tmp_path / str(pid)
    d.mkdir()
    d.joinpath("cmdline").write_bytes(("\0" if nul else " ").join(cmd).encode() + b"\0")
    d.joinpath("status").write_text(f"Name:\tchrome\nVmRSS:\t {rss_kb} kB\nVmSwap:\t {swap_kb} kB\n")


def test_read_renderers_groups_by_profile_and_skips_browser_ui(tmp_path):
    prof = "--user-data-dir=/var/lib/ai-gateway/cluster/profiles/profile_{}"
    _proc(tmp_path, 11, ["/opt/google/chrome/chrome", "--type=renderer", prof.format(0)], 940_000, 158_000)
    # a Chrome child process rewrote its argv with spaces
    _proc(tmp_path, 12, ["/opt/google/chrome/chrome", "--type=renderer", prof.format(0)], 100_000, 0, nul=False)
    _proc(tmp_path, 13, ["/opt/google/chrome/chrome", "--type=renderer", "--top-chrome-webui", prof.format(0)],
          2_000_000, 0)
    _proc(tmp_path, 14, ["/opt/google/chrome/chrome", "--type=gpu-process", prof.format(0)], 900_000, 0)
    _proc(tmp_path, 15, ["/usr/bin/google-chrome", "--remote-debugging-port=9222", prof.format(0)], 900_000, 0)
    _proc(tmp_path, 16, ["/opt/google/chrome/chrome", "--type=renderer", prof.format(10)], 50_000, 0)
    _proc(tmp_path, 17, ["/usr/bin/python3", "--type=renderer", prof.format(0)], 9_000_000, 0)
    (tmp_path / "self").mkdir()

    r = mg.read_renderers(tmp_path)
    assert sorted(r) == [0, 10]
    assert [x.pid for x in r[0]] == [13, 11, 12]          # by weight
    top = mg.heaviest(r[0])
    assert (top.pid, top.rss_mb, top.swap_mb, top.total_mb) == (11, 917, 154, 1071)   # not webui
    assert mg.pid_alive(11, 0, tmp_path) and not mg.pid_alive(11, 1, tmp_path)
    assert not mg.pid_alive(99, 0, tmp_path)
    assert mg.read_renderers(tmp_path / "missing") == {}


def test_read_system_swap_percent(tmp_path):
    (tmp_path / "meminfo").write_text("MemTotal: 7937024 kB\nMemAvailable: 2486272 kB\n"
                                      "SwapTotal: 6291456 kB\nSwapFree: 786432 kB\n")
    s = mg.read_system(tmp_path)
    assert s["swap_total_mb"] == 6144 and s["swap_percent"] == 87.5 and s["mem_available_mb"] == 2428
    assert mg.read_system(tmp_path / "missing") == {}


# --- decision ---------------------------------------------------------------------

def test_decide_threshold_block_interval_and_daily_cap():
    now = 1_000_000.0
    kw = dict(threshold=900, min_gap=1800, max_per_day=3)
    st = mg.GuardState()
    assert mg.decide(st, 899, now, **kw) == "below_threshold"
    assert mg.decide(st, 900, now, **kw) is None
    st.last_attempt = now - 1799
    assert mg.decide(st, 2000, now, **kw) == "interval"
    st.last_attempt = now - 1800
    assert mg.decide(st, 2000, now, **kw) is None
    st.attempts = [now - 86401, now - 7200, now - 5400, now - 3600]   # the first one is older than 24 h
    assert mg.decide(st, 2000, now, **kw) == "daily_cap"
    assert st.attempts_24h(now) == 3
    st.attempts = st.attempts[1:]
    st.blocked = {"reason": "login_required"}
    assert mg.decide(st, 2000, now, **kw) == "blocked"


# --- recovery ---------------------------------------------------------------------

def test_auto_recovery_reopens_tab_without_touching_browser(guard):
    w = make_worker()
    pool = make_pool(w)
    guard.mem = {0: 1100, 1: 300}
    events = run(mg.run_once(pool, set()))
    # Order: blank -> close the old tab -> site -> close blank
    assert guard.ops == [("create", "about:blank"), ("close", "tab9222"),
                         ("create", "https://chatgpt.com/?temporary-chat=true"), ("close", "blank")]
    assert events[0]["ok"] is True and events[0]["kind"] == "recovered"
    assert w.tab_id == "new2" and w.ws_url.endswith("/devtools/page/new2")    # the worker moved to the new tab
    assert w.checks == ["tab9222", "new2"]                                    # checked before and after
    assert not w.lock.locked() and pool.scans == 2       # scanned before and after
    ev = mg.EVENTS[-1]
    assert ev["auto"] is True and ev["instance"] == 0 and ev["model"] == "chatgpt"
    assert mg.state_for(9222).attempts_24h(time.time()) == 1


def test_busy_worker_is_skipped_silently(guard):
    w = make_worker()
    pool = make_pool(w)
    guard.mem = {0: 1500}

    async def scenario():
        await w.lock.acquire()          # a live request
        return await mg.run_once(pool, set())
    assert run(scenario()) == []
    assert guard.ops == [] and w.checks == []
    assert mg.state_for(9222).attempts == [] and list(mg.EVENTS) == []


def test_admin_busy_port_and_below_threshold_are_skipped(guard):
    w = make_worker()
    pool = make_pool(w)
    guard.mem = {0: 1500}
    assert run(mg.run_once(pool, {9222})) == []      # an admin action is in progress
    guard.mem = {0: 899}
    assert run(mg.run_once(pool, set())) == []
    assert guard.ops == []


def test_session_lost_after_recovery_blocks_and_is_not_retried(guard):
    w = make_worker(sessions=[(True, "ok"), (False, "login_required")])
    pool = make_pool(w)
    guard.mem = {0: 1200}
    ev = run(mg.run_once(pool, set()))[0]
    assert ev["ok"] is False and ev["kind"] == "session_lost"
    assert ev["pre"] == "ok" and ev["post"] == "login_required"
    st = mg.state_for(9222)
    assert st.blocked["reason"] == "login_required"
    assert w.needs_attention == "login"                 # no requests are routed to it

    # The interval has passed and memory is still high — still no retry
    guard.ops.clear()
    st.last_attempt = 0
    w.needs_attention = None
    assert run(mg.run_once(pool, set())) == []
    assert run(mg.run_once(pool, set())) == []
    assert guard.ops == []
    skipped = [e for e in mg.EVENTS if e["kind"] == "skipped"]
    assert len(skipped) == 1 and "bloklangan" in skipped[0]["summary"]   # logged only once


def test_unresponsive_page_before_is_still_recovered_but_loss_is_flagged(guard):
    # Under memory pressure evaluate does not answer — inconclusive, recovery goes ahead
    w = make_worker(sessions=[(False, "probe_failed:TimeoutError"), (False, "captcha")])
    pool = make_pool(w)
    guard.mem = {0: 1700}
    ev = run(mg.run_once(pool, set()))[0]
    assert ev["kind"] == "session_lost" and mg.state_for(9222).blocked["reason"] == "captcha"


def test_definitely_dead_session_is_not_touched(guard):
    w = make_worker(sessions=[(False, "login_required")])
    pool = make_pool(w)
    guard.mem = {0: 1700}
    assert run(mg.run_once(pool, set())) == []
    assert guard.ops == [] and mg.state_for(9222).attempts == []
    assert mg.EVENTS[-1]["kind"] == "skipped" and "sog'lom emas" in mg.EVENTS[-1]["summary"]


def test_unverified_post_check_retries_then_reports_without_block(guard):
    w = make_worker(sessions=[(True, "ok"), (False, "no_chat_input")])
    pool = make_pool(w)
    guard.mem = {0: 1000}
    ev = run(mg.run_once(pool, set()))[0]
    assert ev["kind"] == "recover_unverified" and ev["ok"] is False
    assert w.checks.count("new2") == mg.POST_CHECK_TRIES
    assert mg.state_for(9222).blocked is None


def test_interval_between_recoveries(guard):
    w = make_worker()
    pool = make_pool(w)
    guard.mem = {0: 1000}
    assert len(run(mg.run_once(pool, set()))) == 1
    guard.ops.clear()
    assert run(mg.run_once(pool, set())) == []                 # 30 minutes have not passed
    assert guard.ops == []
    later = time.time() + 1801
    st = mg.state_for(9222)
    st.last_attempt -= 1801
    assert len(run(mg.run_once(pool, set(), now=later))) == 1


def test_daily_cap_logged_once(guard, monkeypatch):
    monkeypatch.setattr(ccfg, "MEM_GUARD_MAX_PER_DAY", 2)
    w = make_worker()
    pool = make_pool(w)
    guard.mem = {0: 1000}
    st = mg.state_for(9222)
    now = time.time()
    st.attempts = [now - 7200, now - 5400]
    assert run(mg.run_once(pool, set())) == [] and guard.ops == []
    run(mg.run_once(pool, set()))
    caps = [e for e in mg.EVENTS if "kunlik" in e["summary"]]
    assert len(caps) == 1


def test_only_one_instance_per_pass_heaviest_first(guard):
    a, b = make_worker(9222), make_worker(9223, model="qwen")
    pool = make_pool(a, b)
    guard.mem = {0: 1000, 1: 1400}
    events = run(mg.run_once(pool, set()))
    assert len(events) == 1 and b.tab_id.startswith("new") and a.tab_id == "tab9222"


def test_old_renderer_must_die_and_is_reported(guard):
    w = make_worker()
    pool = make_pool(w)
    guard.mem = {0: 1000}
    guard.alive = {1000}          # the old renderer does not exit
    ev = run(mg.run_once(pool, set()))[0]
    assert "eski renderer o'chmadi" in ev["summary"]
    assert ("create", "https://chatgpt.com/?temporary-chat=true") in guard.ops   # the site is reopened anyway


def test_close_failure_leaves_original_tab(guard):
    guard.browser_kw = {"fail_close": True}
    w = make_worker()
    pool = make_pool(w)
    guard.mem = {0: 1000}
    run(mg.run_once(pool, set()))
    assert guard.ops == [("create", "about:blank"), ("close", "blank")]
    assert w.tab_id == "tab9222" and not w.lock.locked()
    assert mg.EVENTS[-1]["kind"] == "recover_failed"


def test_reopen_failure_keeps_blank_tab_and_blocks(guard):
    guard.browser_kw = {"fail_create_url": True}
    w = make_worker()
    pool = make_pool(w)
    guard.mem = {0: 1000}
    ev = run(mg.run_once(pool, set()))[0]
    assert ev["ok"] is False
    assert ("close", "blank") not in guard.ops            # Chrome must not be left without a window
    assert mg.state_for(9222).blocked["reason"] == "reopen_failed"
    assert mg.EVENTS[-1]["kind"] == "reopen_failed" and not w.lock.locked()


def test_multi_tab_instance_recycles_one_idle_tab(guard):
    """Double buffering: one port may hold several tabs of the same provider.

    They are recycled ONE AT A TIME — the idle, least recently used tab — so the
    tab that is answering right now keeps its chat.
    """
    busy, idle = make_worker(), make_worker()
    busy.tab_id, idle.tab_id = "tabBusy", "tabIdle"
    busy.last_used, idle.last_used = 10.0, 5.0
    pool = make_pool(busy, idle)
    guard.mem = {0: 1100, 1: 300}

    async def scenario():
        await busy.lock.acquire()          # this tab is serving a request
        try:
            return await mg.run_once(pool, set())
        finally:
            busy.lock.release()

    events = run(scenario())
    assert guard.ops == [("create", "about:blank"), ("close", "tabIdle"),
                         ("create", "https://chatgpt.com/?temporary-chat=true"), ("close", "blank")]
    assert events[0]["ok"] is True and idle.tab_id == "new2"
    assert busy.tab_id == "tabBusy" and busy.checks == []     # the serving tab is untouched


def test_state_survives_restart(guard):
    w = make_worker(sessions=[(True, "ok"), (False, "login_required")])
    pool = make_pool(w)
    guard.mem = {0: 1000}
    run(mg.run_once(pool, set()))
    saved = json.loads(open(ccfg.MEM_GUARD_STATE_FILE).read())
    assert saved["9222"]["blocked"]["reason"] == "login_required"
    mg._STATES.clear()
    mg._loaded = False             # cluster restart
    st = mg.state_for(9222)
    assert st.blocked["reason"] == "login_required" and len(st.attempts) == 1


# --- cluster endpoints ---------------------------------------------------------------

@pytest.fixture
def cluster_client(monkeypatch):
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


def test_manual_recovery_endpoint_clears_block_on_success(guard, cluster_client):
    w = make_worker()
    client = cluster_client(make_pool(w))
    guard.mem = {0: 400}                       # below the threshold — still allowed manually
    st = mg.state_for(9222)
    st.blocked = {"ts": 1, "reason": "login_required", "model": "chatgpt"}
    st.last_attempt = time.time()              # the interval does not block a manual action either
    r = client.post("/cluster/instances/0/recover-memory")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert st.blocked is None and inst.LAST_ACTION[9222]["action"] == "recover_memory"
    assert mg.EVENTS[-1]["auto"] is False


def test_manual_recovery_refused_when_session_dead_or_busy(guard, cluster_client):
    w = make_worker(sessions=[(False, "login_required")])
    client = cluster_client(make_pool(w))
    r = client.post("/cluster/instances/0/recover-memory")
    assert r.status_code == 409 and r.json()["error"]["code"] == "session_unhealthy"
    assert guard.ops == []
    inst._BUSY.add(9222)
    assert client.post("/cluster/instances/0/recover-memory").json()["error"]["code"] == "busy"
    assert client.post("/cluster/instances/9/recover-memory").status_code == 404


def test_instances_list_and_memory_endpoint_report_memory(guard, cluster_client, monkeypatch):
    client = cluster_client(make_pool(make_worker()))
    guard.mem = {0: 1000}
    data = client.get("/cluster/instances").json()
    m = data["instances"][0]["memory"]
    assert m["renderer_rss_mb"] == 1000 and m["over_threshold"] is True and m["renderer_pid"] == 1000
    assert data["instances"][1]["memory"]["renderer_rss_mb"] is None
    assert data["system"]["swap_percent"] == 87.0 and data["memory_guard"]["threshold_mb"] == 900
    mg._event(9222, "chatgpt", "recovered", auto=True, ok=True, summary="x")
    first = client.get("/cluster/memory").json()["events"]
    assert len(first) == 1
    assert client.get(f"/cluster/memory?after={first[0]['id']}").json()["events"] == []
    monkeypatch.setattr(ccfg, "API_KEY", "secret")
    assert client.get("/cluster/memory").status_code == 401
    assert client.post("/cluster/instances/0/recover-memory").status_code == 401


# --- backend: admin panel, audit and Telegram ---------------------------------------------

def _login_admin(env):
    add_user(env, "admin@test.uz", role="admin")
    r = env.client.post("/portal/auth/login", json={"email": "admin@test.uz", "password": "parol-12345"},
                        headers=CSRF)
    assert r.status_code == 200


def _audit(env):
    return [dict(r) for r in env.db.all("SELECT admin_email, action, target, ok, detail FROM admin_audit ORDER BY id")]


def test_manual_recover_route_is_admin_only_background_and_audited(env):
    env.db.set_setting("cluster_base_url", "http://cluster/v1")
    env.db.set_setting("cluster_api_key", "cl-key")
    calls = []

    def handler(request):
        calls.append((request.url.path, request.headers.get("authorization")))
        return httpx.Response(200, json={"ok": True, "summary": "xotira bo'shatildi: 1100 MB -> 200 MB"})
    env.app.state.cluster_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    assert env.client.post("/portal/admin/instances/0/recover-memory", headers=CSRF).status_code == 401
    _login_admin(env)
    assert env.client.post("/portal/admin/instances/0/recover-memory").status_code == 403   # without CSRF
    r = env.client.post("/portal/admin/instances/0/recover-memory", headers=CSRF)
    assert r.status_code == 200 and r.json()["started"] is True
    for _ in range(100):
        if _audit(env):
            break
        time.sleep(0.05)
    assert calls == [("/cluster/instances/0/recover-memory", "Bearer cl-key")]
    row = _audit(env)[0]
    assert (row["admin_email"], row["action"], row["target"], row["ok"]) == \
        ("admin@test.uz", "recover_memory", "instance:0", 1)
    assert "1100 MB" in row["detail"]


def test_cluster_watch_audits_auto_events_once_and_alerts_on_session_loss(env, monkeypatch):
    now = int(time.time())
    events = [
        {"id": 10, "ts": now, "instance": 0, "model": "chatgpt", "kind": "recovered", "auto": True, "ok": True,
         "summary": "xotira bo'shatildi: 1100 MB -> 200 MB"},
        {"id": 11, "ts": now, "instance": 0, "model": "chatgpt", "kind": "session_lost", "auto": True,
         "ok": False, "summary": "tiklashdan keyin sessiya yo'qoldi (login_required; oldin: ok)"},
        {"id": 12, "ts": now, "instance": 1, "model": "qwen", "kind": "recovered", "auto": False, "ok": True,
         "summary": "qo'lda"},
    ]
    seen = []

    def handler(request):
        seen.append((request.url.path, dict(request.url.params), request.headers.get("authorization")))
        after = int(request.url.params.get("after", 0))
        return httpx.Response(200, json={"events": [e for e in events if e["id"] > after]})

    class App:
        state = type("S", (), {"cluster_http": httpx.AsyncClient(transport=httpx.MockTransport(handler))})
    monkeypatch.setattr(cluster_watch, "_state", {})
    msgs = run(cluster_watch._memory_events(App, env.db, "http://cluster", {"Authorization": "Bearer k"}))
    assert len(msgs) == 1 and "#0 (chatgpt)" in msgs[0] and "login_required" in msgs[0]
    rows = _audit(env)
    assert [(r["admin_email"], r["action"], r["target"]) for r in rows] == [
        ("avto-nazorat", "memory_recovered", "instance:0"),
        ("avto-nazorat", "memory_session_lost", "instance:0")]       # the manual event is not written again
    assert seen[0] == ("/cluster/memory", {"after": "0"}, "Bearer k")

    # The API restarted: the cluster returns the same events again — not written twice, no alert
    monkeypatch.setattr(cluster_watch, "_state", {})
    assert run(cluster_watch._memory_events(App, env.db, "http://cluster", {})) == []
    assert len(_audit(env)) == 2


def test_transient_login_page_after_reopen_is_not_session_lost(guard):
    # "Log in" flashes briefly while the page loads — the next check finds the session
    w = make_worker(sessions=[(True, "ok"), (False, "login_required"), (True, "ok")])
    pool = make_pool(w)
    guard.mem = {0: 1200}
    ev = run(mg.run_once(pool, set()))[0]
    assert ev["ok"] is True and ev["kind"] == "recovered"
    assert mg.state_for(9222).blocked is None


# --- Gemini (#9): recovery restarts the Chrome unit ---------------------------------------

@pytest.fixture
def chrome_restart(guard, monkeypatch):
    """Fake root helper: `request_restart` calls and the tabs after the restart."""
    monkeypatch.setattr(mg, "RESTART_GONE_WAIT", 0.2)
    monkeypatch.setattr(mg, "RESTART_UP_WAIT", 0.5)
    monkeypatch.setattr(mg, "RESTART_POLL", 0.05)
    monkeypatch.setattr(ccfg, "MEM_GUARD_RESTART_INSTANCES", frozenset({9}))
    guard.restarts = []
    guard.restart_result = (True, None)
    guard.pages = [{"type": "page", "id": "fresh", "url": "https://gemini.google.com/app",
                    "webSocketDebuggerUrl": "ws://127.0.0.1:9231/devtools/page/fresh"}]

    def request_restart(n):
        guard.restarts.append(n)
        return guard.restart_result
    monkeypatch.setattr(mg, "request_restart", request_restart)
    monkeypatch.setattr(mg, "list_pages", lambda port: list(guard.pages))
    return guard


def test_gemini_on_instance_9_restarts_chrome_and_keeps_worker(chrome_restart):
    g = chrome_restart
    w = make_worker(9231, "gemini")
    pool = make_pool(w, ports=(9231,))
    g.mem = {9: 1000}
    ev = run(mg.run_once(pool, set()))[0]
    assert g.restarts == [9] and g.ops == []             # no tab touched — the whole Chrome was restarted
    assert ev["ok"] is True and ev["kind"] == "recovered" and "Chrome qayta ishga tushirildi" in ev["summary"]
    assert w.tab_id == "fresh" and w.ws_url.endswith("/page/fresh") and w.checks == ["tab9231", "fresh"]
    assert pool.workers == [w] and not w.lock.locked()
    assert mg.report(pool)["instances"][9]["method"] == "restart"


def test_chrome_restart_only_for_gemini_on_allowed_instance(chrome_restart):
    g = chrome_restart
    # Gemini on another instance, or another provider on #9 — plain tab recovery
    for port, model in ((9222, "gemini"), (9231, "chatgpt")):
        mg.reset_state()
        g.ops.clear()
        w = make_worker(port, model)
        pool = make_pool(w, ports=(port,))
        g.mem = {port - 9222: 1000}
        ev = run(mg.run_once(pool, set()))[0]
        assert ev["ok"] is True and g.restarts == []
        assert g.ops[0] == ("create", "about:blank")
        assert mg.report(pool)["instances"][port - 9222]["method"] == "tab"


def test_chrome_restart_readopts_worker_dropped_by_scan(chrome_restart, monkeypatch):
    g = chrome_restart
    w = make_worker(9231, "gemini")
    pool = make_pool(w, ports=(9231,))
    g.mem = {9: 1000}

    def request_restart(n):
        pool.workers = []                 # a scan during the restart saw the port down and dropped the worker
        return True, None
    monkeypatch.setattr(mg, "request_restart", request_restart)
    ev = run(mg.run_once(pool, set()))[0]
    assert ev["ok"] is True and pool.workers == [w] and w.tab_id == "fresh"


def test_chrome_restart_failure_is_reported_without_block(chrome_restart):
    g = chrome_restart
    g.restart_result = (False, "tiklash yordamchisi javob bermadi")
    w = make_worker(9231, "gemini")
    pool = make_pool(w, ports=(9231,))
    g.mem = {9: 1000}
    ev = run(mg.run_once(pool, set()))[0]
    assert ev["ok"] is False and ev["kind"] == "restart_failed" and "javob bermadi" in ev["summary"]
    st = mg.state_for(9231)
    assert st.blocked is None and st.attempts_24h(time.time()) == 1 and not w.lock.locked()
    assert w.tab_id == "tab9231"


def test_chrome_restart_opens_site_tab_when_missing(chrome_restart):
    g = chrome_restart
    g.pages = [{"type": "page", "id": "nt", "url": "chrome://newtab/"}]
    w = make_worker(9231, "gemini")
    pool = make_pool(w, ports=(9231,))
    g.mem = {9: 1000}
    ev = run(mg.run_once(pool, set()))[0]
    assert ev["ok"] is True and g.ops == [("create", "https://gemini.google.com/app")]
    assert w.tab_id == "new1"
