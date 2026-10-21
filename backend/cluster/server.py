"""Cluster gateway: an OpenAI-compatible API server.

The gateway backend connects to this server over HTTP (usually loopback 127.0.0.1:8800).
Standalone run: `python -m cluster.server` (or `python -m backend.cluster.server`).
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from . import attachments as attach
from . import instances as inst
from . import cache as response_cache
from . import logs, metrics
from . import output_format
from . import reasoning
from . import runmeta
from . import tools as tool_proto
from . import settings as cfg
from .balancer import ClusterBalancer, DispatchError
from .compose import compile_prompt, mask_brand, tighten_filename_fences
from .pool import ClusterPool
from . import proxy_health
from . import self_heal
from . import memory_guard as mg
from .proxies import auto_refresh as proxy_auto_refresh, get_pool as get_proxy_pool

log = logs.get("server")


class ChatMessage(BaseModel):
    role: str
    # OpenAI multimodal: `content` may be a string OR an array of parts
    # ([{"type":"text"...},{"type":"image_url"...},{"type":"file"...}]).
    content: Any = ""
    # Tool calling: tools the assistant called, and the owner of a tool result
    tool_calls: Optional[List[dict]] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    model: Optional[str] = "auto"
    messages: List[ChatMessage]
    temperature: Optional[float] = 0.7
    stream: Optional[bool] = False
    max_tokens: Optional[int] = None
    # Coding agent / subagent protocol
    tools: Optional[List[dict]] = None
    tool_choice: Any = "auto"
    # "none" disables structured reasoning (when speed matters)
    reasoning_effort: Optional[str] = None
    # {"type": "json_object"} | {"type": "json_schema", "json_schema": {...}}.
    # Must be declared, otherwise pydantic silently drops it.
    response_format: Optional[dict] = None
    # INTERNAL fields (sent only by the gateway/measurement tools; the gateway strips them from client input):
    # sf_allow_fallback: allow the provider-side fallback model on "high demand" (admin setting);
    # sf_force_variant: an exact model variant, for measurements.
    sf_allow_fallback: bool = False
    sf_force_variant: Optional[str] = None


class ClusterError(Exception):
    """An error returned to the client as an OpenAI-style `{"error": {...}}`.

    `message` is visible to the client, so it must not contain provider names,
    ports or internal error text (those go to the log only).
    """

    def __init__(self, status: int, code: str, message: str,
                 retry_after: Optional[int] = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.retry_after = status, code, message, retry_after


def _as_cluster_error(e: BaseException) -> ClusterError:
    if isinstance(e, ClusterError):
        return e
    if isinstance(e, DispatchError):
        return ClusterError(e.status, e.code, str(e), e.retry_after)
    if isinstance(e, HTTPException):
        return ClusterError(e.status_code, "invalid_request" if e.status_code < 500 else "server_error",
                            str(e.detail))
    log.exception("unexpected error", exc_info=e)
    return ClusterError(500, "internal_error", "Ichki xato. Birozdan keyin qayta urinib ko'ring.")


def _error_body(err: ClusterError) -> dict:
    kind = "invalid_request_error" if err.status < 500 else "server_error"
    return {"error": {"message": err.message, "type": kind, "code": err.code}}


def _error_response(err: ClusterError) -> JSONResponse:
    headers = {"Retry-After": str(err.retry_after)} if err.retry_after else None
    return JSONResponse(_error_body(err), status_code=err.status, headers=headers)


def _auth(authorization: Optional[str] = Header(None)) -> None:
    """Bearer token check. With an empty API_KEY auth is open (local dev only)."""
    expected = cfg.API_KEY
    if not expected:
        return
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="API kalit berilmagan")
    token = authorization[7:].strip()
    if token != expected:
        raise HTTPException(status_code=401, detail="API kalit noto'g'ri")


async def session_watchdog(app: FastAPI, interval: int = 300) -> None:
    """Check worker sessions every `interval` seconds.

    Sends no prompt, only inspects the DOM, so it does not consume providers'
    daily quota. Workers that lost their login are flagged right away so real
    requests do not land on them.
    """
    await asyncio.sleep(30)  # give startup a moment
    while True:
        try:
            pool: ClusterPool = app.state.pool
            problems = await pool.probe_sessions()
            if problems:
                log.warning("session watchdog: %d problematic workers: %s",
                            len(problems), problems)
        except Exception as e:
            log.error("session watchdog error: %s", e)
        await asyncio.sleep(interval)


async def chat_cleanup_watchdog(app: FastAPI, interval: int) -> None:
    """Regularly delete old chats that pile up at the providers.

    If chats are kept, the list grows without bound (111 chats were found on one
    account) and stored conversations feed the provider's "memory", which raises
    the risk of mixing users' contexts.
    """
    await asyncio.sleep(120)  # let other startup tasks go first
    while True:
        try:
            pool: ClusterPool = app.state.pool
            for w in list(pool.workers):
                if not w.adapter.caps.delete_chats:
                    continue
                if w.lock.locked() or time.time() < w.cooldown_until:
                    continue
                async with w.lock:
                    try:
                        async with await w.connect(open_timeout=8) as ws:
                            # The adapter may set its own limit (Gemini: 0, every listed chat is surplus)
                            keep = getattr(w.adapter, "CLEANUP_KEEP", None)
                            n = await w.adapter.delete_old_chats(
                                ws, keep=cfg.CHAT_CLEANUP_KEEP if keep is None else keep)
                        if n:
                            log.info("chat cleanup: deleted %d chats from %s", n, w.model_name)
                    except Exception as e:
                        log.warning("chat cleanup %s: %s", w.model_name, e)
                await asyncio.sleep(1.0)
        except Exception as e:
            log.error("chat cleanup watchdog error: %s", e)
        await asyncio.sleep(interval)



# Proxy health watchdog. When a plan expires or a proxy stops working, the
# tinyproxy config is rewritten and SIGHUP is sent; Chrome keeps running and the
# provider session is preserved. The result is served at `/cluster/proxy-health`;
# the API side reads it and sends a Telegram alert.
# (The dict keys are part of that JSON contract and stay as they are.)
PROXY_HEALTH: dict = {"ts": 0, "natijalar": [], "oxirgi_xato": None}


async def proxy_health_watchdog(app: FastAPI, interval: float) -> None:
    await asyncio.sleep(20)  # wait for the scan to finish
    while True:
        try:
            # instance number = CDP port - 9222 (launch-chrome.sh convention:
            # ai-gateway-chrome@N -> CDP 9222+N, tinyproxy 16222+N)
            instances = sorted({p - 9222 for p in cfg.CDP_PORTS if p > 9222})
            results = await asyncio.get_event_loop().run_in_executor(
                None, proxy_health.check_and_repair, instances)
            PROXY_HEALTH["ts"] = int(time.time())
            PROXY_HEALTH["natijalar"] = [
                {"instansiya": n.instance, "port": n.port, "ishlayapti": n.working,
                 "ip": n.ip, "xato": n.error, "almashtirildi": n.replaced}
                for n in results
            ]
            PROXY_HEALTH["oxirgi_xato"] = None
            swapped = [n.instance for n in results if n.replaced]
            if swapped:
                log.warning("proxy watchdog: swapped instances %s", swapped)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            PROXY_HEALTH["oxirgi_xato"] = f"{type(e).__name__}: {e}"
            log.error("proxy health watchdog error: %s", e)
        await asyncio.sleep(interval)



# Self-heal watchdog. If Chrome freezes (the port listens but CDP does not
# answer), the instance is restarted automatically. Human intervention is
# requested only when it fails to recover on its own.
# (The dict keys are part of the `/cluster/self-heal` JSON contract.)
SELF_HEAL: dict = {"ts": 0, "hodisalar": [], "xulosa": {}}


async def self_heal_watchdog(app: FastAPI, interval: float) -> None:
    await asyncio.sleep(25)
    while True:
        try:
            events = await asyncio.get_event_loop().run_in_executor(
                None, self_heal.run_cycle, list(cfg.CDP_PORTS))
            SELF_HEAL["ts"] = int(time.time())
            SELF_HEAL["xulosa"] = self_heal.summary()
            if events:
                SELF_HEAL["hodisalar"] = (events + SELF_HEAL["hodisalar"])[:40]
                # Rescan the recovered instance right away; otherwise the
                # worker stays "missing" until the next scan.
                try:
                    await app.state.pool.scan(force=True)
                except Exception:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("self-heal watchdog error: %s", e)
        await asyncio.sleep(interval)


async def memory_guard_watchdog(app: FastAPI, interval: float) -> None:
    """When renderer memory exceeds the limit, reopen an idle worker's tab in a new
    renderer (see `memory_guard.py`). Chrome itself is restarted only on the Gemini
    instance (#9), where reopening the tab does not free the memory."""
    await asyncio.sleep(90)  # let the scan and other watchdogs settle
    while True:
        try:
            await mg.run_once(app.state.pool, inst._BUSY)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("memory watchdog error: %s", e)
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logs.configure()
    log.info("cluster starting ports=%s auth=%s", cfg.CDP_PORTS, "on" if cfg.API_KEY else "off")
    app.state.pool = ClusterPool(cfg.CDP_PORTS)
    app.state.balancer = ClusterBalancer(app.state.pool)
    get_proxy_pool()  # load from file
    tasks = [asyncio.create_task(session_watchdog(app, cfg.SESSION_PROBE_INTERVAL))]
    if cfg.CHAT_CLEANUP_ENABLED:
        tasks.append(asyncio.create_task(
            chat_cleanup_watchdog(app, cfg.CHAT_CLEANUP_INTERVAL)))
    if cfg.SELF_HEAL_INTERVAL:
        tasks.append(asyncio.create_task(
            self_heal_watchdog(app, cfg.SELF_HEAL_INTERVAL)))
    if cfg.PROXY_HEALTH_INTERVAL:
        tasks.append(asyncio.create_task(
            proxy_health_watchdog(app, cfg.PROXY_HEALTH_INTERVAL)))
    if cfg.MEM_GUARD_INTERVAL:
        tasks.append(asyncio.create_task(
            memory_guard_watchdog(app, cfg.MEM_GUARD_INTERVAL)))
    if cfg.PROXY_LIST_URL and cfg.PROXY_REFRESH_INTERVAL:
        tasks.append(asyncio.create_task(proxy_auto_refresh(cfg.PROXY_LIST_URL, cfg.PROXY_REFRESH_INTERVAL)))
    await app.state.pool.scan(force=True)
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        log.info("cluster shutting down")


app = FastAPI(
    title="AI Gateway Cluster",
    version="2.0.0",
    description="Exposes several web-chat providers through an OpenAI-compatible API.",
    lifespan=lifespan,
    docs_url=None, redoc_url=None, openapi_url=None,
)


@app.exception_handler(ClusterError)
async def _cluster_error_handler(request: Request, exc: ClusterError):
    return _error_response(exc)


@app.exception_handler(DispatchError)
async def _dispatch_error_handler(request: Request, exc: DispatchError):
    return _error_response(_as_cluster_error(exc))


@app.exception_handler(inst.ActionError)
async def _instance_error_handler(request: Request, exc: inst.ActionError):
    return _error_response(ClusterError(exc.status, exc.code, exc.message))


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    rid = "clu_" + uuid.uuid4().hex[:16]
    logs.set_request_id(rid)
    t0 = time.monotonic()
    try:
        response = await call_next(request)
    except Exception as e:
        log.exception("unhandled %s %s", request.method, request.url.path)
        return JSONResponse({"error": {"message": str(e), "type": "server_error"}}, status_code=500)
    finally:
        logs.set_request_id("-")
    response.headers["X-Cluster-Request-Id"] = rid
    log.info("%s %s %s %dms", request.method, request.url.path, response.status_code,
             int((time.monotonic() - t0) * 1000))
    return response


# --- health and status endpoints ---------------------------------------------

@app.get("/health")
async def health(request: Request):
    """Simple liveness probe: fast, no auth. Returns the worker count."""
    pool: ClusterPool = request.app.state.pool
    return {"status": "ok", "workers": len(pool.workers), "last_scan": int(pool._last_scan)}


@app.get("/cluster/status", dependencies=[Depends(_auth)])
async def cluster_status(request: Request):
    pool: ClusterPool = request.app.state.pool
    balancer: ClusterBalancer = request.app.state.balancer
    await pool.scan(force=False)
    return {
        "status": "ok",
        "health": pool.health(),
        "workers": pool.summary(),
        "circuits": balancer.circuits.snapshot(),
    }


@app.post("/cluster/probe", dependencies=[Depends(_auth)])
async def cluster_probe(request: Request):
    """Check sessions right away (without sending a prompt)."""
    pool: ClusterPool = request.app.state.pool
    await pool.scan(force=True)
    problems = await pool.probe_sessions()
    return {
        "checked": len(pool.workers),
        "problems": problems,
        "healthy": len(pool.workers) - len(problems),
    }


@app.get("/cluster/cache", dependencies=[Depends(_auth)])
async def cache_stats():
    return response_cache.get().snapshot() if cfg.CACHE_ENABLED else {"enabled": False}


@app.post("/cluster/cache/clear", dependencies=[Depends(_auth)])
async def cache_clear():
    n = response_cache.get().clear() if cfg.CACHE_ENABLED else 0
    return {"cleared": n}


@app.get("/cluster/metrics", dependencies=[Depends(_auth)])
async def cluster_metrics():
    return metrics.get().snapshot()


@app.get("/cluster/circuits", dependencies=[Depends(_auth)])
async def cluster_circuits(request: Request):
    balancer: ClusterBalancer = request.app.state.balancer
    return {"circuits": balancer.circuits.snapshot()}


@app.post("/cluster/refresh", dependencies=[Depends(_auth)])
async def cluster_refresh(request: Request):
    pool: ClusterPool = request.app.state.pool
    await pool.scan(force=True)
    return {"status": "refreshed", "workers": len(pool.workers)}


# --- instance management (admin panel) ----------------------------------------
# Chrome is not restarted and cookies are not touched; see `instances.py`.

class CleanupBody(BaseModel):
    dry_run: bool = True


@app.get("/cluster/instances", dependencies=[Depends(_auth)])
async def instances_list(request: Request):
    pool: ClusterPool = request.app.state.pool
    await pool.scan(force=False)
    items = inst.snapshot(pool)
    mem = mg.report(pool)
    for it in items:
        it["memory"] = mem["instances"].get(it["instance"])
    return {"ts": int(time.time()), "instances": items, "system": mem["system"], "memory_guard": mem["config"]}


@app.post("/cluster/instances/{n}/probe", dependencies=[Depends(_auth)])
async def instance_probe(n: int, request: Request):
    """Check the session: sends no prompt, does not reload the page."""
    return await inst.probe(request.app.state.pool, n)


@app.post("/cluster/instances/{n}/reload", dependencies=[Depends(_auth)])
async def instance_reload(n: int, request: Request):
    """Navigate the tab to the provider home page (login cookies are kept)."""
    return await inst.reload(request.app.state.pool, n)


@app.post("/cluster/instances/{n}/cleanup", dependencies=[Depends(_auth)])
async def instance_cleanup(n: int, request: Request, body: Optional[CleanupBody] = None):
    """Chat cleanup per adapter policy (see `instances.cleanup`). Dry run by default."""
    dry = True if body is None else body.dry_run
    # Manual: at most 150 per run (~5 s/chat), since the admin triggered it deliberately
    return await inst.cleanup(request.app.state.pool, n, dry_run=dry, keep=cfg.CHAT_CLEANUP_KEEP,
                              max_per_run=150)


@app.post("/cluster/instances/{n}/recover-memory", dependencies=[Depends(_auth)])
async def instance_recover_memory(n: int, request: Request):
    """Reopen the tab in a new renderer (frees memory). The session is checked before and after."""
    pool: ClusterPool = request.app.state.pool
    port = inst._resolve(pool, n)
    if port in inst._BUSY:
        raise inst.ActionError(409, "busy", "Bu instansiyada boshqa amal bajarilyapti.")

    async def job():
        await pool.scan(force=True)
        try:
            return await mg.recover(pool, port, auto=False, lock_wait=30.0)
        except mg.Skip as e:
            raise inst.ActionError(409, e.code, e.message)

    return await inst._run(port, "recover_memory", job())


@app.get("/cluster/memory", dependencies=[Depends(_auth)])
async def memory_status(request: Request, after: int = 0):
    """Memory guard: per-instance renderer memory, server swap and events (`id > after`)."""
    return {"ts": int(time.time()), **mg.report(request.app.state.pool), "events": mg.events_since(after)}


@app.post("/cluster/instances/{n}/focus", dependencies=[Depends(_auth)])
async def instance_focus(n: int, request: Request):
    """Bring this instance's window to the front when VNC is opened."""
    return await inst.focus(request.app.state.pool, n)


@app.get("/cluster/self-heal")
async def self_heal_status():
    """Self-heal status. A non-empty `qolga_muhtoj` means a human is needed."""
    return {"ts": SELF_HEAL["ts"], "interval": cfg.SELF_HEAL_INTERVAL,
            "hodisalar": SELF_HEAL["hodisalar"][:12], **SELF_HEAL["xulosa"]}


@app.get("/cluster/proxy-health")
async def proxy_health_status():
    """Latest proxy watchdog result. No secrets, only IPs and status."""
    n = PROXY_HEALTH["natijalar"]
    return {
        "ts": PROXY_HEALTH["ts"],
        "interval": cfg.PROXY_HEALTH_INTERVAL,
        "jami": len(n),
        "ishlayapti": sum(1 for x in n if x["ishlayapti"]),
        "almashtirildi": [x["instansiya"] for x in n if x["almashtirildi"]],
        "natijalar": n,
        "oxirgi_xato": PROXY_HEALTH["oxirgi_xato"],
    }


# --- proxy pool ---------------------------------------------------------------

@app.get("/cluster/proxies", dependencies=[Depends(_auth)])
async def proxies_status():
    pool = get_proxy_pool()
    return {
        "summary": pool.summary(),
        "configured_url_set": bool(cfg.PROXY_LIST_URL),
        "refresh_interval": cfg.PROXY_REFRESH_INTERVAL,
        "direct_slots": cfg.PROXY_DIRECT_SLOTS,
    }


class ProxyRefreshBody(BaseModel):
    url: Optional[str] = None


@app.post("/cluster/proxies/refresh", dependencies=[Depends(_auth)])
async def proxies_refresh(body: Optional[ProxyRefreshBody] = None):
    url = (body.url if body else None) or cfg.PROXY_LIST_URL
    if not url:
        raise HTTPException(status_code=400, detail="Proxy URL berilmagan va SF_CLUSTER_PROXY_URL ham bo'sh")
    pool = get_proxy_pool()
    count = await asyncio.get_event_loop().run_in_executor(None, pool.fetch, url)
    return {"status": "ok" if not pool.last_error else "error",
            "count": count, "error": pool.last_error, "source": pool.last_source}


def _caps_of(workers: list) -> dict[str, bool]:
    """Capabilities of a model: `true` if at least one worker supports it."""
    keys = ("tool_calling", "vision", "files", "image_gen", "temporary_chat")
    return {k: any(getattr(w.adapter.caps, k, False) for w in workers) for k in keys}


# --- Request parsing ----------------------------------------------------------

def _prepare_request(messages: List[ChatMessage], store: attach.AttachmentStore,
                     tools: Optional[List[dict]] = None, tool_choice: Any = "auto"
                     ) -> tuple[str, list[attach.Attachment], list[str], list[str]]:
    """Extract prompt, attachments, required capabilities and tool names from messages.

    Returns (prompt, attachments, required capabilities, tool names).
    """
    flat: list[dict[str, Any]] = []
    found: list[attach.Attachment] = []
    for m in messages:
        parsed = attach.parse_message_content(m.content, store)
        found.extend(parsed.attachments)
        item: dict[str, Any] = {"role": m.role, "content": parsed.text}
        # Tool protocol fields must reach compile_prompt
        if m.tool_calls:
            item["tool_calls"] = m.tool_calls
        if m.name:
            item["name"] = m.name
        if m.tool_call_id:
            item["tool_call_id"] = m.tool_call_id
        if parsed.text or parsed.attachments or m.tool_calls:
            flat.append(item)

    require: list[str] = []
    if any(a.is_image for a in found):
        require.append("vision")
    if any(not a.is_image for a in found):
        require.append("files")

    instruction = ""
    tool_names: list[str] = []
    if tools:
        require.append("tool_calling")
        instruction = tool_proto.build_instruction(tools, tool_choice)
        for t in tools:
            fn = (t or {}).get("function") or t or {}
            if fn.get("name"):
                tool_names.append(str(fn["name"]))

    prompt = compile_prompt(flat, attachments_note=attach.describe(found),
                            tools_instruction=instruction)
    return prompt, found, require, tool_names


# Keywords for detecting an image request; they decide whether to look for
# images in the answer (avoids needless DOM scans).
_IMAGE_REQUEST_HINTS = (
    "rasm chiz", "rasm yarat", "rasm generat", "surat chiz", "surat yarat",
    "draw ", "generate an image", "generate image", "create an image",
    "make an image", "picture of", "нарисуй", "сгенерируй изображ", "создай изображ",
)


# A request to EDIT an attached image; the result is an image too.
# Only checked when the request carries an image (an edit verb alone means nothing).
_IMAGE_EDIT_HINTS = (
    # Only explicit EDIT verbs. Broad words like "rasmdagi" ("in the image") or "orqa fon"
    # ("background") also caught image ANALYSIS requests and caused a 110 s image wait.
    "tahrirla", "fonini o'zgartir", "fonni o'zgartir", "fonini almashtir", "fonni almashtir",
    "fonini olib tashla", "fonni olib tashla", "rasmni o'zgartir", "suratni o'zgartir",
    "rasmga qo'shib ber", "rasmdan olib tashla", "rasmni tahrir", "suratni tahrir",
    "edit this image", "edit the image", "edit this photo", "edit the photo",
    "change the background", "remove the background", "replace the background",
    "отредактируй", "измени фон", "убери фон", "замени фон", "дорисуй", "измени изображение",
    "измени фото",
)


def _norm_apos(text: str) -> str:
    return (text or "").lower().translate({ord(c): "'" for c in "‘’ʻʼ`"})


def _last_user_text(messages) -> str:
    """Text of the last `user` message (only the text parts if multimodal)."""
    for m in reversed(messages or []):
        if getattr(m, "role", "") != "user":
            continue
        c = m.content
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return " ".join(str(p.get("text") or "") for p in c
                            if isinstance(p, dict) and p.get("type") in ("text", "input_text"))
        return ""
    return ""


def _model_can(pool, requested: str, cap: str) -> bool:
    """Does the requested model (or, for `auto`, any worker) have this capability?"""
    workers = pool.workers if requested in ("", "auto") else pool.workers_for_model(requested)
    return any(getattr(w.adapter.caps, cap, False) for w in workers)


def _wants_image(prompt: str) -> bool:
    low = _norm_apos(prompt)
    return any(h in low for h in _IMAGE_REQUEST_HINTS)


def _wants_image_edit(prompt: str, has_image: bool) -> bool:
    if not has_image:
        return False
    low = _norm_apos(prompt)
    return any(h in low for h in _IMAGE_EDIT_HINTS)


# --- OpenAI-compatible endpoints ----------------------------------------------

@app.get("/v1/models", dependencies=[Depends(_auth)])
async def list_models(request: Request):
    pool: ClusterPool = request.app.state.pool
    await pool.scan(force=False)
    now = int(time.time())
    data = []
    for model_name in cfg.DEFAULT_PRIORITY_ORDER:
        workers = pool.workers_for_model(model_name)
        available = sum(1 for w in workers if w.is_available())
        data.append({
            "id": model_name,
            "object": "model",
            "created": now,
            "owned_by": "ai-gateway-cluster",
            "active_workers": len(workers),
            "available_workers": available,
            "capabilities": _caps_of(workers),
        })
    data.append({
        "id": "auto",
        "object": "model",
        "created": now,
        "owned_by": "ai-gateway-cluster",
        "active_workers": len(pool.workers),
        "available_workers": sum(1 for w in pool.workers if w.is_available()),
    })
    return {"object": "list", "data": data}


@app.post("/v1/chat/completions", dependencies=[Depends(_auth)])
async def chat_completions(req: ChatCompletionRequest, request: Request):
    if not req.messages:
        raise HTTPException(status_code=400, detail="`messages` ro'yxati bo'sh bo'lmasligi kerak")

    balancer: ClusterBalancer = request.app.state.balancer
    requested = (req.model or "auto").lower()
    try:
        spec = output_format.parse(req.response_format)
    except output_format.FormatError as e:
        raise ClusterError(400, "invalid_response_format", str(e))

    # The response ID is created up front: for streaming, the first SSE chunk
    # is sent BEFORE the answer is ready.
    res_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created_ts = int(time.time())

    # Attachments live on disk for the duration of the request. On the streaming
    # path the work runs in a background task, so the store is managed by hand
    # instead of `with`: files must not be deleted before the task finishes.
    store = attach.AttachmentStore()
    store.__enter__()
    try:
        prompt_text, files, require, tool_names = _prepare_request(
            req.messages, store, req.tools, req.tool_choice)
    except attach.AttachmentError as e:
        store.__exit__(None, None, None)
        raise HTTPException(status_code=400, detail=str(e))
    except BaseException:
        store.__exit__(None, None, None)
        raise

    async def run(sse: bool):
        # `prompt_text` and `require` are reassigned below; without `nonlocal`
        # Python treats them as locals and raises UnboundLocalError on read.
        nonlocal prompt_text, require
        try:
            t_start = time.monotonic()
            run_meta = runmeta.start(allow_fallback=bool(req.sf_allow_fallback),
                                     force_variant=(req.sf_force_variant or None),
                                     effort=(req.reasoning_effort or None))
            # Image intent is detected ONLY from the last user message. Checking the whole
            # prompt (an agent CLI's 12,000-char system instruction, history) made any
            # request look like an image request because of the word "draw ", and models
            # without image generation returned 400 unsupported_feature.
            user_text = _last_user_text(req.messages)
            want_images = (_wants_image(user_text)
                           or _wants_image_edit(user_text, any(a.is_image for a in files)))
            if want_images and not _model_can(request.app.state.pool, requested, "image_gen"):
                # The intent is a guess (from keywords): for a model that cannot draw, the
                # request is not rejected but runs as a plain text request.
                want_images = False
            if want_images:
                require = [*require, "image_gen"]

            # Structured reasoning: for hard questions, plan first, then answer.
            # Not applied to requests with tool calling, image generation,
            # ATTACHMENTS or a strict output format (`response_format`): the two
            # formats interfere and the model breaks both. Code and textual
            # demands like "JSON only" are excluded by `reasoning.should_use`.
            use_reasoning = (
                cfg.REASONING_YAML and not tool_names and not want_images and not files
                and not spec.is_json
                and (req.reasoning_effort or "").lower() != "none"
                and reasoning.should_use(prompt_text, cfg.REASONING_MIN_CHARS))
            base_prompt = prompt_text
            if spec.is_json:
                base_prompt = base_prompt + "\n\n" + output_format.instruction(spec)
            prompt_text = base_prompt + ("\n\n" + reasoning.INSTRUCTION if use_reasoning else "")

            # Cache: an identical request is not sent to the browser again. The key
            # covers the prompt (system prompt, history, attachments), the config
            # version and every parameter that affects the answer.
            # A forced variant (`sf_force_variant`, measurements) bypasses the cache:
            # it is not part of the key, so otherwise the primary model's cached
            # answer would be returned as the other variant's answer.
            cache = response_cache.get() if cfg.CACHE_ENABLED and not req.sf_force_variant else None
            cache_key = ""
            if cache is not None:
                cache_key = response_cache.make_key(
                    requested, prompt_text,
                    [response_cache.file_digest(a.path) for a in files],
                    params={
                        "temperature": req.temperature, "max_tokens": req.max_tokens,
                        "response_format": spec.cache_token(),
                        "tools": req.tools, "tool_choice": req.tool_choice,
                        "reasoning_effort": req.reasoning_effort, "reasoning": use_reasoning,
                    })
                hit = cache.get(cache_key)
                if hit is not None and not _cacheable(hit.answer, hit.images, [], spec):
                    # Guard: never return an invalid entry written by older logic
                    log.warning("removed invalid cache entry model=%s", hit.model or requested)
                    cache.purge(lambda e, _h=hit: e is _h)
                    hit = None
                if hit is not None:
                    log.info("cache hit model=%s hits=%d", hit.model or requested, hit.hits)
                    return _completion_payload(
                        answer=hit.answer, images=hit.images, used_model=hit.model or requested,
                        prompt_text=prompt_text, stream=sse, fingerprint="cache", cached=True, tool_names=tool_names,
                        sse_parts=sse, res_id=res_id, created_ts=created_ts)

            # With attachments the model has to read the file, so allow more time
            timeout = cfg.REQUEST_TIMEOUT + (cfg.ATTACHMENT_EXTRA_TIMEOUT if files else 0)

            async def call(text: str):
                return await balancer.execute(
                    prompt_text=text,
                    requested_model=requested,
                    timeout_per_worker=timeout,
                    require=require,
                    attachment_paths=[a.path for a in files],
                    images_only=bool(files) and all(a.is_image for a in files),
                    want_images=want_images,
                )

            def can_retry() -> bool:
                # A retry must not exceed the overall deadline (the gateway is waiting too)
                return time.monotonic() - t_start + timeout + 15 < cfg.REQUEST_DEADLINE

            answer, used_model, worker, images = await call(prompt_text)

            # With structured reasoning only the `answer` part goes to the user;
            # the plan and self-check stay internal. If the template is broken the
            # answer is not shown: ask once more WITHOUT the template.
            if use_reasoning:
                extracted, meta = reasoning.split_answer(answer)
                if meta:
                    log.info("reasoning plan=%d steps", len(meta.get("plan") or []))
                if not extracted.strip() and not images:
                    if not can_retry():
                        raise ClusterError(502, "invalid_output",
                                           "Model yaroqli javob qaytarmadi. Qayta urinib ko'ring.", 10)
                    log.warning("reasoning template invalid, re-asking without template")
                    answer, used_model, worker, images = await call(base_prompt)
                else:
                    answer = extracted

            # Extract tool calls first so their arguments (file paths, code) are NOT
            # brand-masked; only the remaining text is masked afterwards.
            calls: list[dict] = []
            if tool_names:
                calls, answer = tool_proto.parse_tool_calls(answer, tool_names)
            answer = tighten_filename_fences(mask_brand(answer))

            # Requested format: validate; if invalid, re-ask once; if still
            # invalid, return an ERROR to the client.
            # When tool calls came back, the text format is not checked.
            if spec.is_json and not calls:
                ok, answer_fmt, why = output_format.normalize(answer, spec)
                if not ok and can_retry():
                    log.warning("format mismatch (%s), re-asking", why)
                    answer, used_model, worker, images = await call(
                        prompt_text + "\n\n" + output_format.retry_instruction(spec, why))
                    answer = mask_brand(answer)
                    ok, answer_fmt, why = output_format.normalize(answer, spec)
                if not ok:
                    log.warning("format mismatch even after retry: %s", why)
                    raise ClusterError(502, "invalid_output",
                                       "Model talab qilingan JSON formatida javob bermadi. "
                                       "Qayta urinib ko'ring.", 10)
                answer = answer_fmt

            # Answers from the fallback model are not cached: the next identical request
            # should be answered by the primary model once it frees up.
            fallback = bool(run_meta.get("fallback")) or bool(run_meta.get("force_variant"))
            if cache is not None and cache_key and not fallback and _cacheable(answer, images, calls, spec):
                cache.put(cache_key, answer, images, model=used_model)

            log.info("answer model=%s upstream=%s effort=%s fallback=%s phases=%s time=%.1fs reasoning=%s format=%s tool_calls=%d",
                     used_model, run_meta.get("upstream_model") or "-", run_meta.get("upstream_effort") or "-",
                     bool(run_meta.get("fallback")),
                     ",".join(f"{k}:{v}" for k, v in (run_meta.get("phases") or {}).items()) or "-",
                     time.monotonic() - t_start, use_reasoning, spec.kind, len(calls))
            return _completion_payload(
                answer=answer, images=images, used_model=used_model, prompt_text=prompt_text,
                stream=sse,
                fingerprint=f"{worker.browser_port}-{worker.short_tab}", cached=False,
                tool_names=tool_names, calls=calls, sse_parts=sse, res_id=res_id,
                created_ts=created_ts, fallback=bool(run_meta.get("fallback")))
        finally:
            store.__exit__(None, None, None)

    if req.stream:
        # Preflight: wait a little before sending headers. An error detected in
        # this window (unsupported, queue full, no model, cache hit) is returned
        # with the proper HTTP status, as for a non-streaming response, instead
        # of a stream that starts with 200 and carries the error as text.
        task = asyncio.ensure_future(run(True))
        done, _ = await asyncio.wait({task}, timeout=cfg.STREAM_PREFLIGHT_SECONDS)
        if done and task.exception() is not None:
            raise _as_cluster_error(task.exception())
        return StreamingResponse(
            _stream_with_keepalive(task, res_id, created_ts, requested),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    return await run(False)


def _cacheable(answer: str, images: list, calls: list,
               spec: "output_format.FormatSpec") -> bool:
    """May this answer be cached (or served from the cache)?

    An invalid answer (e.g. an empty YAML plan template) must not come back on
    the next request with `cached: true`. So before storing:
      - empty answers are not stored;
      - tool calls are not stored (so a repeated call in an agent loop is not
        "confirmed" from the cache);
      - answers not matching the requested format are not stored;
      - answers with reasoning-template residue are not stored.
    """
    if calls:
        return False
    if not (answer or "").strip():
        return bool(images)
    if spec.is_json and not output_format.normalize(answer, spec)[0]:
        return False
    return not reasoning.is_template_residue(answer)


# Keep a streaming connection alive until the answer is ready.
# If an instance crashed and is recovering, this can take minutes, and the client
# (a coding agent) drops a silent connection. An SSE comment (": ...") is invisible
# and harmless to OpenAI clients; on long waits a `reasoning_content` delta is also
# sent so the user sees a "thinking" indicator.
KEEPALIVE_INTERVAL = 8.0
THINKING_AFTER = 24.0


def _sse_chunks(base: dict, calls: list, answer: str, usage: dict, extra: Optional[dict] = None) -> list[str]:
    """Turn the answer into SSE `data:` chunks (everything after the role delta)."""
    out: list[str] = []
    if calls:
        # Tool calls are not split into chunks: agents expect complete JSON
        out.append(f"data: {json.dumps({**base, 'choices': [{'index': 0, 'delta': {'tool_calls': calls}, 'finish_reason': None}]}, ensure_ascii=False)}\n\n")
        out.append(f"data: {json.dumps({**base, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}], **(extra or {})})}\n\n")
        out.append(f"data: {json.dumps({**base, 'choices': [], 'usage': usage})}\n\n")
        out.append("data: [DONE]\n\n")
        return out

    # The answer arrives in full after DOM polling; send it in pieces
    step = 60
    for i in range(0, len(answer), step):
        piece = answer[i:i + step]
        out.append(f"data: {json.dumps({**base, 'choices': [{'index': 0, 'delta': {'content': piece}, 'finish_reason': None}]}, ensure_ascii=False)}\n\n")
    out.append(f"data: {json.dumps({**base, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}], **(extra or {})})}\n\n")
    out.append(f"data: {json.dumps({**base, 'choices': [], 'usage': usage})}\n\n")
    out.append("data: [DONE]\n\n")
    return out


async def _stream_with_keepalive(job, res_id: str, created_ts: int, model_hint: str):
    """Send keep-alives until `job` finishes, then stream the ready SSE chunks."""
    base = {"id": res_id, "object": "chat.completion.chunk",
            "created": created_ts, "model": model_hint}
    yield f"data: {json.dumps({**base, 'choices': [{'index': 0, 'delta': {'role': 'assistant'}, 'finish_reason': None}]})}\n\n"

    task = job if isinstance(job, asyncio.Future) else asyncio.ensure_future(job)
    started = time.monotonic()
    thinking_sent = False
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=KEEPALIVE_INTERVAL)
            if done:
                break
            yield ": thinking\n\n"
            waited = time.monotonic() - started
            if waited >= THINKING_AFTER and not thinking_sent:
                thinking_sent = True
                yield (f"data: {json.dumps({**base, 'choices': [{'index': 0, 'delta': {'reasoning_content': 'Javob tayyorlanmoqda...'}, 'finish_reason': None}]}, ensure_ascii=False)}\n\n")
    except asyncio.CancelledError:
        task.cancel()
        raise

    try:
        chunks = task.result()
    except Exception as e:
        # The stream has already started, so the HTTP status cannot change.
        # The error is NOT sent as ASSISTANT TEXT (otherwise the client would
        # take an error message for a real answer). The OpenAI SDK reads a
        # `data: {"error": {...}}` event and raises, so the client sees the
        # error as an error.
        err = _as_cluster_error(e)
        log.warning("stream ended with error: %s %s", err.status, err.code)
        yield f"data: {json.dumps(_error_body(err), ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
        return

    for b in chunks:
        yield b


def _completion_payload(*, answer: str, images: list, used_model: str, prompt_text: str,
                        stream: bool, fingerprint: str, cached: bool,
                        tool_names: Optional[List[str]] = None,
                        calls: Optional[List[dict]] = None,
                        sse_parts: bool = False,
                        fallback: bool = False,
                        res_id: Optional[str] = None,
                        created_ts: Optional[int] = None):
    """Build the OpenAI-compatible response (same for the cache and normal path).

    Images are returned in two places: as markdown in the answer text (what
    clients usually expect) and in full base64 in the `images` field.
    """
    # If tool calling was requested, extract the YAML block from the answer
    # (the main path does this earlier and passes ready `calls`)
    if calls is None:
        calls = []
        if tool_names:
            calls, answer = tool_proto.parse_tool_calls(answer, tool_names)

    # Image cost comes from a fixed per-image token count, NOT the base64 length:
    # measured, a 1 MB PNG inside markdown counted as 544,000 "tokens".
    text_tokens = int(len(answer or "") / cfg.CHARS_PER_TOKEN)
    if images:
        md = "\n\n".join(f"![rasm {i + 1}](data:{mime};base64,{b64})"
                          for i, (mime, b64) in enumerate(images))
        answer = (answer + "\n\n" + md).strip() if answer else md

    res_id = res_id or f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created_ts = created_ts if created_ts is not None else int(time.time())
    # Approximate tokens; the gateway backend recomputes them at settlement
    prompt_tokens = max(1, int(len(prompt_text) / cfg.CHARS_PER_TOKEN))
    completion_tokens = max(1, text_tokens + len(images or []) * cfg.IMAGE_OUTPUT_TOKENS)
    usage = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
             "total_tokens": prompt_tokens + completion_tokens,
             # OpenAI style: input tokens served from the cache are reported separately
             "prompt_tokens_details": {"cached_tokens": prompt_tokens if cached else 0}}

    # The provider-side fallback model was used; the client should see that
    # (the internal model name is DELIBERATELY omitted, it is only logged).
    # The "gateway" key and X-SF-* header are part of the backend contract.
    sf_meta = {"gateway": {"fallback_model": True}} if fallback else {}
    sf_headers = {"X-SF-Fallback-Model": "1"} if fallback else {}

    if stream:
        base = {"id": res_id, "object": "chat.completion.chunk",
                "created": created_ts, "model": used_model}
        chunks = _sse_chunks(base, calls, answer, usage, sf_meta)
        # The keep-alive path streams the chunks itself (the role delta has
        # already been sent), so only the list is returned.
        if sse_parts:
            return chunks

        async def event_generator():
            yield f"data: {json.dumps({**base, 'choices': [{'index': 0, 'delta': {'role': 'assistant'}, 'finish_reason': None}]})}\n\n"
            for b in chunks:
                yield b

        return StreamingResponse(event_generator(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                                          "X-Cluster-Cache": "hit" if cached else "miss", **sf_headers})

    return JSONResponse({
        "id": res_id,
        "object": "chat.completion",
        "created": created_ts,
        "model": used_model,
        "system_fingerprint": f"cluster-{fingerprint}",
        "cached": cached,
        "images": [{"b64_json": b64, "mime_type": mime} for mime, b64 in images] or None,
        "choices": [{
            "index": 0,
            "message": ({"role": "assistant", "content": answer or None,
                         "tool_calls": calls} if calls
                        else {"role": "assistant", "content": answer}),
            "finish_reason": "tool_calls" if calls else "stop",
        }],
        "usage": usage,
        **sf_meta,
    }, headers={"X-Cluster-Cache": "hit" if cached else "miss", **sf_headers})


