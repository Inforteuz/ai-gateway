"""Managing browser instances from the admin panel — without logging into VNC.

Instance N = CDP port - 9222 (launch-chrome.sh convention: ai-gateway-chrome@N).
The actions here NEVER restart Chrome and never touch cookies — the owner's
logins matter:

  * probe   — check the session (only inspects the DOM, sends no prompt and
              does not reload the page);
  * reload  — navigates the tab to the provider's home page (`location.href`,
              i.e. plain navigation: cookies and login are kept);
  * cleanup — deletes chats according to the adapter's `CLEANUP_POLICY`:
              "registry" (ChatGPT) — only IDs from the gateway's registry, the
              owner's personal chats can never be in it; "pin" (DeepSeek) — the
              unpinned ones; "all" (Qwen, Gemini) — everything, approved by the
              owner. Refused for adapters without a policy (Grok);
  * focus   — brings this instance's window to the front when VNC is opened
              (all Chromes share one Xvfb display).
"""
from __future__ import annotations

import asyncio
import inspect
import json
import time
import urllib.request
from typing import Any, Dict, List, Optional

import websockets

from . import cdp, logs
from .adapters.base_adapter import _HOME_URLS
from .pool import ClusterPool, WorkerTab

log = logs.get("instances")

BASE_PORT = 9222

# State codes (the admin panel maps them to human-readable labels).
# Order = severity: if a port has several tabs, the worst one is shown.
STATE_OK = "ok"                # working
STATE_RESTING = "resting"      # resting (cooldown / quota)
STATE_LOGIN = "login"          # login required
STATE_CAPTCHA = "captcha"      # provider bot check — the operator solves it via VNC
STATE_CRASHED = "crashed"      # page crashed / no input field
STATE_NO_TAB = "no_tab"        # browser is up, but no provider tab was found
STATE_DOWN = "down"            # browser does not respond to CDP
_SEVERITY = [STATE_OK, STATE_RESTING, STATE_LOGIN, STATE_CAPTCHA, STATE_CRASHED, STATE_NO_TAB, STATE_DOWN]

# Policies allowed for manual cleanup (see the module docstring)
CLEANUP_POLICIES = ("registry", "pin", "all")

# Maximum number of titles in a dry-run response
_MAX_TITLES = 150

# Probe reasons that mean the page itself is broken, not a login problem
_CRASH_REASONS = ("no_chat_input", "probe_failed", "probe_bad_result", "ws:")

# Last manual action per instance (in memory)
LAST_ACTION: Dict[int, Dict[str, Any]] = {}

# Ports with an action in progress — no two actions run in parallel on one instance
_BUSY: set[int] = set()


class ActionError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def port_of(instance: int) -> int:
    return BASE_PORT + instance


def instance_of(port: int) -> int:
    return port - BASE_PORT


def worker_state(w: WorkerTab, now: Optional[float] = None) -> str:
    now = time.time() if now is None else now
    probe = w.last_probe or {}
    reason = str(probe.get("reason") or "")
    if probe and not probe.get("ok") and reason.startswith(_CRASH_REASONS):
        return STATE_CRASHED
    if w.needs_attention == "captcha" or reason == "captcha":
        return STATE_CAPTCHA
    if w.needs_attention == "login" or reason == "login_required":
        return STATE_LOGIN
    if w.needs_attention == "quota" or now < w.cooldown_until:
        return STATE_RESTING
    return STATE_OK


def cleanup_info(w: WorkerTab) -> Dict[str, Any]:
    """Whether manual cleanup is possible and how many chats the registry holds."""
    policy = getattr(w.adapter, "CLEANUP_POLICY", None)
    ok = bool(w.adapter.caps.delete_chats) and policy in CLEANUP_POLICIES
    info: Dict[str, Any] = {"supported": ok, "policy": policy}
    if ok and policy == "registry":
        try:
            info["registry_size"] = int(w.adapter.registry_size())
        except Exception:
            info["registry_size"] = None
    return info


