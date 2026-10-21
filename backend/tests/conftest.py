import asyncio
import json
import os

# Test deployment identity (branding is read from the environment at import time)
os.environ.setdefault("SF_SITE_URL", "https://example.com")
os.environ.setdefault("SF_API_URL", "https://api.example.com/v1")
import re
import secrets
import warnings

import httpx
import psycopg
import pytest
from psycopg import sql as pgsql
from psycopg.conninfo import make_conninfo
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient

from app import billing, security
from app.common import create_user
from app.config import Config
from app.db import apply_schema
from app.main import create_app

warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", message=".*httpx.*")

SOM = billing.MICRO
# qwen3:14b: input 10 000 som / 1M => 1 token = 10 000 micro-som; output 30 000 som / 1M => 30 000 micro-som
IN_PER_TOKEN, OUT_PER_TOKEN = 10_000, 30_000


def make_upstream(state: dict) -> FastAPI:
    """Mimics the OpenAI-compatible part of Ollama."""
    up = FastAPI()

    @up.get("/api/tags")
    async def tags():
        return {"models": [{"name": "qwen3:14b"}, {"name": "llama3.2:3b"}]}

    @up.post("/v1/chat/completions")
    async def chat(request: Request):
        body = await request.json()
        state["last"] = body
        state["calls"] = state.get("calls", 0) + 1
        if state.get("delay"):
            await asyncio.sleep(state["delay"])   # keeps requests in flight for concurrency tests
        mode = state.get("mode", "ok")
        if mode == "500":
            return JSONResponse({"error": {"message": "boom"}}, status_code=500)
        if mode == "400":
            return JSONResponse({"error": {"message": "context length exceeded"}}, status_code=400)
        if mode == "timeout":
            raise httpx.ReadTimeout("provider did not answer in time")   # what the real transport raises
        if mode == "429":
            return JSONResponse({"error": {"message": "rate limited"}}, status_code=429, headers={"Retry-After": "7"})
        usage = state.get("usage", {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18})
        if state.get("starve") and body.get("reasoning_effort") not in (None, "none") and not body.get("stream"):
            # reasoning ate the whole budget: empty answer, finish_reason=length
            return {"id": "x", "model": "qwen3:14b", "choices": [{"message": {"role": "assistant", "content": "", "reasoning": "o'ylayapman..."}, "finish_reason": "length"}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 500, "total_tokens": 511}}
        if body.get("stream"):
            async def gen():
                if state.get("loop_stream"):
                    for _ in range(500):
                        yield "data: " + json.dumps({"model": "qwen3:14b", "choices": [{"delta": {"content": "Salom dunyo. "}}]}) + "\n\n"
                elif state.get("tool_stream"):
                    for part in ['{"city":', '"Toshkent"}']:
                        yield "data: " + json.dumps({"model": "qwen3:14b", "choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "get_weather", "arguments": part}}]}}]}) + "\n\n"
                else:
                    for w in ["Sa", "lom"]:
                        yield "data: " + json.dumps({"model": "qwen3:14b", "system_fingerprint": "fp_ollama", "choices": [{"delta": {"content": w}}]}) + "\n\n"
                        if state.get("break_stream"):
                            # what httpx raises when the provider's connection drops mid-stream
                            raise httpx.RemoteProtocolError("peer closed connection without sending complete message body")
                if (body.get("stream_options") or {}).get("include_usage") and not state.get("no_usage"):
                    yield "data: " + json.dumps({"model": "qwen3:14b", "choices": [], "usage": usage}) + "\n\n"
                yield "data: [DONE]\n\n"
            return StreamingResponse(gen(), media_type="text/event-stream")
        data = {"id": "x", "model": "qwen3:14b", "system_fingerprint": "fp_ollama",
                "choices": [{"message": {"role": "assistant", "content": state.get("draft", "Salom")}, "finish_reason": state.get("finish", "stop")}]}
        if not state.get("no_usage"):
            data["usage"] = usage
        return data

    return up


def make_external(state: dict) -> FastAPI:
    """Mimics an external OpenAI-compatible provider."""
    ext = FastAPI()

    @ext.get("/v1/models")
    async def models(request: Request):
        state["models_auth"] = request.headers.get("authorization")
        if state.get("bad_key"):
            return JSONResponse({"error": "bad key"}, status_code=401)
        return {"data": [{"id": "posiden/gpt-6-luna"}, {"id": "posiden/deepseek-v4-flash"}]}

    @ext.post("/v1/chat/completions")
    async def chat(request: Request):
        body = await request.json()
        state["last"] = body
        state["auth"] = request.headers.get("authorization")
        state["calls"] = state.get("calls", 0) + 1
        mode = state.get("mode", "ok")
        if mode == "500":
            return JSONResponse({"error": {"message": "boom"}}, status_code=500)
        usage = {"prompt_tokens": 20, "completion_tokens": 9, "total_tokens": 29}
        if body.get("stream"):
            async def gen():
                if mode != "empty":
                    for w in re.findall(r"\S+\s*", state.get("reply", "Tekshirilgan javob.")):
                        yield "data: " + json.dumps({"model": "posiden/gpt-6-luna", "choices": [{"delta": {"content": w}}]}) + "\n\n"
                    yield "data: " + json.dumps({"model": "posiden/gpt-6-luna", "choices": [{"delta": {}, "finish_reason": state.get("finish", "stop")}]}) + "\n\n"
                yield "data: " + json.dumps({"model": "posiden/gpt-6-luna", "choices": [], "usage": usage}) + "\n\n"
                yield "data: [DONE]\n\n"
            return StreamingResponse(gen(), media_type="text/event-stream")
        content = {"empty": "", "short": "x"}.get(mode, state.get("reply", "Tekshirilgan javob."))
        return {"id": "e1", "model": "posiden/gpt-6-luna", "system_fingerprint": "fp_ext",
                "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": state.get("finish", "stop")}], "usage": usage}

    return ext


