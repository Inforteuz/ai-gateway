"""API playground inside the user's panel (2026-11-12).

A signed-in user tries listed models from the browser, paid from their own balance.
The request goes through the same gateway pipeline as /v1/chat/completions (holds,
billing, rate limits, routing, fallbacks), authenticated by the portal session:

  * each user gets one hidden key of kind 'playground' (created on first use). Its
    secret is discarded at creation and the bearer lookup only accepts kind 'api', so
    the key cannot be used from outside; it exists so usage, limits and the ledger
    work exactly as for any other key, and the usage page shows it as "Playground";
  * the portal's CSRF guard (custom header, same-origin cookie) applies;
  * the body is a narrow schema: text messages only, no tools, capped sizes, listed
    models only; the key has its own requests-per-minute limit.
"""
from __future__ import annotations

import json
import re
from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from . import directions, gateway, security, videos
from .common import FALLBACK_SUFFIX, ApiError, csrf_guard, require_user
from .db import Database, now

router = APIRouter(prefix="/portal/playground", dependencies=[Depends(csrf_guard)], tags=["playground"])

PLAYGROUND_RPM = 20            # requests per minute on the playground key
MAX_MESSAGES = 40
MAX_MESSAGE_CHARS = 32_000
MAX_TOTAL_CHARS = 60_000
MAX_OUTPUT_TOKENS = 4096
REQUEST_ID = re.compile(r"^req_[0-9a-f]{24}" + re.escape(FALLBACK_SUFFIX) + "?$")


class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(max_length=MAX_MESSAGE_CHARS)


class ChatIn(BaseModel):
    model: str = Field(min_length=1, max_length=80)
    messages: list[Message] = Field(min_length=1, max_length=MAX_MESSAGES)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1, le=MAX_OUTPUT_TOKENS)
    reasoning_effort: Literal["none", "low", "medium", "high"] | None = None
    stream: bool = True


def playground_key(db: Database, user_id: int) -> int:
    row = db.one("SELECT id FROM api_keys WHERE user_id=? AND kind='playground' AND status='active' ORDER BY id DESC LIMIT 1",
                 (user_id,))
    if row is not None:
        return row["id"]
    _raw, prefix, last4, digest = security.new_api_key()   # the secret is never stored or shown
    with db.tx() as c:
        cur = c.execute("""INSERT INTO api_keys(user_id, name, prefix, last4, key_hash, rpm_limit, kind, created_at)
                           VALUES (?,?,?,?,?,?,'playground',?)""",
                        (user_id, "Playground", prefix, last4, digest, PLAYGROUND_RPM, now()))
    return int(cur.lastrowid)


def _listed(db: Database, model_id: str) -> bool:
    row_id, _ = directions.resolve(model_id)
    return db.one("SELECT 1 FROM models WHERE id=? AND enabled=1 AND listed=1", (row_id,)) is not None


@router.post("/chat")
async def chat(body: ChatIn, request: Request, user=Depends(require_user)):
    db: Database = request.app.state.db
    if sum(len(m.content) for m in body.messages) > MAX_TOTAL_CHARS:
        raise ApiError(413, "request_too_large", "Suhbat juda uzun. Yangi suhbat boshlang.")
    if body.messages[-1].role != "user" or not body.messages[-1].content.strip():
        raise ApiError(400, "invalid_request", "Oxirgi xabar foydalanuvchidan bo'lishi kerak.")
    if not await run_in_threadpool(_listed, db, body.model):
        raise ApiError(404, "model_not_found", "Model topilmadi.")
    key_id = await run_in_threadpool(playground_key, db, user["id"])
    payload = body.model_dump(exclude_none=True)
    if body.stream:
        payload["stream_options"] = {"include_usage": True}
    request.state.translated_body = json.dumps(payload, ensure_ascii=False).encode()
    request.state.playground_key_id = key_id
    return await gateway.proxy(request, "chat/completions")


@router.get("/receipt/{request_id}")
def receipt(request_id: str, request: Request, user=Depends(require_user)):
    """What one playground answer cost, read from the caller's own usage rows."""
    if not REQUEST_ID.match(request_id):
        raise ApiError(400, "invalid_request", "Noto'g'ri so'rov.")
    db: Database = request.app.state.db
    base = request_id[:-len(FALLBACK_SUFFIX)] if request_id.endswith(FALLBACK_SUFFIX) and len(request_id) > 28 else request_id
    rows = db.all("""SELECT model, status_code, prompt_tokens, completion_tokens, cost, latency_ms, ttfb_ms, attribution
                     FROM usage_logs WHERE user_id=? AND request_id IN (?, ?) ORDER BY id""",
                  (user["id"], base, base + FALLBACK_SUFFIX))
    if not rows:
        return {"ready": False}
    last = rows[-1]
    try:
        routed = json.loads(last["attribution"] or "{}").get("route_model")
    except ValueError:
        routed = None
    wallet = db.one("SELECT balance FROM wallets WHERE user_id=?", (user["id"],))
    return {
        "ready": True, "model": last["model"], "routed_model": routed, "status_code": last["status_code"],
        "prompt_tokens": last["prompt_tokens"], "completion_tokens": last["completion_tokens"],
        "cost": sum(r["cost"] for r in rows), "latency_ms": last["latency_ms"], "ttfb_ms": last["ttfb_ms"],
        "balance": wallet["balance"] if wallet else 0,
    }


# --- video (2026-11-12): the same /v1/videos jobs, through the session and the hidden playground key ----------
async def _as_playground(request: Request, user: dict) -> None:
    request.state.playground_key_id = await run_in_threadpool(playground_key, request.app.state.db, user["id"])


@router.post("/videos")
async def create_video(request: Request, user=Depends(require_user)):
    """Queues a clip paid from the user's balance (body as for POST /v1/videos: model, prompt, size,
    optional input_reference data URI). The job is the same row the public API uses."""
    await _as_playground(request, user)
    return await videos.create_video(request)


@router.get("/videos/{video_id}")
async def video_status(video_id: str, request: Request, user=Depends(require_user)):
    await _as_playground(request, user)
    return await videos.retrieve_video(video_id, request)


@router.get("/videos/{video_id}/content")
async def video_file(video_id: str, request: Request, user=Depends(require_user)):
    await _as_playground(request, user)
    return await videos.video_content(video_id, request)


# --- images (2026-11-12): /v1/images/generations through the session, same billing as the API ----------------
class ImageIn(BaseModel):
    model: str = Field(min_length=1, max_length=80)
    prompt: str = Field(min_length=1, max_length=4000)
    size: Literal["1024x1024", "1024x1536", "1536x1024"] | None = None


@router.post("/images")
async def create_image(body: ImageIn, request: Request, user=Depends(require_user)):
    db: Database = request.app.state.db
    if not await run_in_threadpool(_listed, db, body.model):
        raise ApiError(404, "model_not_found", "Model topilmadi.")
    request.state.playground_key_id = await run_in_threadpool(playground_key, db, user["id"])
    payload = {"model": body.model, "prompt": body.prompt.strip(), "n": 1}
    if body.size:
        payload["size"] = body.size
    request.state.translated_body = json.dumps(payload, ensure_ascii=False).encode()
    return await gateway.proxy(request, "images/generations")