def _image_payload(used_model: str, task: str, images: list) -> dict:
    """Response of the image endpoints. Without `usage` the gateway would bill it as 1 token."""
    prompt_tokens = max(1, int(len(task) / cfg.CHARS_PER_TOKEN))
    completion_tokens = len(images) * cfg.IMAGE_OUTPUT_TOKENS
    return {
        "created": int(time.time()),
        "model": used_model,
        "data": [{"b64_json": b64, "mime_type": mime} for mime, b64 in images],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                  "total_tokens": prompt_tokens + completion_tokens},
    }


def _phases_str(meta: dict) -> str:
    return ",".join(f"{k}:{v}" for k, v in (meta.get("phases") or {}).items()) or "-"


class ImageRequest(BaseModel):
    prompt: str
    model: Optional[str] = "auto"
    n: Optional[int] = 1
    size: Optional[str] = None
    response_format: Optional[str] = "b64_json"


@app.post("/v1/images/generations", dependencies=[Depends(_auth)])
async def images_generations(req: ImageRequest, request: Request):
    """OpenAI-compatible image generation.

    Web-chat providers return images inside the conversation, so the prompt is
    sent to the chat and the image is taken from the DOM. Image URLs are usually
    `blob:` or need a cookie, so the client could not open them; the bytes are
    read inside the page and returned as base64 (regardless of `response_format`).
    """
    prompt = (req.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="`prompt` bo'sh bo'lmasligi kerak")

    balancer: ClusterBalancer = request.app.state.balancer
    count = max(1, min(int(req.n or 1), cfg.MAX_IMAGES_PER_REQUEST))
    size_note = f" O'lcham: {req.size}." if req.size else ""
    task = (f"{prompt}\n\n[Vazifa]: Yuqoridagi tavsif bo'yicha {count} ta rasm chiz "
            f"(generate image).{size_note} Matn bilan tushuntirma berma, faqat rasmni qaytar.")

    t_start = time.monotonic()
    run_meta = runmeta.start(image_only=True)
    try:
        _, used_model, worker, images = await balancer.execute(
            prompt_text=task,
            requested_model=(req.model or "auto").lower(),
            timeout_per_worker=cfg.IMAGE_REQUEST_TIMEOUT,
            require=["image_gen"],
            want_images=True,
        )
    except DispatchError:
        raise   # the handler returns the right status and Retry-After
    except RuntimeError as e:
        log.warning("images: %s", e)
        raise ClusterError(503, "model_unavailable",
                           "Model hozir mavjud emas. Birozdan keyin qayta urinib ko'ring.", 15)

    if not images:
        raise HTTPException(status_code=502,
                            detail="Model rasm qaytarmadi — boshqa tavsif bilan urinib ko'ring")

    log.info("image model=%s effort=%s count=%d phases=%s time=%.1fs", used_model,
             run_meta.get("upstream_effort") or "-", len(images),
             _phases_str(run_meta), time.monotonic() - t_start)
    return _image_payload(used_model, task, images[:count])


