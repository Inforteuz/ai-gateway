"""FastAPI app: /v1 (OpenAI-compatible API), /portal (panel API), optionally the React build directory."""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import socket
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import billing, branding
from . import gateway, monitor, routes_account, routes_admin, routes_auth, routes_instances, routes_monitor, routes_referral
from . import topup_telegram
from .vnc import TicketStore
from . import cluster_watch
from .common import ApiError
from .config import Config
from .db import from_config
from . import abuse
from .bodylimit import BodySizeLimit
from .ratelimit import SlidingWindow


log = logging.getLogger("main")

LEASE_INTERVAL = 20   # seconds; billing.LEASE_STALE_SECONDS must stay well above it


async def _lease_loop(app: FastAPI) -> None:
    """Heartbeat for this process's reservations + release of orphaned ones (dead process, expired)."""
    from fastapi.concurrency import run_in_threadpool
    while True:
        await asyncio.sleep(LEASE_INTERVAL)
        try:
            n = await run_in_threadpool(billing.sweep_reservations, app.state.db, app.state.instance_id)
            if n:
                log.warning("released %d orphaned reservation(s)", n)
        except Exception as e:
            log.warning("reservation sweep failed: %s", type(e).__name__)


def create_app(config: Config | None = None, http_client: httpx.AsyncClient | None = None) -> FastAPI:
    config = config or Config()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks = [asyncio.create_task(gateway.janitor(app)), asyncio.create_task(_lease_loop(app))]
        if config.monitor_enabled:
            tasks.append(asyncio.create_task(monitor.loop(app)))
            # Cluster providers can fail independently — monitor them
            # separately from ollama and announce outages.
            tasks.append(asyncio.create_task(cluster_watch.loop(app)))
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            try:
                billing.unregister_instance(app.state.db, app.state.instance_id)
            except Exception as e:
                log.warning("could not unregister instance: %s", type(e).__name__)
            await app.state.notify_http.aclose()
            await app.state.ext_http.aclose()
            await app.state.cluster_http.aclose()
            if http_client is None:
                await app.state.http.aclose()
            app.state.db.close()

    app = FastAPI(title=f"{branding.BRAND_NAME} API", version="1.0.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    s = app.state
    s.config = config
    s.db = from_config(config)
    # Identifies this process's reservations; orphans of dead processes are released, live ones are kept.
    s.instance_id = f"{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(4)}"
    billing.register_instance(s.db, s.instance_id)
    billing.sweep_reservations(s.db, s.instance_id)
    s.http = http_client or httpx.AsyncClient(limits=httpx.Limits(max_connections=50, max_keepalive_connections=10))
    s.sem = asyncio.Semaphore(config.max_concurrent)
    s.ext_http = httpx.AsyncClient(limits=httpx.Limits(max_connections=40, max_keepalive_connections=8))
    s.ext_sem = asyncio.Semaphore(config.ext_concurrent)
    s.cluster_http = httpx.AsyncClient(limits=httpx.Limits(max_connections=40, max_keepalive_connections=8))
    s.cluster_sem = asyncio.Semaphore(config.cluster_concurrent)
    s.inflight = {}
    s.open_tickets = set()
    s.vnc_tickets = TicketStore()  # admin VNC: one-time tickets (the backend is a single process)
    s.rpm = SlidingWindow(60)
    s.auth_limiter = SlidingWindow(15 * 60)
    s.signup_limiter = SlidingWindow(3600)
    # Separate per-IP rate limit: prevents multiplying throughput by spreading
    # one key across many machines.
    s.ip_rpm = SlidingWindow(60)
    # Which IPs use each key — to detect shared keys
    s.sharing = abuse.SharingTracker(window=3600)
    s.status_cache = (0.0, False)
    s.reject_log_last = {}
    s.notify_http = httpx.AsyncClient(timeout=10.0)
    s.health = {"state": "unknown", "ts": 0, "latency_ms": None, "error": None, "since": 0, "streak": 0, "first_fail": 0}

    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["Authorization", "Content-Type"],
        expose_headers=["X-Request-Id", "X-SF-Cost", "X-SF-Balance", "Retry-After",
                        "X-Fallback-Model", "X-SF-Routed-Model", "X-SF-Route-Reason", "X-SF-Cache"], max_age=600,
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if request.url.path.startswith("/portal"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    # Outermost: caps request bodies also when they arrive chunked (no Content-Length)
    app.add_middleware(BodySizeLimit, api_limit=max(config.max_body_bytes, config.max_multimodal_bytes),
                       default_limit=config.max_body_bytes)

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError):
        # /v1 is read by other people's programs and their logs: answer it in English.
        # The panel (/portal) keeps the Uzbek wording its users expect.
        return JSONResponse(exc.body(english=request.url.path.startswith("/v1/")),
                            status_code=exc.status, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(p) for p in first.get("loc", []) if p not in ("body", "query"))
        english = request.url.path.startswith("/v1/")
        bad_value, bad_request = ("invalid value", "Invalid request") if english else ("noto‘g‘ri qiymat", "Noto‘g‘ri so‘rov")
        message = f"{loc}: {first.get('msg', bad_value)}" if loc else first.get("msg", bad_request)
        return JSONResponse({"error": {"message": message, "type": "invalid_request_error", "code": "validation_error"}},
                            status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        english = request.url.path.startswith("/v1/")
        message = (("Not found." if english else "Manzil topilmadi.") if exc.status_code == 404
                   else str(exc.detail))
        return JSONResponse({"error": {"message": message, "type": "invalid_request_error", "code": code}},
                            status_code=exc.status_code, headers=getattr(exc, "headers", None))

    app.include_router(gateway.router)
    app.include_router(routes_auth.router)
    app.include_router(routes_account.router)
    app.include_router(routes_admin.router)
    app.include_router(routes_referral.router)
    app.include_router(routes_referral.admin_router)
    app.include_router(routes_monitor.admin_router)
    app.include_router(routes_monitor.public_router)
    app.include_router(routes_instances.router)
    app.include_router(routes_instances.ws_router)
    app.include_router(topup_telegram.router)
    app.include_router(topup_telegram.admin_router)

    @app.get("/portal/health")
    def health():
        s.db.scalar("SELECT 1")
        return {"ok": True}

    if config.static_dir:
        _mount_spa(app, Path(config.static_dir))
    return app


def _mount_spa(app: FastAPI, root: Path) -> None:
    """Serves the React build (Docker image, single-process setups). The brand name is put
    into index.html here so link previews and the first paint show it before the JS runs."""
    from html import escape
    from fastapi.responses import HTMLResponse
    from . import branding

    brand = escape(branding.BRAND_NAME)
    page = (root / "index.html").read_text(encoding="utf-8") \
        .replace("<title>AI Gateway</title>", f"<title>{brand}</title>") \
        .replace('content="AI Gateway:', f'content="{brand}:')
    index_headers = {"Cache-Control": "no-cache"}

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith(("v1/", "portal/")) or path in ("v1", "portal"):
            raise ApiError(404, "not_found", "Manzil topilmadi.")
        candidate = (root / path).resolve()
        if path and candidate.is_file() and root.resolve() in candidate.parents:
            return FileResponse(candidate)
        return HTMLResponse(page, headers=index_headers)
