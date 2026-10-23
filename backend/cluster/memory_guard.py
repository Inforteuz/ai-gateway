"""Chrome renderer memory control and tab recovery.

PROBLEM: the renderer process of a ChatGPT tab accumulated memory on a single
page and reached 920 MB RAM + 741 MB swap; server swap filled to 87% and ~44%
of ChatGPT requests timed out (`cdp.evaluate` got no reply within 20 s).
Both `Page.reload` and a new tab of the same site stay in the OLD renderer
process — memory is not released.

WHAT WORKS (tested manually, 1 663 MB -> 205 MB):
  open an about:blank tab -> close the site tab -> wait for the old renderer
  pid to exit -> open the site in a new tab -> close the blank tab -> rescan.
The blank tab is required: closing the last tab closes the window and Chrome exits.

GEMINI EXCEPTION: the Gemini renderer grows to ~1 GB in ~10 requests and
reopening the tab does not release it — because of `--renderer-process-limit=2`
the new tab lands in the same old renderer ("eski renderer o'chmadi"). `systemctl
restart ai-gateway-chrome@9` does release it and the Google login survives (verified
manually). So ONLY when a provider from `RESTART_MODELS` sits on an instance listed
in `MEM_GUARD_RESTART_INSTANCES` does recovery restart the Chrome unit (through the
root helper in `self_heal`, after a cookie backup). Other instances hold the
owner's personal logins — Chrome is never touched there. Xvfb is never restarted.

GUARANTEES:
  * apart from the exception above the Chrome browser process is NEVER
    restarted and profiles/cookies are never touched — only one tab is
    closed and reopened;
  * the worker lock is held: a tab serving a live request is not touched (in
    background mode a busy lock simply defers to the next cycle);
  * the session is checked BEFORE and AFTER recovery. If a session that was
    healthy (or undetermined) before ends up in a login/captcha state
    afterwards, that is a "session_lost" event: auto-recovery is blocked on
    the instance and not retried (the API side notifies via Telegram). ChatGPT
    was once found logged out after a recovery, cause unknown;
  * there is a minimum interval between recoveries and a daily cap; the state
    is persisted to a file — limits and blocks survive a cluster restart.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import urllib.request
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

import websockets

from . import cdp, logs, self_heal
from . import settings as cfg
from .adapters.base_adapter import _HOME_URLS
from .pool import ClusterPool, WorkerTab, detect_model_from_url

log = logs.get("memory_guard")

BASE_PORT = 9222
PROC = Path(os.getenv("SF_CLUSTER_PROC_ROOT", "/proc"))

# These reasons state the session status DEFINITIVELY. The rest (probe_failed:*,
# ws:*, no_chat_input) are ambiguous: a page under memory pressure not answering
# evaluate is a symptom of this very problem, so they do not stop recovery.
DEFINITIVE_BAD = ("login_required", "captcha")

# Post-recovery check: several attempts so the page can fully load
POST_CHECK_TRIES = 4
POST_CHECK_DELAY = 5.0
# How long to wait for the input field in the new tab (seconds)
LOAD_WAIT = 30.0

DAY = 86400

# Providers recovered by restarting Chrome. Only add a provider whose account holds
# none of the owner's personal sessions and whose login is confirmed to survive a
# restart (see the module docstring).
RESTART_MODELS = ("gemini",)
# After a restart: wait for the old Chrome process to exit and CDP to come back (seconds)
RESTART_GONE_WAIT = 30.0
RESTART_UP_WAIT = 90.0
RESTART_POLL = 2.0

# Events (newest at the end, not the start; the API reads by `id`)
EVENTS: Deque[Dict[str, Any]] = deque(maxlen=60)
_seq = 0


# --- /proc measurement -----------------------------------------------------------

@dataclass
class Renderer:
    pid: int
    rss_mb: int
    swap_mb: int
    webui: bool = False      # the browser's own UI (tab strip) — not a site

    @property
    def total_mb(self) -> int:
        return self.rss_mb + self.swap_mb


_PROFILE_RE = re.compile(r"--user-data-dir=\S*?profile_(\d+)(?:\s|/|$)")


def _status_kb(text: str, key: str) -> int:
    m = re.search(rf"^{key}:\s+(\d+)", text, re.M)
    return int(m.group(1)) if m else 0


def _read_cmdline(pid_dir: Path) -> str:
    # Chrome child processes rewrite argv: arguments end up as one space-separated
    # string instead of NUL-separated — handle both.
    return pid_dir.joinpath("cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")


def _renderer_profile(cmd: str) -> Optional[int]:
    if "--type=renderer" not in cmd or "chrome" not in cmd.split(" ", 1)[0]:
        return None
    m = _PROFILE_RE.search(cmd)
    return int(m.group(1)) if m else None


def read_renderers(proc: Optional[Path] = None) -> Dict[int, List[Renderer]]:
    """{instance: [renderer, ...]} — heaviest first.

    The instance comes from `--user-data-dir=.../profile_N` (N = port - 9222,
    launch-chrome.sh convention). Returns empty on systems without /proc (macOS).
    """
    proc = proc or PROC
    out: Dict[int, List[Renderer]] = {}
    try:
        entries = list(proc.iterdir())
    except OSError:
        return out
    for d in entries:
        if not d.name.isdigit():
            continue
        try:
            cmd = _read_cmdline(d)
            n = _renderer_profile(cmd)
            if n is None:
                continue
            st = d.joinpath("status").read_text()
        except OSError:
            continue      # the process exited in the meantime
        out.setdefault(n, []).append(Renderer(
            pid=int(d.name), rss_mb=_status_kb(st, "VmRSS") // 1024,
            swap_mb=_status_kb(st, "VmSwap") // 1024, webui="--top-chrome-webui" in cmd))
    for lst in out.values():
        lst.sort(key=lambda r: -r.total_mb)
    return out


def heaviest(renderers: List[Renderer]) -> Optional[Renderer]:
    """The heaviest site renderer (the browser UI is ignored)."""
    return next((r for r in renderers if not r.webui), None)


def pid_alive(pid: int, instance: int, proc: Optional[Path] = None) -> bool:
    """Whether this pid is still that instance's renderer (guards against pid reuse)."""
    try:
        return _renderer_profile(_read_cmdline((proc or PROC) / str(pid))) == instance
    except OSError:
        return False


