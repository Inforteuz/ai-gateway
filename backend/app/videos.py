"""/v1/videos: OpenAI-compatible async video generation on the cluster's video model.

A clip takes minutes, so POST /v1/videos only queues a job and answers at once. A background
worker (worker(), started in main's lifespan) runs the queue; the client polls
GET /v1/videos/{id} and then downloads GET /v1/videos/{id}/content. The shapes follow
OpenAI's Videos API, so `client.videos.create / retrieve / download_content` work unchanged.

Billing: one clip is billed as CLIP_TOKENS (1,000,000) output tokens, i.e. it costs exactly
the model's `price_out_per_1m`, whatever its length. The price is held when the job is
created; the hold lives on the job row (`video_jobs.hold`, counted in wallets.held) instead
of `reservations`, because a reservation is leased to one app process and expires, while a
queued clip may wait for minutes and must survive a restart. When the job ends it goes
through billing.settle like any other request: one usage row (endpoint "videos") and, if
anything was charged, one ledger entry. A failed job is logged with cost 0.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import math
import re
import time
import uuid

import anyio
import httpx
from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response

from . import billing, directions, gateway
from .common import ApiError, client_ip
from .db import Database, now

router = APIRouter(prefix="/v1", tags=["videos"])

log = logging.getLogger("videos")

# Cluster models (models.upstream_name) that render video. Only these take /v1/videos.
VIDEO_UPSTREAMS = frozenset({"flow"})
# One clip is billed as this many output tokens, so its price is the model's price_out_per_1m.
CLIP_TOKENS = 1_000_000
UPSTREAM_TIMEOUT = 420.0      # the cluster renders a clip in a few minutes; past this it is not coming
FILE_TTL = 86400              # finished clips are kept for a day (expires_at), then purged
QUEUE_LIMIT = 3600            # a job still queued after an hour is failed and its hold released
RECOVER_EVERY = 30            # seconds between scans for jobs whose worker died
IDLE_WAIT = 2.0               # the worker looks at the queue at least this often
POLL_RPM = 120                # status/content/list reads per key per minute (apart from the request limit)
MAX_PROMPT_CHARS = 32_000
DEFAULT_SECONDS, DEFAULT_SIZE = "8", "1280x720"
ACTIVE = ("queued", "in_progress")

_ID = re.compile(r"video_[0-9a-f]{24}")
_SIZE = re.compile(r"\d{2,4}x\d{2,4}")
_DATA_IMAGE = re.compile(r"data:image/(?:png|jpeg|jpg|webp|gif);base64,", re.I)


# --- request parsing ---------------------------------------------------------------------------------------------
def _bad_image() -> ApiError:
    return ApiError(400, "invalid_image",
                    "'input_reference' PNG, JPEG, WEBP yoki GIF rasm bo'lishi kerak (fayl yoki data:image/...;base64,...).",
                    en="`input_reference` must be a PNG, JPEG, WEBP or GIF image: a file upload or a "
                       "data:image/...;base64,... URI.")


def parse_body(raw: bytes, content_type: str) -> dict:
    """JSON body, or multipart/form-data as the OpenAI SDK sends it (`input_reference` as a file)."""
    if not (content_type or "").lower().startswith("multipart/form-data"):
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise ApiError(400, "invalid_json", "So'rov tanasi to'g'ri JSON obyekt bo'lishi kerak.")
        return body
    body: dict = {}
    for name, data, part in gateway.multipart_parts(raw, content_type):
        if name == "input_reference":
            if data[:5].lower() == b"data:":     # a data URI sent as a plain form field
                body[name] = data.decode("ascii", "replace").strip()
                continue
            # The image's own bytes decide its type: a declared Content-Type could let anything through.
            mime = gateway._sniff_image(data, "")
            if not mime:
                raise _bad_image()
            body[name] = f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
        elif name in ("model", "prompt", "seconds", "size"):
            body[name] = data.decode("utf-8", "replace")
    return body


def check_image(value) -> str | None:
    """Only inline images: a URL would make the cluster fetch whatever address a client names."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise _bad_image()
    m = _DATA_IMAGE.match(value)
    if not m:
        raise _bad_image()
    try:
        data = base64.b64decode(value[m.end():], validate=True)
    except (binascii.Error, ValueError):
        raise _bad_image()
    if not gateway._sniff_image(data, ""):
        raise _bad_image()
    return value


