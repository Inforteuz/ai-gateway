"""/portal/admin/instances/*: manage cluster browser instances without opening VNC.

Actions go through the cluster server (backend/cluster/instances.py): Chrome is
not restarted and login cookies are not touched. Every action is written to the
`admin_audit` table (who, when, which instance, result).

VNC: the browser connects to `/portal/admin/vnc/ws` with a one-time ticket, and
the backend connects to x11vnc itself and supplies the password (see `vnc.py`).
"""
from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Path, Request, WebSocket
from starlette.websockets import WebSocketState
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from . import audit, vnc
from .common import ApiError, client_ip, csrf_guard, get_db, optional_user, require_admin
from .db import Database
from .routes_admin import _cluster_target

log = logging.getLogger("instances")

router = APIRouter(prefix="/portal/admin/instances", dependencies=[Depends(csrf_guard), Depends(require_admin)],
                   tags=["admin"])
ws_router = APIRouter()

Instance = Path(ge=0, le=63)
VNC_MAX_SECONDS = 2 * 3600

# Background jobs (long cleanups): references are kept so the GC does not collect them
_JOBS: set[asyncio.Task] = set()


def _target(n: int) -> str:
    return f"instance:{n}"


def _cluster_error(r) -> ApiError:
    if r.status_code in (401, 403):
        return ApiError(400, "cluster_bad_key", "Cluster shlyuzi kalitni qabul qilmadi.")
    try:
        err = (r.json() or {}).get("error") or {}
    except Exception:
        err = {}
    if r.status_code in (404, 409) and err.get("message"):
        return ApiError(r.status_code, err.get("code") or "instance_error", err["message"])
    return ApiError(502, "cluster_error", f"Cluster javobi: HTTP {r.status_code}.", "server_error")


async def _cluster(request: Request, db: Database, method: str, path: str, *, json=None, timeout: float = 55.0):
    base, headers = await run_in_threadpool(_cluster_target, db)
    try:
        r = await request.app.state.cluster_http.request(method, f"{base}{path}", headers=headers, json=json,
                                                         timeout=timeout)
    except Exception as e:
        raise ApiError(502, "cluster_unreachable", f"Cluster shlyuziga ulanib bo'lmadi ({type(e).__name__}).",
                       "server_error")
    if r.status_code != 200:
        raise _cluster_error(r)
    return r.json()


async def _audited(request: Request, db: Database, admin: dict, action: str, n: int, call):
    """Runs the action and records its result (success or failure) in the audit log."""
    ip = client_ip(request)
    try:
        res = await call
    except ApiError as e:
        await run_in_threadpool(audit.record, db, admin=admin, action=action, target=_target(n), ok=False,
                                detail=f"{e.code}: {e.message}", ip=ip)
        raise
    await run_in_threadpool(audit.record, db, admin=admin, action=action, target=_target(n),
                            ok=bool(res.get("ok", True)), detail=str(res.get("summary") or ""), ip=ip)
    return res


@router.get("")
async def list_instances(request: Request, db: Database = Depends(get_db)):
    data = await _cluster(request, db, "GET", "/cluster/instances", timeout=15.0)
    data["vnc_enabled"] = bool(request.app.state.config.vnc_password)
    data["audit"] = await run_in_threadpool(audit.recent, db, target_prefix="instance:", limit=30)
    return data


@router.post("/{n}/probe")
async def probe(request: Request, n: int = Instance, admin=Depends(require_admin), db: Database = Depends(get_db)):
    return await _audited(request, db, admin, "probe", n,
                          _cluster(request, db, "POST", f"/cluster/instances/{n}/probe"))


@router.post("/{n}/reload")
async def reload(request: Request, n: int = Instance, admin=Depends(require_admin), db: Database = Depends(get_db)):
    return await _audited(request, db, admin, "reload", n,
                          _cluster(request, db, "POST", f"/cluster/instances/{n}/reload"))


class CleanupIn(BaseModel):
    dry_run: bool = True


async def _background_job(app, db: Database, admin: dict, ip: str, n: int, base: str, headers: dict, *,
                          action: str, path: str, json: dict | None, timeout: float) -> None:
    """Long-running cluster action: the result (success or failure) is recorded in the audit log."""
    ok, detail = False, ""
    try:
        r = await app.state.cluster_http.post(f"{base}/cluster/instances/{n}/{path}", headers=headers,
                                              json=json, timeout=timeout)
        if r.status_code == 200:
            res = r.json()
            ok, detail = bool(res.get("ok")), str(res.get("summary") or "")
        else:
            e = _cluster_error(r)
            detail = f"{e.code}: {e.message}"
    except Exception as e:
        detail = f"cluster_unreachable: {type(e).__name__}"
    await run_in_threadpool(audit.record, db, admin=admin, action=action, target=_target(n), ok=ok,
                            detail=detail, ip=ip)


def _start(coro) -> None:
    task = asyncio.create_task(coro)
    _JOBS.add(task)
    task.add_done_callback(_JOBS.discard)