def read_system(proc: Optional[Path] = None) -> Dict[str, Any]:
    try:
        mi = ((proc or PROC) / "meminfo").read_text()
    except OSError:
        return {}
    mb = lambda k: _status_kb(mi, k) // 1024  # noqa: E731
    swap_total, swap_free = mb("SwapTotal"), mb("SwapFree")
    used = swap_total - swap_free
    return {"mem_total_mb": mb("MemTotal"), "mem_available_mb": mb("MemAvailable"),
            "swap_total_mb": swap_total, "swap_used_mb": used,
            "swap_percent": round(100.0 * used / swap_total, 1) if swap_total else 0.0}


# --- state (persisted to a file) ----------------------------------------------------

@dataclass
class GuardState:
    attempts: List[float] = field(default_factory=list)   # recovery attempts (24 hours)
    last_attempt: float = 0.0
    blocked: Optional[Dict[str, Any]] = None               # {"ts", "reason", "model"}
    last_result: Optional[Dict[str, Any]] = None
    last_skip: str = ""                                    # avoids logging duplicate events

    def attempts_24h(self, now: float) -> int:
        self.attempts = [t for t in self.attempts if t > now - DAY]
        return len(self.attempts)


_STATES: Dict[int, GuardState] = {}
_loaded = False


def _state_file() -> Optional[Path]:
    return Path(cfg.MEM_GUARD_STATE_FILE) if cfg.MEM_GUARD_STATE_FILE else None


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    path = _state_file()
    if not path or not path.exists():
        return
    try:
        raw = json.loads(path.read_text())
        for port, d in raw.items():
            _STATES[int(port)] = GuardState(**{k: v for k, v in d.items() if k in GuardState.__dataclass_fields__})
    except Exception as e:
        log.warning("could not read memory guard state (%s): %s", path, e)


def _save() -> None:
    path = _state_file()
    if not path:
        return
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({str(p): asdict(s) for p, s in _STATES.items()}))
        tmp.replace(path)
    except Exception as e:
        log.warning("could not write memory guard state (%s): %s", path, e)


def state_for(port: int) -> GuardState:
    _load()
    return _STATES.setdefault(port, GuardState())


def reset_state() -> None:
    """For tests: clears the in-memory state."""
    global _loaded, _seq
    _STATES.clear()
    EVENTS.clear()
    _loaded = False
    _seq = 0