def parse_seconds(value) -> str:
    if value is None or value == "":
        return DEFAULT_SECONDS
    text = str(value).strip() if isinstance(value, (int, str)) and not isinstance(value, bool) else ""
    if not text.isdigit() or not 1 <= int(text) <= 60:
        raise ApiError(400, "invalid_seconds", "'seconds' 1 dan 60 gacha butun son bo'lishi kerak.",
                       en="`seconds` must be a whole number from 1 to 60.")
    return str(int(text))


def parse_size(value) -> str:
    if value is None or value == "":
        return DEFAULT_SIZE
    if not isinstance(value, str) or not _SIZE.fullmatch(value.strip()):
        raise ApiError(400, "invalid_size", "'size' KENGLIKxBALANDLIK ko'rinishida bo'lishi kerak, masalan 1280x720.",
                       en="`size` must be WIDTHxHEIGHT, for example 1280x720.")
    return value.strip()


def is_video_model(model) -> bool:
    return model["provider"] == "cluster" and model["upstream_name"] in VIDEO_UPSTREAMS


def _video_model(db: Database, model_id) -> tuple[dict, str]:
    row_id, public_id = directions.resolve(model_id) if isinstance(model_id, str) and model_id else (None, model_id)
    row = db.one("SELECT * FROM models WHERE id=? AND enabled=1", (row_id,)) if row_id else None
    if row is None:
        names = ", ".join(r["id"] for r in db.all(
            "SELECT id, provider, upstream_name FROM models WHERE enabled=1 AND listed=1 ORDER BY id") if is_video_model(r))
        raise ApiError(404, "model_not_found", f"Model topilmadi. Video modellari: {names or '-'}.",
                       en=f"The model `{model_id}` does not exist. Video models: {names or 'none'}.")
    if not is_video_model(row):
        raise ApiError(400, "unsupported_endpoint", "Bu model video yaratmaydi.",
                       en="This model does not generate videos. Use a video model with /v1/videos.")
    return dict(row), public_id


# --- job rows ----------------------------------------------------------------------------------------------------
def video_object(job: dict) -> dict:
    """The OpenAI Video object."""
    ended = job["completed_at"] if job["status"] in ("completed", "failed") else None
    return {"id": job["id"], "object": "video", "model": job["public_model"], "status": job["status"],
            "progress": job["progress"], "created_at": job["created_at"], "completed_at": ended,
            "expires_at": ended + FILE_TTL if ended and job["status"] == "completed" else None,
            "seconds": job["seconds"], "size": job["size"],
            "error": json.loads(job["error"]) if job["error"] else None}


def insert_job(db: Database, job: dict, *, monthly_limit: int | None, max_active: int) -> None:
    """Writes the job and holds its price in one transaction (wallet and monthly limit as in billing.reserve)."""
    with db.tx() as c:
        active = c.execute("SELECT COUNT(*) FROM video_jobs WHERE key_id=? AND status IN ('queued','in_progress')",
                           (job["key_id"],)).fetchone()[0]
        if active >= max_active:
            raise billing.BillingError("too_many_concurrent",
                                       f"Bir kalitda bir vaqtda {max_active} tadan ortiq video bo'lishi mumkin emas.", 429)
        job["hold"] = 0 if job["free"] else job["price"]
        billing.hold_tx(c, user_id=job["user_id"], key_id=job["key_id"], money=job["hold"],
                        monthly_limit=monthly_limit, ts=job["created_at"])
        c.execute("""INSERT INTO video_jobs(id, user_id, key_id, model, public_model, upstream, prompt, input_image,
                         prompt_chars, status, progress, seconds, size, free, price, hold, ip, created_at)
                     VALUES (?,?,?,?,?,?,?,?,?,'queued',0,?,?,?,?,?,?,?)""",
                  (job["id"], job["user_id"], job["key_id"], job["model"], job["public_model"], job["upstream"],
                   job["prompt"], job["input_image"], job["prompt_chars"], job["seconds"], job["size"],
                   job["free"], job["price"], job["hold"], job["ip"], job["created_at"]))