def snapshot(pool: ClusterPool, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """One record per CDP port — a card in the admin panel."""
    now = time.time() if now is None else now
    out = []
    for port in pool.ports:
        workers = [w for w in pool.workers if w.browser_port == port]
        tabs = []
        for w in workers:
            tabs.append({
                "model": w.model_name,
                "tab_id": w.short_tab,
                "state": worker_state(w, now),
                "busy": w.lock.locked(),
                "cooldown_left": max(0, int(w.cooldown_until - now)),
                "needs_attention": w.needs_attention,
                "last_error": w.last_error,
                "last_probe": w.last_probe,
                "last_used": int(w.last_used) if w.last_used else None,
                "cleanup": cleanup_info(w),
            })
        if port in pool.scan_errors:
            state = STATE_DOWN
        elif not tabs:
            state = STATE_NO_TAB
        else:
            state = max((t["state"] for t in tabs), key=_SEVERITY.index)
        last_error = next((t["last_error"] for t in tabs if t["last_error"]), None)
        if state == STATE_DOWN:
            last_error = pool.scan_errors.get(port)
        out.append({
            "instance": instance_of(port),
            "port": port,
            "state": state,
            "models": sorted({t["model"] for t in tabs}),
            "last_error": last_error,
            "tabs": tabs,
            "busy": port in _BUSY,
            "last_action": LAST_ACTION.get(port),
        })
    return out


# --- actions -----------------------------------------------------------------

def _resolve(pool: ClusterPool, instance: int) -> int:
    port = port_of(instance)
    if port not in pool.ports:
        raise ActionError(404, "unknown_instance", f"{instance}-instansiya klasterda yo'q.")
    return port


async def _workers(pool: ClusterPool, port: int) -> List[WorkerTab]:
    await pool.scan(force=True)
    if port in pool.scan_errors:
        raise ActionError(409, "browser_down",
                          "Brauzer javob bermayapti — VNC orqali tekshiring.")
    ws = [w for w in pool.workers if w.browser_port == port]
    if not ws:
        raise ActionError(409, "no_tab",
                          "Provayder tabi topilmadi (sahifa boshqa saytda bo'lishi mumkin) — VNC orqali tekshiring.")
    return ws


class _Locked:
    """Acquires the worker lock, waiting at most `wait` seconds.

    The lock prevents connecting to a tab at the same time as a real request —
    otherwise CDP replies get mixed up (see `ClusterPool.probe_sessions`).
    """

    def __init__(self, w: WorkerTab, wait: float) -> None:
        self.w, self.wait = w, wait

    async def __aenter__(self):
        try:
            await asyncio.wait_for(self.w.lock.acquire(), timeout=self.wait)
        except asyncio.TimeoutError:
            raise ActionError(409, "busy", "Instansiya hozir so'rov bajaryapti — birozdan keyin urinib ko'ring.")
        return self.w

    async def __aexit__(self, *exc):
        self.w.lock.release()


async def _run(port: int, action: str, coro):
    """One action per port at a time; the result is stored in `LAST_ACTION`."""
    if port in _BUSY:
        raise ActionError(409, "busy", "Bu instansiyada boshqa amal bajarilyapti.")
    _BUSY.add(port)
    t0 = time.time()
    try:
        result = await coro
        LAST_ACTION[port] = {"action": action, "ts": int(t0), "ok": bool(result.get("ok", True)),
                             "summary": result.get("summary", "")}
        return result
    except ActionError as e:
        LAST_ACTION[port] = {"action": action, "ts": int(t0), "ok": False, "summary": e.message}
        raise
    except Exception as e:
        log.warning("instance action %s port=%d: %s", action, port, e)
        LAST_ACTION[port] = {"action": action, "ts": int(t0), "ok": False, "summary": type(e).__name__}
        raise ActionError(502, "action_failed", f"Amal bajarilmadi ({type(e).__name__}).")
    finally:
        _BUSY.discard(port)


async def _probe_one(w: WorkerTab, *, reloaded: bool = False) -> Dict[str, Any]:
    try:
        async with await w.connect(open_timeout=8) as ws:
            # recover=False: the probe must not reload the page itself —
            # there is a separate "reload page" action for that.
            healthy, reason = await w.adapter.check_session(ws, recover=False)
    except Exception as e:
        healthy, reason = False, f"ws:{type(e).__name__}"
    w.apply_probe(healthy, reason, reloaded=reloaded)
    return {"model": w.model_name, "ok": healthy, "reason": reason}


async def probe(pool: ClusterPool, instance: int, lock_wait: float = 20.0) -> Dict[str, Any]:
    port = _resolve(pool, instance)

    async def work():
        tabs = []
        for w in await _workers(pool, port):
            async with _Locked(w, lock_wait):
                tabs.append(await _probe_one(w))
        ok = all(t["ok"] for t in tabs)
        return {"ok": ok, "tabs": tabs,
                "summary": "sessiya joyida" if ok else ", ".join(f"{t['model']}: {t['reason']}" for t in tabs if not t["ok"])}

    return await _run(port, "probe", work())


async def reload(pool: ClusterPool, instance: int, lock_wait: float = 20.0) -> Dict[str, Any]:
    port = _resolve(pool, instance)

    async def work():
        tabs = []
        for w in await _workers(pool, port):
            url = _HOME_URLS.get(w.model_name)
            if not url:
                tabs.append({"model": w.model_name, "ok": False, "reason": "no_home_url"})
                continue
            async with _Locked(w, lock_wait):
                navigated = False
                try:
                    async with await w.connect(open_timeout=8) as ws:
                        # Plain navigation: cookies (login) are kept
                        await w.adapter.goto(ws, url)
                        navigated = await w.adapter.wait_for_input(ws, timeout=20) is not False
                except Exception as e:
                    log.info("reload %s port=%d connection dropped during navigation: %s",
                             w.model_name, port, type(e).__name__)
                # Navigation may have recreated the target — probe over a fresh connection
                await asyncio.sleep(1.0)
                tabs.append(await _probe_one(w, reloaded=navigated))
        ok = all(t["ok"] for t in tabs)
        return {"ok": ok, "tabs": tabs,
                "summary": "qayta yuklandi, sessiya joyida" if ok else
                           "qayta yuklandi, lekin: " + ", ".join(f"{t['model']}: {t['reason']}" for t in tabs if not t["ok"])}

    return await _run(port, "reload", work())


async def cleanup(pool: ClusterPool, instance: int, *, dry_run: bool, keep: int,
                  max_per_run: int = 40, lock_wait: float = 60.0) -> Dict[str, Any]:
    """Deletes chats according to the adapter policy (or only counts them with `dry_run`).

    Refused for adapters without a policy. With the "all" policy (Qwen, Gemini;
    approved by the owner) manual cleanup deletes everything (`keep=0`); the others
    use the same `keep` as the background watchdog — the rules are not loosened.
    The worker lock is held: no live request runs on the tab during cleanup.
    """
    port = _resolve(pool, instance)

    async def work():
        workers = await _workers(pool, port)
        safe = [w for w in workers if cleanup_info(w)["supported"]]
        if not safe:
            raise ActionError(409, "cleanup_unsupported",
                              "Bu instansiyada tozalashga ruxsat yo'q (shaxsiy chatlarni himoyalash uchun).")
        tabs = []
        for w in safe:
            policy = w.adapter.CLEANUP_POLICY
            k = 0 if policy == "all" else keep
            async with _Locked(w, lock_wait):
                async with await w.connect(open_timeout=8) as ws:
                    if dry_run:
                        plan = await w.adapter.plan_old_chats(ws, keep=k)
                        tabs.append({"model": w.model_name, "ok": True, "policy": policy, "keep": k,
                                     "would_delete": len(plan["delete"]), "protected": len(plan["protect"]),
                                     "skipped": len(plan["skip"]), "later": int(plan.get("later") or 0),
                                     "registry_size": plan.get("ledger_size"),
                                     "rows": plan["rows"], "complete": plan["complete"],
                                     "delete_titles": [str(r.get("title") or "")[:80] for r in plan["delete"]][:_MAX_TITLES],
                                     "protect_titles": [str(r.get("title") or "")[:80] for r in plan["protect"]][:_MAX_TITLES]})
                    else:
                        fn = w.adapter.delete_old_chats
                        kw = {"max_per_run": max_per_run} if "max_per_run" in inspect.signature(fn).parameters else {}
                        n = await fn(ws, keep=k, **kw)
                        tabs.append({"model": w.model_name, "ok": True, "policy": policy, "deleted": int(n)})
        if dry_run:
            total = sum(t["would_delete"] for t in tabs)
            summary = f"quruq sinov: {total} ta chat o'chiriladi"
        else:
            total = sum(t["deleted"] for t in tabs)
            summary = f"{total} ta chat o'chirildi"
        return {"ok": True, "dry_run": dry_run, "tabs": tabs, "summary": summary}

    return await _run(port, "cleanup_dry" if dry_run else "cleanup", work())


def _cdp_json(port: int, path: str) -> Any:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3.0) as resp:
        return json.loads(resp.read().decode())