def _event(port: int, model: str, kind: str, *, auto: bool, ok: bool, summary: str, **extra) -> Dict[str, Any]:
    global _seq
    _seq += 1
    ev = {"id": int(time.time() * 1000) * 100 + _seq % 100, "ts": int(time.time()),
          "instance": port - BASE_PORT, "port": port, "model": model, "kind": kind,
          "auto": auto, "ok": ok, "summary": summary, **extra}
    EVENTS.append(ev)
    (log.info if ok else log.warning)("memory: #%d %s %s %s", port - BASE_PORT, kind, model, summary)
    return ev


# --- decision --------------------------------------------------------------------

def decide(st: GuardState, top_mb: int, now: float, *, threshold: int, min_gap: float,
           max_per_day: int) -> Optional[str]:
    """Whether to auto-recover. `None` — recover; otherwise the reason to skip."""
    if top_mb < threshold:
        return "below_threshold"
    if st.blocked:
        return "blocked"
    if now - st.last_attempt < min_gap:
        return "interval"
    if st.attempts_24h(now) >= max_per_day:
        return "daily_cap"
    return None


# --- browser-level CDP --------------------------------------------------------------

def _cdp_json(port: int, path: str) -> Any:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3.0) as resp:
        return json.loads(resp.read().decode())


class Browser:
    """Browser-level CDP connection: opens/closes tabs (does not touch the tab connection)."""

    def __init__(self, port: int) -> None:
        self.port = port
        self._ws = None

    async def __aenter__(self):
        version = await asyncio.get_event_loop().run_in_executor(None, _cdp_json, self.port, "/json/version")
        self._ws = await websockets.connect(version["webSocketDebuggerUrl"], ping_interval=None, open_timeout=5)
        return self

    async def __aexit__(self, *exc):
        await self._ws.close()

    async def create(self, url: str) -> str:
        res = await cdp.call(self._ws, "Target.createTarget", {"url": url}, timeout=10)
        return res["targetId"]

    async def close(self, target_id: str) -> None:
        await cdp.call(self._ws, "Target.closeTarget", {"targetId": target_id}, timeout=10)

    def page_ws_url(self, target_id: str) -> str:
        return f"ws://127.0.0.1:{self.port}/devtools/page/{target_id}"


def open_browser(port: int) -> Browser:
    return Browser(port)


def list_pages(port: int) -> List[Dict[str, Any]]:
    """Page tabs in the browser; an empty list if CDP does not respond."""
    try:
        return [t for t in _cdp_json(port, "/json") if t.get("type") == "page"]
    except Exception:
        return []


def restart_allowed(instance: int, model: str) -> bool:
    return model in RESTART_MODELS and instance in cfg.MEM_GUARD_RESTART_INSTANCES


def request_restart(instance: int) -> tuple[bool, Optional[str]]:
    """Cookie backup + `ai-gateway-chrome@N` restart request to the root helper (blocking)."""
    self_heal.backup_cookies(instance)
    return self_heal.request_restart(instance)


# --- recovery -------------------------------------------------------------------