@pytest.fixture(autouse=True)
def _reset_refiner_cooldown():
    from app import refine
    refine.COOLDOWN.clear()
    yield
    refine.COOLDOWN.clear()


@pytest.fixture
def upstream_state():
    return {}


@pytest.fixture
def ext_state():
    return {}


# Tests run against a real PostgreSQL (same engine as production). Each pytest process
# gets its own schema, so parallel pytest runs do not interfere with each other.
# The app connects with a RESTRICTED role, as in production (no DDL, ledger is append-only),
# so a test fails if a privilege is missing.
TEST_DSN = os.getenv("SF_TEST_DATABASE_URL", "postgresql:///gateway_test")
TEST_APP_ROLE = os.getenv("SF_TEST_APP_ROLE", "gateway_test_app")


@pytest.fixture(scope="session")
def pg_dsn():
    params = psycopg.conninfo.conninfo_to_dict(TEST_DSN)
    try:
        psycopg.connect(TEST_DSN, autocommit=True).close()
    except psycopg.OperationalError:
        # Create the test database if it does not exist (as the local superuser).
        with psycopg.connect(make_conninfo(TEST_DSN, dbname="postgres"), autocommit=True) as c:
            c.execute(pgsql.SQL("CREATE DATABASE {}").format(pgsql.Identifier(params.get("dbname", "gateway_test"))))
    with psycopg.connect(TEST_DSN, autocommit=True) as c:
        c.execute("CREATE EXTENSION IF NOT EXISTS citext WITH SCHEMA public")
        if not c.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (TEST_APP_ROLE,)).fetchone():
            c.execute(pgsql.SQL("CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE").format(pgsql.Identifier(TEST_APP_ROLE)))
        c.execute(pgsql.SQL("GRANT {} TO CURRENT_USER").format(pgsql.Identifier(TEST_APP_ROLE)))
    return TEST_DSN


def _wipe_order(c) -> list[str]:
    """Tables, children first (so DELETE does not hit FKs). schema_meta is left alone."""
    tables = [r[0] for r in c.execute("SELECT tablename FROM pg_tables WHERE schemaname=current_schema()")]
    deps = {t: set() for t in tables}
    for child, parent in c.execute(
            "SELECT conrelid::regclass::text, confrelid::regclass::text FROM pg_constraint "
            "WHERE contype='f' AND connamespace=current_schema()::regnamespace"):
        if child != parent:
            deps[parent].add(child)
    order, seen = [], set()
    def visit(t):
        if t not in seen:
            seen.add(t)
            for ch in sorted(deps[t]):
                visit(ch)
            order.append(t)
    for t in sorted(tables):
        visit(t)
    return [t for t in order if t != "schema_meta"]


@pytest.fixture(scope="session")
def _pg_session_schema(pg_dsn):
    """One schema + `schema.sql` per pytest process (owned by the test user).
    Tables are wiped between tests — creating a schema every time is much slower."""
    name = "t_" + secrets.token_hex(6)
    c = psycopg.connect(pg_dsn, autocommit=True, options="-c synchronous_commit=off")
    c.execute(pgsql.SQL("CREATE SCHEMA {}").format(pgsql.Identifier(name)))
    c.execute(pgsql.SQL("SET search_path = {}, public").format(pgsql.Identifier(name)))
    apply_schema(c, TEST_APP_ROLE)
    wipe = [pgsql.SQL("DELETE FROM {}").format(pgsql.Identifier(t)) for t in _wipe_order(c)]
    seqs = [r[0] for r in c.execute(
        "SELECT pg_get_serial_sequence(table_name, column_name) FROM information_schema.columns "
        "WHERE table_schema=current_schema() AND is_identity='YES'")]
    yield name, c, wipe, seqs
    c.execute(pgsql.SQL("DROP SCHEMA {} CASCADE").format(pgsql.Identifier(name)))
    c.close()