class ImageEditRequest(BaseModel):
    prompt: str
    image: Any = None          # data URI / https URL, or a list of them
    model: Optional[str] = "auto"
    n: Optional[int] = 1
    size: Optional[str] = None
    response_format: Optional[str] = "b64_json"


@app.post("/v1/images/edits", dependencies=[Depends(_auth)])
async def images_edits(req: ImageEditRequest, request: Request):
    """OpenAI-compatible image editing: uploaded image + instruction -> new image.

    The gateway converts the multipart request into this JSON shape. The image
    is attached to the chat and, as with plain generation, the new image from
    the answer is returned (the user's uploaded image is not included).
    """
    prompt = (req.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="`prompt` bo'sh bo'lmasligi kerak")
    sources = req.image if isinstance(req.image, list) else ([req.image] if req.image else [])
    sources = [x for x in sources if isinstance(x, str) and x.strip()]
    if not sources:
        raise HTTPException(status_code=400, detail="`image` berilmagan")

    balancer: ClusterBalancer = request.app.state.balancer
    count = max(1, min(int(req.n or 1), cfg.MAX_IMAGES_PER_REQUEST))
    size_note = f" O'lcham: {req.size}." if req.size else ""
    task = (f"{prompt}\n\n[Vazifa]: Biriktirilgan rasmni yuqoridagi ko'rsatma bo'yicha tahrirla "
            f"va natijani {count} ta rasm qilib qaytar (edit image).{size_note} "
            f"Matn bilan tushuntirma berma, faqat tahrirlangan rasmni qaytar.")

    with attach.AttachmentStore() as store:
        try:
            files = [await asyncio.to_thread(attach._ingest, store, src, f"image-{i + 1}")
                     for i, src in enumerate(sources)]
        except attach.AttachmentError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if not all(a.is_image for a in files):
            raise HTTPException(status_code=400, detail="`image` faqat rasm bo'lishi kerak")
        t_start = time.monotonic()
        run_meta = runmeta.start(image_only=True)
        try:
            _, used_model, worker, images = await balancer.execute(
                prompt_text=task,
                requested_model=(req.model or "auto").lower(),
                timeout_per_worker=cfg.IMAGE_REQUEST_TIMEOUT,
                require=["image_gen", "vision"],
                attachment_paths=[a.path for a in files],
                images_only=True,
                want_images=True,
            )
        except DispatchError:
            raise
        except RuntimeError as e:
            log.warning("images/edits: %s", e)
            raise ClusterError(503, "model_unavailable",
                               "Model hozir mavjud emas. Birozdan keyin qayta urinib ko'ring.", 15)

    if not images:
        raise HTTPException(status_code=502,
                            detail="Model tahrirlangan rasm qaytarmadi — ko'rsatmani boshqacha yozib ko'ring")
    log.info("image model=%s effort=%s count=%d phases=%s time=%.1fs", used_model,
             run_meta.get("upstream_effort") or "-", len(images),
             _phases_str(run_meta), time.monotonic() - t_start)
    return _image_payload(used_model, task, images[:count])


def main() -> None:
    import uvicorn
    log.info("binding %s:%d", cfg.HOST, cfg.PORT)
    uvicorn.run("cluster.server:app" if __package__ is None else f"{__package__}.server:app",
                host=cfg.HOST, port=cfg.PORT, reload=False, log_level=cfg.LOG_LEVEL.lower())


if __name__ == "__main__":
    main()