async def focus(pool: ClusterPool, instance: int) -> Dict[str, Any]:
    """Brings the instance window to the front on the Xvfb display (for VNC).

    Uses a browser-level CDP connection — it does not touch the tab
    connection, so it does not disturb a running request and does not wait
    for the worker lock. The page is not reloaded.
    """
    port = _resolve(pool, instance)
    loop = asyncio.get_event_loop()
    try:
        version = await loop.run_in_executor(None, _cdp_json, port, "/json/version")
        tabs = await loop.run_in_executor(None, _cdp_json, port, "/json")
    except Exception as e:
        raise ActionError(409, "browser_down", f"Brauzer javob bermayapti ({type(e).__name__}).")
    pages = [t for t in tabs if t.get("type") == "page"]
    if not pages:
        raise ActionError(409, "no_tab", "Brauzerda ochiq sahifa yo'q.")
    known = {w.tab_id for w in pool.workers if w.browser_port == port}
    target = next((t for t in pages if t.get("id") in known), pages[0])
    tid = target["id"]
    async with websockets.connect(version["webSocketDebuggerUrl"], ping_interval=None,
                                  open_timeout=5) as ws:
        win = await cdp.call(ws, "Browser.getWindowForTarget", {"targetId": tid}, timeout=5)
        wid = win.get("windowId")
        if wid is not None and (win.get("bounds") or {}).get("windowState") == "minimized":
            await cdp.call(ws, "Browser.setWindowBounds",
                           {"windowId": wid, "bounds": {"windowState": "normal"}}, timeout=5)
        await cdp.call(ws, "Target.activateTarget", {"targetId": tid}, timeout=5)
    return {"ok": True, "instance": instance, "summary": "oyna oldinga chiqarildi"}