@router.post("/{n}/cleanup")
async def cleanup(body: CleanupIn, request: Request, n: int = Instance, admin=Depends(require_admin),
                  db: Database = Depends(get_db)):
    """Cleans up according to the provider policy (decided by the cluster). `dry_run` counts what would be deleted.

    Real deletion can take several minutes (~7 s/chat), so it runs in the
    background; the result goes to the audit log and the instance card.
    """
    if body.dry_run:
        return await _audited(request, db, admin, "cleanup_dry", n,
                              _cluster(request, db, "POST", f"/cluster/instances/{n}/cleanup",
                                       json={"dry_run": True}))
    base, headers = await run_in_threadpool(_cluster_target, db)
    _start(_background_job(request.app, db, dict(admin), client_ip(request), n, base, headers,
                           action="cleanup", path="cleanup", json={"dry_run": False}, timeout=1200.0))
    return {"ok": True, "started": True, "summary": "tozalash fonda boshlandi"}


@router.post("/{n}/recover-memory")
async def recover_memory(request: Request, n: int = Instance, admin=Depends(require_admin),
                         db: Database = Depends(get_db)):
    """Reopens the tab in a new renderer process, freeing memory the renderer accumulated.

    Chrome is not restarted and cookies are not touched; the session is checked before
    and after (cluster `memory_guard.recover`). Waiting for the lock and loading the page
    can exceed a minute, so it runs in the background; the result goes to the audit log
    and the card.
    """
    base, headers = await run_in_threadpool(_cluster_target, db)
    _start(_background_job(request.app, db, dict(admin), client_ip(request), n, base, headers,
                           action="recover_memory", path="recover-memory", json=None, timeout=300.0))
    return {"ok": True, "started": True, "summary": "tabni tiklash fonda boshlandi"}


@router.post("/{n}/vnc-ticket")
async def vnc_ticket(request: Request, n: int = Instance, admin=Depends(require_admin),
                     db: Database = Depends(get_db)):
    """One-time (60 s) VNC ticket; also brings the instance window to the front."""
    if not request.app.state.config.vnc_password:
        raise ApiError(503, "vnc_disabled", "VNC sozlanmagan (SF_VNC_PASSWORD).", "server_error")
    try:
        focus = await _cluster(request, db, "POST", f"/cluster/instances/{n}/focus", timeout=15.0)
        detail = "tiket berildi; " + str(focus.get("summary") or "")
    except ApiError as e:
        # VNC opens even if the window could not be raised: that is exactly when it is needed
        detail = f"tiket berildi; oyna oldinga chiqmadi ({e.code})"
    token = request.app.state.vnc_tickets.issue(admin["id"], n)
    await run_in_threadpool(audit.record, db, admin=admin, action="vnc_open", target=_target(n), ok=True,
                            detail=detail, ip=client_ip(request))
    return {"ticket": token, "expires_in": vnc.TICKET_TTL, "path": "/portal/admin/vnc/ws"}


@router.get("/audit")
def audit_log(limit: int = 50, db: Database = Depends(get_db)):
    return {"items": audit.recent(db, target_prefix="instance:", limit=max(1, min(limit, 200)))}


def _same_origin(ws: WebSocket) -> bool:
    """A page on another site must not connect with the admin cookie (CSWSH)."""
    origin, host = ws.headers.get("origin"), ws.headers.get("host")
    return bool(origin and host and urlparse(origin).netloc == host)


@ws_router.websocket("/portal/admin/vnc/ws")
async def vnc_ws(websocket: WebSocket, ticket: str = ""):
    app = websocket.app
    cfg, db = app.state.config, app.state.db
    if not cfg.vnc_password or not _same_origin(websocket):
        await websocket.close(code=1008)
        return
    user = await run_in_threadpool(optional_user, websocket, db)
    if not user or user["role"] != "admin":
        await websocket.close(code=1008)
        return
    t = app.state.vnc_tickets.consume(ticket, user["id"])
    if t is None:
        await websocket.close(code=1008)
        return
    proto = "binary" if "binary" in websocket.scope.get("subprotocols", []) else None
    await websocket.accept(subprotocol=proto)
    ip = websocket.client.host if websocket.client else "-"
    t0, ok, detail = time.time(), False, ""
    writer = None
    try:
        reader, writer = await vnc.connect_upstream(cfg.vnc_host, cfg.vnc_port, cfg.vnc_password)
        leftover = await vnc.client_handshake(websocket)
        ok = True
        await vnc.pipe(websocket, reader, writer, leftover, VNC_MAX_SECONDS)
    except (vnc.VncError, OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as e:
        detail = f"{type(e).__name__}: {e}"
        log.warning("vnc session instance=%d: %s", t.instance, detail)
    finally:
        if writer is not None:
            writer.close()
        # Synchronous write: the task may be cancelled when the browser disconnects, and an
        # `await` here would be interrupted, losing the audit entry. One INSERT per session.
        audit.record(db, admin=dict(user), action="vnc_session", target=_target(t.instance), ok=ok,
                     detail=(detail or f"{int(time.time() - t0)} s davom etdi"), ip=ip)
        if websocket.client_state == WebSocketState.CONNECTED:
            try:
                await websocket.close()
            except Exception:
                pass