def claim(db: Database, owner: str, limit: int) -> dict | None:
    """Oldest queued job -> in_progress for `owner`, if fewer than `limit` run gateway-wide."""
    if db.one("SELECT 1 FROM video_jobs WHERE status='queued' LIMIT 1") is None:
        return None   # the common case, answered without taking the write lock
    with db.tx() as c:
        if c.execute("SELECT COUNT(*) FROM video_jobs WHERE status='in_progress'").fetchone()[0] >= limit:
            return None
        row = c.execute("SELECT id FROM video_jobs WHERE status='queued' ORDER BY seq LIMIT 1").fetchone()
        if row is None:
            return None
        job = c.execute("UPDATE video_jobs SET status='in_progress', started_at=?, owner=? WHERE id=? RETURNING *",
                        (now(), owner, row["id"])).fetchone()
    return dict(job)


def finish_job(db: Database, job_id: str, *, status_code: int, clip: bytes = b"", mime: str = "video/mp4",
               seconds: str | None = None, size: str | None = None, error: dict | None = None,
               label: str | None = None, latency_ms: int | None = None) -> dict | None:
    """Ends a job once: stores the clip and charges it (status 200), or fails it with nothing charged.

    The hold is released and billing.settle writes the usage row and ledger entry in the same
    transaction as the status change. Returns None when the job had already ended (a second
    call, or recover() got to it first), so a clip is never charged twice.
    """
    ok = status_code == 200
    ts = now()
    with db.tx() as c:
        row = c.execute("SELECT * FROM video_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None or row["status"] not in ACTIVE:
            return None
        job = dict(row)
        billing.unhold_tx(c, user_id=job["user_id"], money=job["hold"], ts=ts)
        settled = billing.settle(
            db, request_id=job_id, user_id=job["user_id"], key_id=job["key_id"], model=job["model"],
            endpoint="videos", reserved=job["hold"], free=bool(job["free"]), price_in=0, price_out=job["price"],
            prompt_tokens=math.ceil(job["prompt_chars"] / gateway.CONTEXT_CHARS_PER_TOKEN),
            completion_tokens=CLIP_TOKENS, status_code=status_code, stream=False, estimated=True,
            latency_ms=latency_ms, error=None if ok else (label or (error or {}).get("code")), ip=job["ip"])
        if ok:
            c.execute("INSERT INTO video_files(job_id, mime, data, created_at) VALUES (?,?,?,?)", (job_id, mime, clip, ts))
        done = c.execute(
            """UPDATE video_jobs SET status=?, progress=?, hold=0, cost=?, error=?, seconds=?, size=?, completed_at=?,
                   prompt=NULL, input_image=NULL, owner=NULL WHERE id=? RETURNING *""",
            ("completed" if ok else "failed", 100 if ok else job["progress"], settled.cost,
             None if ok else json.dumps(error, ensure_ascii=False), seconds or job["seconds"], size or job["size"],
             ts, job_id)).fetchone()
    return dict(done)


def recover(db: Database) -> int:
    """Fails jobs that will never finish: in_progress under an app process that is gone (restart,
    crash) and queued ones that waited past QUEUE_LIMIT. Their hold is released, nothing charged."""
    ts = now()
    rows = db.all(
        """SELECT id, status FROM video_jobs v
           WHERE (status='in_progress' AND NOT EXISTS (
                     SELECT 1 FROM app_instances i WHERE i.owner = v.owner AND i.last_seen >= ?))
              OR (status='queued' AND created_at < ?)""",
        (ts - billing.LEASE_STALE_SECONDS, ts - QUEUE_LIMIT))
    for r in rows:
        if r["status"] == "in_progress":
            finish_job(db, r["id"], status_code=503, label="video_interrupted",
                       error={"code": "generation_interrupted",
                              "message": "The gateway restarted while this video was being generated. "
                                         "You were not charged; create it again."})
        else:
            finish_job(db, r["id"], status_code=503, label="queue_timeout",
                       error={"code": "queue_timeout",
                              "message": "The video waited too long in the queue and was cancelled. "
                                         "You were not charged; create it again."})
    return len(rows)


def purge_files(db: Database) -> int:
    """Deletes clips past their expires_at (gateway.janitor). The job rows stay as receipts."""
    with db.tx() as c:
        return c.execute("DELETE FROM video_files WHERE created_at < ?", (now() - FILE_TTL,)).rowcount


# --- worker ------------------------------------------------------------------------------------------------------
def wake(state) -> None:
    event = getattr(state, "video_wakeup", None)
    if event is not None:
        event.set()


def _max_concurrent(settings: dict) -> int:
    try:
        return max(0, int(settings.get("video_max_concurrent") or 0))
    except ValueError:
        return 2


async def start_jobs(app, running: set) -> int:
    """Claims queued jobs while there is room and runs each in its own task. Returns how many started."""
    state = app.state
    limit = _max_concurrent(await run_in_threadpool(state.db.get_settings))
    started = 0
    while len(running) < limit:
        job = await run_in_threadpool(claim, state.db, state.instance_id, limit)
        if job is None:
            break
        task = asyncio.create_task(process(app, job))
        running.add(task)
        task.add_done_callback(running.discard)
        started += 1
    return started


async def worker(app) -> None:
    """Runs the video queue for this app process. Several processes may run it: claim() is serialized
    in the database and video_max_concurrent counts jobs across all of them."""
    state = app.state
    state.video_wakeup = asyncio.Event()
    running: set[asyncio.Task] = set()
    last_recover = 0.0
    try:
        while True:
            state.video_wakeup.clear()
            try:
                if time.monotonic() - last_recover >= RECOVER_EVERY:
                    last_recover = time.monotonic()
                    n = await run_in_threadpool(recover, state.db)
                    if n:
                        log.warning("failed %d abandoned video job(s)", n)
                await start_jobs(app, running)
            except Exception as e:
                log.warning("video worker: %s", type(e).__name__)
            try:
                await asyncio.wait_for(state.video_wakeup.wait(), IDLE_WAIT)
            except asyncio.TimeoutError:
                pass
    finally:
        for t in list(running):
            t.cancel()


def _failure(status_code: int, code: str, message: str, label: str | None = None) -> dict:
    return {"status_code": status_code, "error": {"code": code, "message": message}, "label": label or code}


async def _render(state, job: dict) -> dict:
    """Asks the cluster for the clip. Returns finish_job() arguments for success or failure."""
    settings = await run_in_threadpool(state.db.get_settings)
    # Same client and auth as cluster chat requests. The cluster semaphore is not taken: a clip
    # holds its slot for minutes and chat would queue behind it; video_max_concurrent bounds videos.
    client, base, headers, _sem = gateway._target(state, {"provider": "cluster"}, settings)
    if not base:
        return _failure(503, "model_unavailable", "The video model is unavailable right now. You were not charged.")
    payload = {"model": job["upstream"], "prompt": job["prompt"], "image": job["input_image"],
               "seconds": int(job["seconds"]), "size": job["size"]}
    try:
        resp = await client.post(f"{base}/videos/generations", json=payload, headers=headers,
                                 timeout=httpx.Timeout(UPSTREAM_TIMEOUT, connect=5.0))
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return _failure(503, "model_offline", "The video model is offline. You were not charged.", "upstream_unavailable")
    except httpx.TimeoutException:
        return _failure(504, "upstream_timeout", "The video model did not finish in time. You were not charged.")
    except httpx.HTTPError as e:
        return _failure(502, "upstream_error", "The connection to the video model failed. You were not charged.",
                        f"upstream_error: {type(e).__name__}")
    if resp.status_code != 200:
        model = {"upstream_name": job["upstream"], "public_id": job["public_model"]}
        status, code, _uz, _headers, message = gateway.upstream_error(resp, resp.content, model)
        return _failure(status, code, message, f"upstream_{resp.status_code}:{code}")
    try:
        item = resp.json()["data"][0]
        clip = base64.b64decode(item["b64_mp4"], validate=True)
    except (ValueError, KeyError, IndexError, TypeError, binascii.Error):
        item, clip = {}, b""
    if not clip:
        return _failure(502, "upstream_failed", "The video model returned no video. You were not charged.", "empty_video")
    mime = item.get("mime_type") if isinstance(item.get("mime_type"), str) else ""
    seconds = item.get("seconds")
    size = item.get("size")
    return {"status_code": 200, "clip": clip, "mime": mime if mime.startswith("video/") else "video/mp4",
            "seconds": str(seconds) if isinstance(seconds, int) and not isinstance(seconds, bool) and seconds > 0 else None,
            "size": size if isinstance(size, str) and _SIZE.fullmatch(size) else None}


async def process(app, job: dict) -> dict | None:
    """Runs one claimed job to the end. Awaitable directly (tests) or as a worker task."""
    state = app.state
    t0 = time.monotonic()
    try:
        outcome = await _render(state, job)
    except asyncio.CancelledError:
        raise   # shutdown: the job stays in_progress and recover() fails it on the next start
    except Exception as e:
        log.warning("video %s failed: %s", job["id"], type(e).__name__)
        outcome = _failure(500, "internal_error", "The video could not be generated. You were not charged.")
    try:
        with anyio.CancelScope(shield=True):
            return await run_in_threadpool(finish_job, state.db, job["id"],
                                           latency_ms=int((time.monotonic() - t0) * 1000), **outcome)
    finally:
        wake(state)   # a slot is free: the next queued job can start


# --- routes ------------------------------------------------------------------------------------------------------
async def _admit(request: Request, key: dict, job_id: str, ctx: dict) -> dict:
    state = request.app.state
    db: Database = state.db
    cfg = state.config
    hard_limit = max(cfg.max_body_bytes, cfg.max_multimodal_bytes)
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > hard_limit:
        raise ApiError(413, "request_too_large", "So'rov hajmi juda katta.")
    raw = await request.body()
    if len(raw) > hard_limit:
        raise ApiError(413, "request_too_large", "So'rov hajmi juda katta.")
    body = parse_body(raw, request.headers.get("content-type", ""))
    image = check_image(body.get("input_reference"))
    # Only a request with a reference image may have a large body (as for chat attachments)
    if len(raw) > cfg.max_body_bytes and not image:
        raise ApiError(413, "request_too_large", "So'rov hajmi juda katta.")
    model_id = body.get("model")
    ctx["model"] = model_id if isinstance(model_id, str) and model_id else "-"
    model, public_id = await run_in_threadpool(_video_model, db, model_id)
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ApiError(400, "invalid_prompt", "'prompt' berilmagan.")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ApiError(400, "invalid_prompt", f"'prompt' {MAX_PROMPT_CHARS} belgidan oshmasligi kerak.",
                       en=f"`prompt` must be at most {MAX_PROMPT_CHARS} characters.")
    seconds, size = parse_seconds(body.get("seconds")), parse_size(body.get("size"))
    settings = await run_in_threadpool(db.get_settings)
    if not (settings.get("cluster_base_url") or "").strip():
        raise ApiError(503, "model_unavailable", "Bu model hozircha mavjud emas.", "server_error")
    free_mode = settings.get("free_mode") == "1"
    job = {"id": job_id, "user_id": key["user_id"], "key_id": key["id"], "model": model["id"],
           "public_model": public_id, "upstream": model["upstream_name"], "prompt": prompt, "input_image": image,
           "prompt_chars": len(prompt), "status": "queued", "progress": 0, "seconds": seconds, "size": size,
           "error": None, "free": int(bool(key["free"])),
           "price": 0 if free_mode else billing.cost_for(CLIP_TOKENS, model["price_out_per_1m"]),
           "ip": client_ip(request), "created_at": now(), "completed_at": None}
    try:
        await run_in_threadpool(insert_job, db, job, monthly_limit=key["monthly_limit"], max_active=cfg.per_key_concurrent)
    except billing.BillingError as e:
        raise ApiError(e.status, e.code, e.message, "insufficient_quota" if e.status == 402 else "rate_limit_error")
    return job


@router.post("/videos")
async def create_video(request: Request):
    state = request.app.state
    job_id = "video_" + uuid.uuid4().hex[:24]
    key = await run_in_threadpool(gateway.lookup_key, state.db, request)
    ctx = {"model": "-", "stream": False}
    try:
        await gateway.check_rate(request, key)
        job = await _admit(request, key, job_id, ctx)
    except ApiError as exc:
        exc.headers = {**exc.headers, "X-Request-Id": job_id}
        await gateway._log_rejection(state, key, job_id, "videos", ctx, exc, client_ip(request))
        raise
    wake(state)
    return JSONResponse(video_object(job), headers={"X-Request-Id": job_id})


async def _reader(request: Request) -> dict:
    """Key check for status, content and list reads. They get their own per-key window: an SDK
    polls every second or two while a clip renders, and that must not eat the key's request limit."""
    state = request.app.state
    key = await run_in_threadpool(gateway.lookup_key, state.db, request)
    ok, retry = state.video_poll.allow(key["id"], POLL_RPM)
    if not ok:
        raise ApiError(429, "rate_limit_exceeded",
                       f"Daqiqasiga {POLL_RPM} ta so'rov chegarasi oshdi. {retry} soniyadan keyin urinib ko'ring.",
                       "rate_limit_error", {"Retry-After": str(retry)},
                       en=f"Rate limit of {POLL_RPM} video status reads per minute exceeded. Try again in {retry} s.")
    return key


def _not_found() -> ApiError:
    return ApiError(404, "video_not_found", "Video topilmadi.", en="No video with this id was found.")


def _own_job(db: Database, video_id: str, user_id: int) -> dict:
    row = db.one("SELECT * FROM video_jobs WHERE id=? AND user_id=?", (video_id, user_id)) if _ID.fullmatch(video_id) else None
    if row is None:
        raise _not_found()
    return dict(row)


@router.get("/videos")
async def list_videos(request: Request, limit: int = 20, after: str | None = None, order: str = "desc"):
    key = await _reader(request)
    db: Database = request.app.state.db
    limit = min(max(limit, 1), 50)
    desc = order != "asc"
    where, args = "user_id=?", [key["user_id"]]
    if after:
        cursor = await run_in_threadpool(_own_job, db, after, key["user_id"])
        where += " AND seq < ?" if desc else " AND seq > ?"
        args.append(cursor["seq"])
    rows = await run_in_threadpool(
        db.all, f"SELECT * FROM video_jobs WHERE {where} ORDER BY seq {'DESC' if desc else 'ASC'} LIMIT ?",
        (*args, limit + 1))
    data = [video_object(dict(r)) for r in rows[:limit]]
    return {"object": "list", "data": data, "first_id": data[0]["id"] if data else None,
            "last_id": data[-1]["id"] if data else None, "has_more": len(rows) > limit}


@router.get("/videos/{video_id}")
async def retrieve_video(video_id: str, request: Request):
    key = await _reader(request)
    return video_object(await run_in_threadpool(_own_job, request.app.state.db, video_id, key["user_id"]))


@router.get("/videos/{video_id}/content")
async def video_content(video_id: str, request: Request, variant: str = "video"):
    key = await _reader(request)
    if variant != "video":
        raise ApiError(400, "invalid_request", "Faqat variant=video mavjud.", en="Only variant=video is available.")
    db: Database = request.app.state.db
    job = await run_in_threadpool(_own_job, db, video_id, key["user_id"])
    if job["status"] != "completed":
        raise ApiError(404, "video_not_ready", "Video hali tayyor emas.",
                       en=f"The video is not ready: its status is {job['status']}.")
    row = await run_in_threadpool(db.one, "SELECT mime, data FROM video_files WHERE job_id=?", (video_id,))
    if row is None or job["completed_at"] + FILE_TTL <= now():
        raise ApiError(404, "video_expired", "Video muddati tugagan va o'chirilgan.",
                       en="The video has expired and was deleted.")
    return Response(bytes(row["data"]), media_type=row["mime"],
                    headers={"Content-Disposition": f'attachment; filename="{video_id}.mp4"'})