class Skip(Exception):
    """Recovery did not start (nothing was touched)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


async def _check(w: WorkerTab) -> tuple[bool, str]:
    try:
        async with await w.connect(open_timeout=8) as ws:
            return await w.adapter.check_session(ws, timeout=8, recover=False)
    except Exception as e:
        return False, f"ws:{type(e).__name__}"


async def _wait_pid_gone(pid: int, instance: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid, instance):
            return True
        await asyncio.sleep(0.5)
    return not pid_alive(pid, instance)


def _top(instance: int) -> Optional[Renderer]:
    return heaviest(read_renderers().get(instance, []))


async def _restart_chrome(pool: ClusterPool, w: WorkerTab, port: int, url: str,
                          old_pid: Optional[int], notes: List[str]) -> Optional[str]:
    """Restarts the Chrome unit and moves the worker to the new tab.

    The caller holds the worker lock. Returns: an error text or `None` (success).
    """
    instance = port - BASE_PORT
    loop = asyncio.get_event_loop()
    ok, err = await loop.run_in_executor(None, request_restart, instance)
    if not ok:
        return f"Chrome qayta ishga tushirilmadi: {err}"
    # After the helper accepts the request the old Chrome keeps answering for a while —
    # wait for the old renderer to exit first, then for CDP to come back.
    if old_pid and not await _wait_pid_gone(old_pid, instance, RESTART_GONE_WAIT):
        notes.append("eski renderer o'chmadi")
    page, mine = None, []
    deadline = time.monotonic() + RESTART_UP_WAIT
    while time.monotonic() < deadline:
        pages = await loop.run_in_executor(None, list_pages, port)
        mine = [t for t in pages if detect_model_from_url(t.get("url", "")) == w.model_name]
        if mine:
            page = mine[0]
            break
        if pages and time.monotonic() > deadline - RESTART_UP_WAIT / 2:
            # Chrome is up but the site tab was not opened — open it ourselves
            async with open_browser(port) as b:
                tid = await b.create(url)
            page = {"id": tid, "webSocketDebuggerUrl": b.page_ws_url(tid)}
            break
        await asyncio.sleep(RESTART_POLL)
    if page is None:
        return "Chrome qayta ishga tushdi, lekin CDP javob bermayapti"
    if len(mine) > 1:
        # Chrome may have restored previous tabs — close the extras
        async with open_browser(port) as b:
            for t in mine[1:]:
                try:
                    await b.close(t["id"])
                except Exception:
                    notes.append("ortiqcha tab yopilmadi")
    # The worker object (lock, stats) moves to the new tab. A scan during the restart may
    # have dropped it from the list — put it back, otherwise the next scan would create
    # a new worker without the lock.
    w.tab_id = page["id"]
    w.ws_url = page.get("webSocketDebuggerUrl") or f"ws://127.0.0.1:{port}/devtools/page/{page['id']}"
    w.warm_since = 0.0
    if w not in pool.workers:
        pool.workers = [x for x in pool.workers
                        if not (x.browser_port == port and x.model_name == w.model_name)] + [w]
    try:
        async with await w.connect(open_timeout=10) as ws:
            await w.adapter.wait_for_input(ws, timeout=LOAD_WAIT)
    except Exception as e:
        notes.append(f"yuklanish: {type(e).__name__}")
    notes.append("Chrome qayta ishga tushirildi")
    return None


async def recover(pool: ClusterPool, port: int, *, auto: bool, lock_wait: float = 0.0) -> Dict[str, Any]:
    """Reopens the instance's provider tab in a new renderer.

    `auto=True` (background): `Skip("busy")` immediately if the lock is taken. Manual:
    waits `lock_wait` seconds for the lock. In both cases the before/after session
    check and the `session_lost` block work the same way.
    """
    instance = port - BASE_PORT
    st = state_for(port)
    workers = [w for w in pool.workers if w.browser_port == port]
    if not workers:
        raise Skip("no_tab", "Provayder tabi topilmadi.")
    # Several tabs of one provider is the double-buffered setup (one answers while
    # the other prepares a clean chat), so recycle them ONE AT A TIME: take an idle
    # tab, least recently used first. A Chrome restart would take the other tab
    # down with it, so that route stays closed while more than one tab is open.
    multi_tab = len(workers) > 1
    workers.sort(key=lambda x: (x.lock.locked(), x.last_used))
    w = workers[0]
    url = _HOME_URLS.get(w.model_name)
    if not url:
        raise Skip("no_home_url", f"{w.model_name} uchun bosh sahifa manzili yo'q.")

    if auto:
        if w.lock.locked():
            raise Skip("busy", "Instansiya so'rov bajaryapti.")
        await w.lock.acquire()
    else:
        try:
            await asyncio.wait_for(w.lock.acquire(), timeout=lock_wait)
        except asyncio.TimeoutError:
            raise Skip("busy", "Instansiya hozir so'rov bajaryapti — birozdan keyin urinib ko'ring.")

    model = w.model_name
    try:
        before = _top(instance)
        pre_ok, pre_reason = await _check(w)
        if not pre_ok and pre_reason in DEFINITIVE_BAD:
            w.apply_probe(pre_ok, pre_reason)
            raise Skip("session_unhealthy",
                       f"Sessiya tiklashdan oldin ham sog'lom emas ({pre_reason}) — avval VNC orqali tekshiring.")

        now = time.time()
        st.last_attempt = now
        st.attempts.append(now)
        _save()

        old_id = w.tab_id
        old_pid = before.pid if before else None
        notes: List[str] = []
        if restart_allowed(instance, model) and not multi_tab:
            err = await _restart_chrome(pool, w, port, url, old_pid, notes)
            if err:
                st.last_result = {"ts": int(time.time()), "ok": False, "summary": err}
                _save()
                _event(port, model, "restart_failed", auto=auto, ok=False, summary=err)
                return {"ok": False, "summary": err, "kind": "restart_failed"}
        else:
            async with open_browser(port) as b:
                blank = await b.create("about:blank")
                try:
                    await b.close(old_id)
                except Exception:
                    # The site tab did not close — nothing changed, just remove the blank tab
                    await b.close(blank)
                    raise
                if old_pid and not await _wait_pid_gone(old_pid, instance, cfg.MEM_GUARD_PID_WAIT):
                    notes.append("eski renderer o'chmadi")
                new_id = None
                for _ in range(2):
                    try:
                        new_id = await b.create(url)
                        break
                    except Exception as e:
                        log.warning("memory: #%d could not open site tab: %s", instance, e)
                        await asyncio.sleep(2.0)
                if new_id is None:
                    # The blank tab STAYS — otherwise the last window closes and Chrome exits
                    st.blocked = {"ts": int(time.time()), "reason": "reopen_failed", "model": model}
                    st.last_result = {"ts": int(time.time()), "ok": False, "summary": "sayt tabi qayta ochilmadi"}
                    _save()
                    _event(port, model, "reopen_failed", auto=auto, ok=False,
                           summary="sayt tabi yopildi, lekin qayta ochilmadi — VNC orqali oching")
                    return {"ok": False, "summary": "sayt tabi qayta ochilmadi — VNC orqali oching"}
                # The worker object (lock, stats, cooldown) moves to the new tab
                w.tab_id, w.ws_url, w.warm_since = new_id, b.page_ws_url(new_id), 0.0
                try:
                    async with await w.connect(open_timeout=10) as ws:
                        await w.adapter.wait_for_input(ws, timeout=LOAD_WAIT)
                except Exception as e:
                    notes.append(f"yuklanish: {type(e).__name__}")
                try:
                    await b.close(blank)
                except Exception:
                    notes.append("blank tab yopilmadi")

        post_ok, post_reason = False, ""
        for i in range(POST_CHECK_TRIES):
            post_ok, post_reason = await _check(w)
            # Measured: a reopened ChatGPT page briefly shows the "Log in" button until
            # auth loads — the first check returned `login_required` and ChatGPT was
            # needlessly disabled for 10 minutes. So the session only counts as lost if
            # the login marker persists across all attempts.
            if post_ok or post_reason == "captcha":
                break
            if i + 1 < POST_CHECK_TRIES:
                await asyncio.sleep(POST_CHECK_DELAY)
        w.apply_probe(post_ok, post_reason)
        after = _top(instance)
    finally:
        w.lock.release()
    try:
        await pool.scan(force=True)
    except Exception as e:
        log.warning("memory: post-recovery scan: %s", e)

    mb = lambda r: r.total_mb if r else None  # noqa: E731
    freed = f"{mb(before)} MB -> {mb(after)} MB" if before and after else ""
    extra = {"before_mb": mb(before), "after_mb": mb(after), "pre": pre_reason, "post": post_reason}
    if not post_ok and post_reason in DEFINITIVE_BAD:
        st.blocked = {"ts": int(time.time()), "reason": post_reason, "model": model}
        summary = (f"tiklashdan keyin sessiya yo'qoldi ({post_reason}; oldin: {pre_reason}) — "
                   "avto-tiklash to'xtatildi, VNC orqali tekshiring")
        kind, ok = "session_lost", False
    elif not post_ok:
        summary = f"tab qayta ochildi, lekin sessiya tasdiqlanmadi ({post_reason}) {freed}".strip()
        kind, ok = "recover_unverified", False
    else:
        if not auto:
            st.blocked = None   # admin recovered manually and the session is intact — lift the block
        summary = f"xotira bo'shatildi: {freed}" if freed else "tab qayta ochildi, sessiya joyida"
        kind, ok = "recovered", True
    if notes:
        summary += " (" + "; ".join(notes) + ")"
    st.last_result = {"ts": int(time.time()), "ok": ok, "summary": summary}
    _save()
    _event(port, model, kind, auto=auto, ok=ok, summary=summary, **extra)
    return {"ok": ok, "summary": summary, "kind": kind, **extra}


# --- background cycle and report -----------------------------------------------------

def _config() -> Dict[str, Any]:
    return {"enabled": bool(cfg.MEM_GUARD_INTERVAL), "interval": cfg.MEM_GUARD_INTERVAL,
            "threshold_mb": cfg.MEM_GUARD_THRESHOLD_MB, "min_gap": cfg.MEM_GUARD_MIN_GAP,
            "max_per_day": cfg.MEM_GUARD_MAX_PER_DAY}


async def run_once(pool: ClusterPool, busy: set, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """One cycle: recovers ONE instance over the threshold (one at a time).

    `busy` — ports with an admin action in progress (`instances._BUSY`): they are
    left alone, and during recovery the port is added to this set.
    """
    now = time.time() if now is None else now
    renderers = read_renderers()
    candidates = []
    for port in pool.ports:
        top = heaviest(renderers.get(port - BASE_PORT, []))
        if not top:
            continue
        st = state_for(port)
        why = decide(st, top.total_mb, now, threshold=cfg.MEM_GUARD_THRESHOLD_MB,
                     min_gap=cfg.MEM_GUARD_MIN_GAP, max_per_day=cfg.MEM_GUARD_MAX_PER_DAY)
        if why is None:
            candidates.append((top.total_mb, port))
        elif why == "below_threshold":
            st.last_skip = ""
        elif why in ("blocked", "daily_cap") and st.last_skip != why:
            # Over the threshold but not recovered — record it once
            st.last_skip = why
            ws = [w for w in pool.workers if w.browser_port == port]
            _event(port, ws[0].model_name if ws else "", "skipped", auto=True, ok=False,
                   summary=f"renderer {top.total_mb} MB, lekin avto-tiklash o'tkazib yuborildi: "
                           + ("bloklangan (oldingi tiklashda sessiya yo'qolgan)" if why == "blocked"
                              else f"kunlik chegara ({cfg.MEM_GUARD_MAX_PER_DAY})"))
    events: List[Dict[str, Any]] = []
    if candidates:
        # Tab IDs may be stale (navigation, manual close) — refresh them
        await pool.scan(force=True)
    for _mb, port in sorted(candidates, reverse=True):
        if port in busy:
            continue
        ws = [w for w in pool.workers if w.browser_port == port]
        if ws and ws[0].needs_attention in ("login", "captcha"):
            continue    # needs an operator — recovery will not help
        busy.add(port)
        try:
            res = await recover(pool, port, auto=True)
            state_for(port).last_skip = ""
            events.append(res)
        except Skip as e:
            st = state_for(port)
            if e.code != "busy" and st.last_skip != e.code:
                st.last_skip = e.code
                _event(port, ws[0].model_name if ws else "", "skipped", auto=True, ok=False, summary=e.message)
            continue
        except Exception as e:
            log.exception("memory: #%d unexpected error during recovery", port - BASE_PORT)
            st = state_for(port)
            st.last_result = {"ts": int(time.time()), "ok": False, "summary": type(e).__name__}
            _save()
            _event(port, ws[0].model_name if ws else "", "recover_failed", auto=True, ok=False,
                   summary=f"tiklash xatosi: {type(e).__name__}")
        finally:
            busy.discard(port)
        break   # one recovery per cycle
    return events


def report(pool: ClusterPool, now: Optional[float] = None) -> Dict[str, Any]:
    """For the admin panel: per-instance memory and state, plus overall server memory."""
    now = time.time() if now is None else now
    renderers = read_renderers()
    per: Dict[int, Dict[str, Any]] = {}
    for port in pool.ports:
        n = port - BASE_PORT
        lst = renderers.get(n, [])
        top = heaviest(lst)
        st = state_for(port)
        models = {w.model_name for w in pool.workers if w.browser_port == port}
        per[n] = {
            # "restart" — recovery restarts the Chrome unit (Gemini only, #9)
            "method": "restart" if any(restart_allowed(n, m) for m in models) else "tab",
            "renderer_rss_mb": top.rss_mb if top else None,
            "renderer_swap_mb": top.swap_mb if top else None,
            "renderer_pid": top.pid if top else None,
            "renderers_total_mb": sum(r.total_mb for r in lst) if lst else None,
            "over_threshold": bool(top and top.total_mb >= cfg.MEM_GUARD_THRESHOLD_MB),
            "blocked": st.blocked,
            "last_result": st.last_result,
            "recoveries_24h": st.attempts_24h(now),
            "next_allowed": int(st.last_attempt + cfg.MEM_GUARD_MIN_GAP) if st.last_attempt else 0,
        }
    return {"system": read_system(), "config": _config(), "instances": per}


def events_since(after_id: int = 0) -> List[Dict[str, Any]]:
    return [e for e in EVENTS if e["id"] > after_id]