@pytest.fixture
def pg_schema(_pg_session_schema):
    """Clean schema (ids start at 1). Returns the app's connection string, using the restricted role."""
    name, c, wipe, seqs = _pg_session_schema
    with c.transaction():
        for stmt in wipe:
            c.execute(stmt)
        for seq in seqs:
            c.execute("SELECT setval(%s, 1, false)", (seq,))
    return make_conninfo(TEST_DSN, options=f"-c search_path={name},public -c role={TEST_APP_ROLE} -c synchronous_commit=off")


@pytest.fixture
def env(pg_schema, upstream_state, ext_state):
    cfg = Config(database_url=pg_schema, database_password=None, upstream_url="http://upstream", cookie_secure=False,
                 monitor_enabled=False, max_concurrent=2, queue_wait_seconds=2, per_key_concurrent=3)
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=make_upstream(upstream_state)), base_url="http://upstream")
    app = create_app(cfg, http_client=http)
    app.state.ext_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=make_external(ext_state)), base_url="http://external")
    with TestClient(app) as client:
        yield type("Env", (), {"app": app, "db": app.state.db, "client": client, "up": upstream_state, "ext": ext_state, "cfg": cfg})()


def add_user(env, email="a@test.uz", *, role="user", balance_som=0, password="parol-12345"):
    uid = create_user(env.db, email=email, name=email.split("@")[0], password=password, role=role)
    if balance_som:
        billing.credit(env.db, user_id=uid, amount=balance_som * SOM, kind="topup", note="test")
    return uid


def add_key(env, user_id, *, free=0, rpm=None, monthly=None, name="test"):
    raw, prefix, last4, digest = security.new_api_key()
    with env.db.tx() as c:
        cur = c.execute(
            "INSERT INTO api_keys(user_id, name, prefix, last4, key_hash, free, rpm_limit, monthly_limit, created_at) VALUES (?,?,?,?,?,?,?,?,1)",
            (user_id, name, prefix, last4, digest, free, rpm, monthly))
    return raw, cur.lastrowid


def reserve(env, uid, key_id, amount, *, request_id=None, model="model-1.0", tokens=0, monthly_limit=None,
            max_concurrent=None, quota_limit=0, quota_models=None, owner=None, ttl=3600):
    """billing.reserve with test defaults (this app process as the owner)."""
    return billing.reserve(env.db, request_id=request_id or "req_" + secrets.token_hex(6), user_id=uid, key_id=key_id,
                           model=model, amount=amount, tokens=tokens, free=False, monthly_limit=monthly_limit,
                           owner=owner or env.app.state.instance_id, ttl=ttl, max_concurrent=max_concurrent,
                           quota_limit=quota_limit, quota_models=quota_models)


def bearer(key):
    return {"Authorization": f"Bearer {key}"}


def wallet(env, uid):
    r = env.db.one("SELECT balance, held FROM wallets WHERE user_id=?", (uid,))
    return r["balance"], r["held"]


CHAT = {"model": "model-1.0", "messages": [{"role": "user", "content": "Salom"}]}
CSRF = {"X-SF-CSRF": "1"}


def add_model(env, mid, *, upstream="qwen3:14b", provider="local", refine=0, listed=1, enabled=1, display=None,
              price_in=10_000 * SOM, price_out=30_000 * SOM, default_reasoning=None, fallback=None, allow_thinking=1):
    with env.db.tx() as c:
        c.execute("""INSERT INTO models(id, upstream_name, display_name, description, price_in_per_1m, price_out_per_1m,
                         context_window, enabled, created_at, provider, refine, listed, default_reasoning, fallback_price_from, allow_thinking)
                     VALUES (?,?,?,?,?,?,16384,?,1,?,?,?,?,?,?)""",
                  (mid, upstream, display or mid, "", price_in, price_out, enabled, provider, refine, listed, default_reasoning, fallback, allow_thinking))


def configure_external(env, *, key="ext-secret-key", model="posiden/gpt-6-luna", timeout="25"):
    for k, v in {"external_base_url": "http://external/v1", "external_api_key": key, "refiner_model": model, "refine_timeout": timeout}.items():
        env.db.set_setting(k, v)
