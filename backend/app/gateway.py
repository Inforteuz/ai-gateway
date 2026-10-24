"""/v1/*: OpenAI-compatible API. Checks the key, reserves funds, forwards to Ollama, charges for the actual tokens."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import re
import time
import uuid
from dataclasses import dataclass, field

import anyio
import httpx
from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse

from . import admission, upstream_errors, local_guard, attribution, abuse, billing, branding, catalog, directions, fastpath, loops, refine, security, untrusted
from .common import ApiError, client_ip
from .db import Database, now

router = APIRouter(prefix="/v1", tags=["gateway"])

log = logging.getLogger("gateway")

CHARS_PER_TOKEN_EST = 2.5   # output estimate when no token count is returned
HOLD_CHARS_PER_TOKEN = 2.0  # conservative estimate for the hold (assume more tokens)


def fmt_money(micro: int) -> str:
    return f"{micro / billing.MICRO:.6f}".rstrip("0").rstrip(".") or "0"


# --- key lookup ----------------------------------------------------------------------------------------------------
def lookup_key(db: Database, request: Request) -> dict:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer ") or len(auth) < 15:
        raise ApiError(401, "invalid_api_key",
                       "API kalit berilmagan. 'Authorization: Bearer <kalit>' sarlavhasini yuboring.",
                       "authentication_error")
    row = db.one(
        """SELECT k.id, k.user_id, k.name, k.status, k.rpm_limit, k.monthly_limit, k.free, k.reasoning_effort,
                  u.status AS user_status, w.balance, w.held
           FROM api_keys k JOIN users u ON u.id = k.user_id LEFT JOIN wallets w ON w.user_id = k.user_id
           WHERE k.key_hash = ?""",
        (security.sha256_hex(auth[7:].strip()),),
    )
    if row is None:
        raise ApiError(401, "invalid_api_key", "API kalit noto'g'ri.", "authentication_error")
    if row["status"] != "active":
        raise ApiError(401, "revoked_api_key", "Bu API kalit bekor qilingan.", "authentication_error")
    if row["user_status"] != "active":
        raise ApiError(403, "account_suspended", "Hisob to'xtatilgan. Administrator bilan bog'laning.", "permission_error")
    return dict(row)


# On a short burst we wait up to this many seconds instead of returning 429.
# Coding agents and benchmarks fire dozens of requests at once: rejecting them
# stops the work entirely, while waiting a second or two goes unnoticed.
RPM_BURST_WAIT = 6


async def check_rate(request: Request, key: dict) -> None:
    state = request.app.state
    limit = key["rpm_limit"] or state.db.setting_int("default_rpm")

    ok, retry = state.rpm.allow(key["id"], limit)
    if not ok and retry <= RPM_BURST_WAIT:
        # The window frees up soon — wait instead of rejecting
        await asyncio.sleep(retry)
        ok, retry = state.rpm.allow(key["id"], limit)
    if not ok:
        remaining = max(0, limit - state.rpm.count(key["id"]))
        raise ApiError(
            429, "rate_limit_exceeded",
            f"Daqiqasiga {limit} ta so'rov chegarasi oshdi. {retry} soniyadan keyin urinib ko'ring.",
            "rate_limit_error",
            {"Retry-After": str(retry),
             "X-RateLimit-Limit": str(limit),
             "X-RateLimit-Remaining": str(remaining),
             "X-RateLimit-Reset": str(retry)})
    # Per-IP limit and shared-key check. No hard block:
    # a key used from many IPs is only slowed down.
    flag = abuse.check_request(state, key, client_ip(request))
    if flag:
        log.warning("key=%s %s", key["id"], flag)


def tokens(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def free_limit_error(quota: billing.FreeQuota) -> ApiError:
    retry = max(1, quota.resets_at - now())
    hours, minutes = divmod(-(-retry // 60), 60)
    wait = f"{hours} soat {minutes} daqiqa" if hours else f"{minutes} daqiqa"
    wait_en = f"{hours}h {minutes}m" if hours else f"{minutes}m"
    names = ", ".join(quota.models)
    remaining = max(0, quota.limit - quota.used)
    if quota.requested and remaining > 0:
        return ApiError(
            429, "free_daily_limit",
            f"Bepul {', '.join(quota.models)} modelining bugungi limitidan {tokens(remaining)} token qoldi, bu so'rov "
            f"esa {tokens(quota.requested)} tokengacha ishlatishi mumkin (prompt + max_tokens). max_tokens ni kamaytiring "
            f"yoki limit Toshkent vaqti bilan 00:00 da yangilanishini kuting ({wait}dan keyin).",
            "rate_limit_error", {"Retry-After": str(retry)},
            en=(f"Only {tokens(remaining)} tokens are left of today's free allowance for {names}, and this "
                f"request could use up to {tokens(quota.requested)} (prompt + max_tokens). Lower max_tokens, "
                f"or wait {wait_en} for the allowance to reset at 00:00 Asia/Tashkent."))
    return ApiError(
        429, "free_daily_limit",
        f"Bepul {', '.join(quota.models)} modelining kunlik limiti tugadi: bugun {tokens(quota.used)} / "
        f"{tokens(quota.limit)} token ishlatildi. Limit Toshkent vaqti bilan soat 00:00 da yangilanadi "
        f"({wait}dan keyin). Hisobingizni to'ldirsangiz, bu cheklov butunlay olib tashlanadi.",
        "rate_limit_error", {"Retry-After": str(retry)},
        en=(f"Today's free allowance for {names} is used up: {tokens(quota.used)} of {tokens(quota.limit)} "
            f"tokens. It resets at 00:00 Asia/Tashkent, in {wait_en}. Adding funds removes the limit."))


# --- request state ---------------------------------------------------------------------------------------------------
@dataclass(eq=False)
class Ticket:
    state: object
    request_id: str
    key: dict
    model_id: str
    endpoint: str
    price_in: int
    price_out: int
    reserved: int
    stream: bool
    ip: str
    t0: float
    est_prompt: int
    ttfb_ms: int | None = None
    done: bool = False
    sem_held: bool = False
    key_slot: bool = False
    started: float = field(default_factory=time.monotonic)
    resp: object = None  # lets the janitor close a stream left open
    reasoning: str = ""
    sem: object = None            # which semaphore is held (local or external)
    refined: str | None = None    # verification result
    free_mode: bool = False       # free mode: price is always 0
    reasoning_auto: bool = False  # we set the reasoning level (the client did not)
    retried: bool = False         # retried without thinking because the answer was empty
    refine_ms: int | None = None
    ext_held: bool = False        # external provider slot (held during streaming verification)
    attribution: dict = field(default_factory=dict)

    def release_ext(self) -> None:
        if self.ext_held:
            self.ext_held = False
            self.state.ext_sem.release()

    def release_sem(self) -> None:
        if self.sem_held:
            self.sem_held = False
            self.sem.release()

    def _cleanup(self) -> None:
        st = self.state
        self.release_sem()
        self.release_ext()
        if self.key_slot:
            self.key_slot = False
            _release_key_slot(st, self.key["id"])
        st.open_tickets.discard(self)

    async def finish(self, *, status_code: int, prompt_tokens: int = 0, completion_tokens: int = 0,
                     estimated: bool = False, error: str | None = None) -> billing.Settled | None:
        """Runs once (later calls are ignored). Completes fully even during cancellation."""
        if self.done:
            return None
        self.done = True
        try:
            with anyio.CancelScope(shield=True):
                return await run_in_threadpool(
                    billing.settle, self.state.db,
                    request_id=self.request_id, user_id=self.key["user_id"], key_id=self.key["id"],
                    model=self.model_id, endpoint=self.endpoint, reserved=self.reserved, free=bool(self.key["free"]),
                    price_in=self.price_in, price_out=self.price_out,
                    prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, status_code=status_code,
                    stream=self.stream, estimated=estimated, latency_ms=int((time.monotonic() - self.t0) * 1000),
                    ttfb_ms=self.ttfb_ms, error=error or ("auto_retry_no_thinking" if self.retried and status_code == 200 else None), ip=self.ip, reasoning=self.reasoning,
                    refined=self.refined, refine_ms=self.refine_ms, attribution=attribution.persisted(self.attribution),
                )
        finally:
            self._cleanup()


# Estimated token cost counted instead of an image/file base64 payload.
# Counting the real base64 length would make a 1 MB image ~350 000 tokens
# and reserve the user's entire balance for a single request.
ATTACHMENT_TOKEN_COST = 800


def _strip_payloads(obj):
    """Replaces base64/URL payloads inside multimodal `content` with a marker."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in ("url", "file_data", "b64_json", "data") and isinstance(v, str) and len(v) > 256:
                out[k] = "<attachment>"
            else:
                out[k] = _strip_payloads(v)
        return out
    if isinstance(obj, list):
        return [_strip_payloads(x) for x in obj]
    return obj


_IMAGE_MAGIC = ((b"\x89PNG", "image/png"), (b"\xff\xd8", "image/jpeg"), (b"GIF8", "image/gif"))


def _sniff_image(data: bytes, declared: str) -> str:
    if declared.startswith("image/"):
        return declared
    for magic, mime in _IMAGE_MAGIC:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def parse_image_edit(raw: bytes, content_type: str) -> dict:
    """Normalises a `/v1/images/edits` body to JSON.

    The OpenAI SDK sends multipart/form-data to this endpoint (`image` or `image[]`
    files + `prompt`, `model`, `n`, `size`). The cluster receives JSON with the images
    as a list of data URIs. A JSON body is accepted too.
    `mask` is not supported yet (web-chat editing has no masks).
    """
    ctype = (content_type or "").lower()
    if not ctype.startswith("multipart/form-data"):
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise ApiError(400, "invalid_body", "So'rov multipart/form-data yoki JSON obyekt bo'lishi kerak.")
        img = body.get("image")
        body["image"] = [x for x in (img if isinstance(img, list) else [img]) if isinstance(x, str) and x]
        return body

    from email.parser import BytesParser
    from email.policy import HTTP
    try:
        msg = BytesParser(policy=HTTP).parsebytes(
            b"Content-Type: " + content_type.encode("latin-1") + b"\r\nMIME-Version: 1.0\r\n\r\n" + raw)
        parts = list(msg.iter_parts()) if msg.is_multipart() else []
    except Exception:
        parts = []
    if not parts:
        raise ApiError(400, "invalid_body", "multipart/form-data tanasini o'qib bo'lmadi.")
    body: dict = {"image": []}
    for part in parts:
        name = part.get_param("name", header="content-disposition") or ""
        data = part.get_payload(decode=True) or b""
        if name in ("image", "image[]"):
            mime = _sniff_image(data, part.get_content_type() if part.get_filename() else "")
            if not mime:
                raise ApiError(400, "invalid_image", "'image' faqat PNG, JPEG, WEBP yoki GIF rasm bo'lishi kerak.")
            body["image"].append(f"data:{mime};base64," + base64.b64encode(data).decode("ascii"))
        elif name in ("prompt", "model", "size", "response_format", "user", "quality"):
            body[name] = data.decode("utf-8", "replace")
        elif name == "n":
            try:
                body["n"] = int(data.decode("ascii", "replace").strip() or 1)
            except ValueError:
                raise ApiError(400, "invalid_n", "'n' butun son bo'lishi kerak.")
    return body


def count_attachments(body: dict) -> int:
    """Counts the image/file parts in the request."""
    n = len(body.get("image") or []) if isinstance(body.get("image"), list) else 0
    for m in body.get("messages") or []:
        content = (m or {}).get("content") if isinstance(m, dict) else None
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and (part.get("type") or "") in (
                    "image_url", "input_image", "file", "input_file", "document"):
                n += 1
    return n


# Measured ratio (qwen3 tokenizer, an agent CLI request): 46 974 characters = 11 359 tokens,
# i.e. ~4.1 chars/token. 4.0 is used — close to the limit, but a request that fits is not rejected.
CONTEXT_CHARS_PER_TOKEN = 4.0


def context_tokens(body: dict) -> int:
    """Estimated context size of the request: messages + tool definitions (+ attachments)."""
    text = json.dumps(_strip_payloads(body.get("messages") or body.get("prompt") or ""), ensure_ascii=False)
    if body.get("tools"):
        text += json.dumps(body["tools"], ensure_ascii=False)
    return math.ceil(len(text) / CONTEXT_CHARS_PER_TOKEN) + count_attachments(body) * ATTACHMENT_TOKEN_COST


def estimate_prompt_tokens(body: dict) -> int:
    src = body.get("messages") if "messages" in body else body.get("prompt", "")
    text = json.dumps(_strip_payloads(src), ensure_ascii=False)
    tokens = math.ceil(len(text) / HOLD_CHARS_PER_TOKEN) + 16
    return tokens + count_attachments(body) * ATTACHMENT_TOKEN_COST


def usage_of(obj: dict | None) -> tuple[int, int] | None:
    u = (obj or {}).get("usage")
    if isinstance(u, dict) and ("prompt_tokens" in u or "completion_tokens" in u):
        return int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
    return None


# Error codes returned by the cluster server that are safe to show clients
# (cluster/server.py and cluster/balancer.py). Their messages contain no provider name or
# internal detail, so both the message and the code are passed through as is.
_SAFE_UPSTREAM_CODES = {
    "unsupported_feature", "invalid_response_format", "invalid_request",
    "model_busy", "queue_full", "model_unavailable", "upstream_failed", "invalid_output",
}


def upstream_error(resp: httpx.Response, body: bytes, model=None) -> tuple[int, str, str, dict[str, str], str]:
    """Maps a model server error response to (status, code, message, headers, english) for the client.

    Previously EVERYTHING outside the 400 group became 502: "queue full, retry
    in 10 seconds" (503 + Retry-After) and "the model did not answer" looked
    the same to the client, and SDKs could not know when to retry.
    """
    msg, code, e = "", "", None
    try:
        j = json.loads(body)
        e = j.get("error")
        if isinstance(e, dict):
            msg, code = e.get("message") or "", str(e.get("code") or "")
        else:
            msg = e or j.get("detail") or ""
    except Exception:
        msg = body[:200].decode("utf-8", "replace")
    msg = str(msg)
    if model is not None and model["upstream_name"]:
        msg = msg.replace(model["upstream_name"], _public_id(model))   # never leak the real model name to the client
    headers: dict[str, str] = {}
    retry_after = resp.headers.get("retry-after")
    if retry_after and retry_after.isdigit():
        headers["Retry-After"] = retry_after
    safe = code in _SAFE_UPSTREAM_CODES

    st = resp.status_code
    localized = upstream_errors.localize(code, e.get("details") if isinstance(e, dict) and isinstance(e.get("details"), dict) else {})
    if localized:
        status = 400 if st in (400, 404, 413, 422) else (503 if st in (429, 503) else 502)
        return status, code, localized[0], headers, localized[1]
    if st in (400, 404, 413, 422):
        # The provider's own words are the useful part here, so they are kept in both
        return (400, (code if safe else "upstream_rejected"), f"Model so'rovni qabul qilmadi: {msg}"[:400],
                headers, f"The model rejected the request: {msg}"[:400])
    if st in (429, 503):
        return (503, (code if safe else "model_busy"), (msg[:300] if safe else
                "Model hozir band. Birozdan keyin qayta urinib ko'ring."), headers or {"Retry-After": "15"},
                "The model is busy. Try again shortly.")
    if st == 504:
        return 504, "upstream_timeout", "Model javob berishga ulgurmadi.", headers, \
            "The model did not answer in time."
    if safe:
        return 502, code, msg[:300], headers, msg[:300]
    return (502, "upstream_error", "Model serveri xato qaytardi. Birozdan keyin qayta urinib ko'ring.",
            headers, "The model server returned an error. Try again shortly.")


def _public_id(model) -> str:
    """Client-visible name: the direction name when the request came through a direction (model-pro ...)."""
    try:
        return model["public_id"]
    except (KeyError, IndexError):
        return model["id"]


def legacy_headers(requested: str, sunset: str = "") -> dict[str, str]:
    """For a legacy name (model-1.0 … 4.0): the recommended direction and (if set) the sunset date.

    Request behaviour is unchanged — headers only. `Deprecation`/`Sunset` are sent only after an admin
    sets `legacy_models_sunset` (YYYY-MM-DD).
    """
    new = directions.replacement(requested)
    if not new:
        return {}
    out = {"X-SF-Replacement": new}
    sunset = (sunset or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", sunset):
        out["Deprecation"] = "true"
        out["Sunset"] = time.strftime("%a, %d %b %Y 00:00:00 GMT", time.strptime(sunset, "%Y-%m-%d"))
        if branding.SITE_URL:
            out["Link"] = f'<{branding.SITE_URL}/docs#modellar>; rel="deprecation"'
    return out


# --- main proxy ----------------------------------------------------------------------------------------------------
async def proxy(request: Request, endpoint: str):
    t0 = time.monotonic()
    state = request.app.state
    request_id = "req_" + uuid.uuid4().hex[:24]

    key = await run_in_threadpool(lookup_key, state.db, request)
    ctx = {"model": "-", "stream": False, "reasoning": "", "reasoning_auto": False}
    try:
        await check_rate(request, key)
        ticket, body, model, max_out, stream = await _prepare(request, endpoint, key, request_id, t0, ctx)
    except ApiError as exc:
        exc.headers = {**exc.headers, "X-Request-Id": request_id}
        await _log_rejection(state, key, request_id, endpoint, ctx, exc, client_ip(request))
        raise
    try:
        response = await _execute(request, endpoint, ticket, body, model, max_out, stream)
    except ApiError as exc:
        stand_in = _stand_in_for(model, exc)
        if not stand_in:
            raise
        log.info("fallback: %s -> %s after %s (%s)", model["id"], stand_in, exc.code, request_id)
        # Its own id: the failed attempt already wrote a usage row under the first one,
        # and request_id is unique there. The suffix keeps the two attempts tied together.
        retry_id = request_id + "f"
        try:
            ticket2, body2, model2, max_out2, stream2 = await _prepare(
                request, endpoint, key, retry_id, t0, ctx, force_model=stand_in)
        except ApiError as exc2:
            exc2.headers = {**exc2.headers, "X-Request-Id": retry_id}
            await _log_rejection(state, key, retry_id, endpoint, ctx, exc2, client_ip(request))
            raise
        # One hop only: the stand-in answers or the request fails.
        response = await _execute(request, endpoint, ticket2, body2, {**model2, "fallback_to": None},
                                  max_out2, stream2)
        response.headers["X-Fallback-Model"] = stand_in
    extra = legacy_headers(ctx.get("requested") or "", ctx.get("legacy_sunset") or "")
    for k, v in extra.items():
        response.headers[k] = v
    return response


def _stand_in_for(model, exc: ApiError) -> str | None:
    """The model to retry on, or None to let the error through.

    Only for failures another model would not share: the provider being offline, busy
    or erroring. A rejected or oversized request, a bad key, an exhausted balance or a
    rate limit would fail exactly the same way on the stand-in, and `server_busy` is
    this gateway being full - retrying would only add to the load.
    """
    if exc.status not in (502, 503, 504) or exc.code == "server_busy":
        return None
    stand_in = model.get("fallback_to") if isinstance(model, dict) else None
    return stand_in if stand_in and stand_in != model.get("id") else None


async def _log_rejection(state, key: dict, request_id: str, endpoint: str, ctx: dict, exc: ApiError, ip: str) -> None:
    """Logs requests that never reached the model so they show in reports (repeated 429s once per minute)."""
    if exc.status == 429:
        mark = (key["id"], exc.code)
        last = state.reject_log_last.get(mark, 0.0)
        if time.monotonic() - last < 60:
            return
        state.reject_log_last[mark] = time.monotonic()
    try:
        await run_in_threadpool(
            billing.log_rejection, state.db, request_id=request_id, user_id=key["user_id"], key_id=key["id"],
            model=ctx["model"], endpoint=endpoint, status_code=exc.status, code=exc.code, stream=ctx["stream"], ip=ip)
    except Exception:
        pass  # the client must get a proper response even if reporting fails


async def _prepare(request: Request, endpoint: str, key: dict, request_id: str, t0: float, ctx: dict,
                   force_model: str | None = None):
    """Validation and the funds hold. Raises ApiError on rejection (which gets logged).

    `force_model` ignores the model the client asked for: it is how a fallback runs the
    same request on the stand-in model, with that model's own price and limits.
    """
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
    if endpoint == "images/edits":
        body = parse_image_edit(raw, request.headers.get("content-type", ""))
    else:
        try:
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            raise ApiError(400, "invalid_json", "So'rov tanasi to'g'ri JSON obyekt bo'lishi kerak.")
    # Only requests with image/file attachments may have a large body
    if len(raw) > cfg.max_body_bytes and not count_attachments(body):
        raise ApiError(413, "request_too_large", "So'rov hajmi juda katta.")

    model_id = force_model or body.get("model")
    ctx["model"] = model_id if isinstance(model_id, str) and model_id else "-"
    ctx["requested"] = model_id if isinstance(model_id, str) else ""
    ctx["stream"] = bool(body.get("stream"))
    # A direction name (model-free/core/code/pro) maps to an existing row: price, privacy and billing come from that row.
    row_id, public_id = directions.resolve(model_id) if isinstance(model_id, str) else (model_id, model_id)
    model = await run_in_threadpool(db.one, "SELECT * FROM models WHERE id=? AND enabled=1", (row_id,)) if isinstance(row_id, str) else None
    if model is None:
        listed = [r["id"] for r in await run_in_threadpool(db.all, "SELECT id FROM models WHERE enabled=1 AND listed=1 ORDER BY id")]
        names = ", ".join(sorted(directions.listed(set(listed))) + listed)
        raise ApiError(404, "model_not_found", f"Model topilmadi. Mavjud modellar: {names}.",
                       en=f"The model `{public_id}` does not exist. Available models: {names}.")
    model = dict(model)
    model["public_id"] = public_id
    if endpoint in ("images/generations", "images/edits"):
        # Image generation/editing exists only on cluster models (local models are text-only)
        if model["provider"] != "cluster":
            raise ApiError(400, "unsupported_endpoint",
                           "Bu model rasm generatsiya qilmaydi.")
    elif (model["provider"] in ("external", "cluster") or model["refine"]) and endpoint != "chat/completions":
        raise ApiError(400, "unsupported_endpoint", "Bu model faqat /v1/chat/completions orqali ishlaydi.")
    if endpoint == "chat/completions" and not isinstance(body.get("messages"), list):
        raise ApiError(400, "invalid_messages", "'messages' ro'yxat bo'lishi kerak.")
    # If an empty list reached the model it would generate "something" and return 200
    if endpoint == "chat/completions" and not body["messages"]:
        raise ApiError(400, "invalid_messages", "'messages' bo'sh bo'lmasligi kerak.")
    if endpoint == "completions" and "prompt" not in body:
        raise ApiError(400, "invalid_prompt", "'prompt' berilmagan.")
    if endpoint in ("images/generations", "images/edits") and not str(body.get("prompt") or "").strip():
        raise ApiError(400, "invalid_prompt", "'prompt' berilmagan.")
    if endpoint == "images/edits" and not body.get("image"):
        raise ApiError(400, "invalid_image", "'image' berilmagan.")

    # max_tokens: always capped, so the worst-case cost is known up front.
    settings = await run_in_threadpool(db.get_settings)
    ctx["legacy_sunset"] = settings.get("legacy_models_sunset") or ""
    if model["provider"] == "external" and not ((settings.get("external_base_url") or "").strip() and (settings.get("external_api_key") or "").strip()):
        raise ApiError(503, "model_unavailable", "Bu model hozircha mavjud emas.", "server_error")
    if model["provider"] == "cluster" and not (settings.get("cluster_base_url") or "").strip():
        raise ApiError(503, "model_unavailable", "Bu model hozircha mavjud emas.", "server_error")
    # Daily token limit on free models (for accounts without a deposit). Over the limit: rejected, not rerouted to another model.
    if model["id"] in billing.free_limit_models(settings) and not key["free"]:
        quota = await run_in_threadpool(billing.free_quota, db, user_id=key["user_id"], settings=settings)
        if quota is not None and quota.exhausted:
            raise free_limit_error(quota)
    default_max, hard_max = int(settings["default_max_tokens"]), int(settings["max_max_tokens"])
    requested = body.get("max_tokens", body.get("max_completion_tokens"))
    if requested is None:
        max_out = default_max
    elif isinstance(requested, int) and not isinstance(requested, bool) and requested > 0:
        max_out = min(requested, hard_max)
    else:
        raise ApiError(400, "invalid_max_tokens", "'max_tokens' musbat butun son bo'lishi kerak.")

    # Thinking tokens cost money and time. Priority: request value > key setting > global default.
    reasoning = ""
    if endpoint == "chat/completions":
        if model["provider"] == "local" and not model["allow_thinking"]:
            # Thinking is off on this model: always 'none' whatever the client asks (agents usually send "high": 5-6x slower and pricier).
            body = {k: v for k, v in body.items() if k not in ("think", "reasoning")} | {"reasoning_effort": "none"}
            reasoning = "none"
        elif any(k in body for k in ("reasoning_effort", "think", "reasoning")):
            r = body.get("reasoning_effort")
            reasoning = r if isinstance(r, str) else ("think" if body.get("think") else "")
        else:
            level = key.get("reasoning_effort") or model["default_reasoning"] or settings.get("default_reasoning_effort", "")
            if model["provider"] in ("external", "cluster"):
                level = ""   # no default reasoning level for external and cluster providers
            if level and level != "model":
                body = {**body, "reasoning_effort": level}
                reasoning = level
                ctx["reasoning_auto"] = level != "none"
            elif level == "model":
                reasoning = "model"
    ctx["reasoning"] = reasoning

    stream = bool(body.get("stream"))
    est_prompt = estimate_prompt_tokens(body)
    # Ollama SILENTLY truncates the start of a request larger than the local model's context: the model
    # loses the system prompt and tool format, then writes tool calls as text or loops
    # (seen with Qwen Code). Now a proper OpenAI error is returned — many CLIs
    # react to `context_length_exceeded` by compacting the conversation themselves.
    if model["provider"] == "local" and model.get("context_window"):
        need = context_tokens(body)
        if need > int(model["context_window"]):
            raise ApiError(400, "context_length_exceeded",
                           f"This model's maximum context length is {model['context_window']} tokens, "
                           f"but the request needs about {need} tokens. Modelning maksimal konteksti "
                           f"{model['context_window']} token, so'rov taxminan {need} token — suhbatni qisqartiring "
                           f"yoki kattaroq kontekstli modelni tanlang.")
    free_mode = settings.get("free_mode") == "1"       # free mode: nothing is charged and no balance is required
    price_in, price_out = (0, 0) if free_mode else (model["price_in_per_1m"], model["price_out_per_1m"])
    hold = billing.cost_for(est_prompt, price_in) + billing.cost_for(max_out, price_out)

    # Concurrent requests per key. The local counter is checked and taken with no await in between
    # (atomic within this process); reserve() repeats the check in the database across processes.
    if state.inflight.get(key["id"], 0) >= cfg.per_key_concurrent:
        raise ApiError(429, "too_many_concurrent", f"Bir vaqtda {cfg.per_key_concurrent} tadan ortiq so'rov yuborib bo'lmaydi.",
                       "rate_limit_error", {"Retry-After": "2"})
    state.inflight[key["id"]] = state.inflight.get(key["id"], 0) + 1

    quota_limit = 0
    if model["id"] in billing.free_limit_models(settings) and not key["free"]:
        quota_limit = int(settings.get("free_daily_token_limit") or 0)
        if quota_limit > 0 and await run_in_threadpool(billing.has_deposit, db, key["user_id"]):
            quota_limit = 0
    admitted = False
    try:
        reserved = await run_in_threadpool(
            billing.reserve, db, request_id=request_id, user_id=key["user_id"], key_id=key["id"], model=model["id"],
            amount=hold, tokens=est_prompt + max_out, free=bool(key["free"]), monthly_limit=key["monthly_limit"],
            owner=state.instance_id, ttl=reservation_ttl(cfg), max_concurrent=cfg.per_key_concurrent,
            quota_limit=quota_limit, quota_models=billing.free_limit_models(settings))
        admitted = True
    except billing.FreeQuotaExceeded as e:
        raise free_limit_error(e.quota)
    except billing.BillingError as e:
        headers = {"Retry-After": "2"} if e.code == "too_many_concurrent" else None
        raise ApiError(e.status, e.code, e.message, "insufficient_quota" if e.status == 402 else "rate_limit_error", headers)
    finally:
        if not admitted:
            _release_key_slot(state, key["id"])
            # Cancelled while reserve() was running in its thread: the row may exist — drop it (idempotent).
            with anyio.CancelScope(shield=True):
                await run_in_threadpool(billing.release, db, request_id=request_id, user_id=key["user_id"])

    ticket = Ticket(state=state, request_id=request_id, key=key, model_id=model["id"], endpoint=endpoint,
                    price_in=price_in, price_out=price_out, reserved=reserved,
                    stream=stream, ip=client_ip(request), t0=t0, est_prompt=est_prompt, reasoning=ctx["reasoning"],
                    free_mode=free_mode, reasoning_auto=ctx["reasoning_auto"])
    ticket.key_slot = True
    state.open_tickets.add(ticket)
    return ticket, body, model, max_out, stream


def reservation_ttl(cfg) -> int:
    """How long a reservation may live: past the janitor's limit, so a live request is never expired."""
    return janitor_limit(cfg) + 120


def janitor_limit(cfg) -> int:
    return max(cfg.upstream_read_timeout, cfg.max_stream_seconds) + cfg.queue_wait_seconds + 60


def _release_key_slot(state, key_id: int) -> None:
    n = state.inflight.get(key_id, 1) - 1
    if n <= 0:
        state.inflight.pop(key_id, None)
    else:
        state.inflight[key_id] = n


def _target(state, model, settings: dict):
    """Where to send the request: (client, base URL, headers, semaphore)."""
    provider = model["provider"]
    if provider == "external":
        return (state.ext_http, (settings.get("external_base_url") or "").rstrip("/"),
                {"Authorization": f"Bearer {settings.get('external_api_key', '')}", "User-Agent": refine.USER_AGENT}, state.ext_sem)
    if provider == "cluster":
        base = (settings.get("cluster_base_url") or "").rstrip("/")
        headers = {"User-Agent": refine.USER_AGENT}
        key = settings.get("cluster_api_key") or ""
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return state.cluster_http, base, headers, state.cluster_sem
    return state.http, f"{state.config.upstream_url}/v1", {}, state.sem


def _provider_limit(state, provider):
    return {"local": state.config.max_concurrent, "cluster": state.config.cluster_concurrent}.get(provider, state.config.ext_concurrent)


async def _acquire(state, ticket: Ticket, sem) -> None:
    provider = ticket.attribution.get("provider", "local")
    provider = provider if provider in ("local", "cluster") else "external"
    deadline = time.monotonic() + state.config.queue_wait_seconds
    await run_in_threadpool(admission.register, state.db, ticket.request_id, "primary", provider)
    try:
        await asyncio.wait_for(sem.acquire(), timeout=max(.001, deadline - time.monotonic()))
        ticket.sem, ticket.sem_held = sem, True
        if not await admission.wait(state.db, ticket.request_id, "primary", provider, _provider_limit(state, provider), deadline):
            raise asyncio.TimeoutError()
    except asyncio.TimeoutError:
        await ticket.finish(status_code=503, error="server_busy")
        raise ApiError(503, "server_busy", "Model band. Birozdan keyin qayta urinib ko'ring.", "server_error", {"Retry-After": "10"})


async def _acquire_verification(state, ticket):
    deadline = time.monotonic() + state.config.queue_wait_seconds
    await run_in_threadpool(admission.register, state.db, ticket.request_id, "verification", "external")
    try:
        await asyncio.wait_for(state.ext_sem.acquire(), timeout=max(.001, deadline - time.monotonic()))
        ticket.ext_held = True
        if await admission.wait(state.db, ticket.request_id, "verification", "external", state.config.ext_concurrent, deadline):
            return True
    except asyncio.TimeoutError:
        pass
    await _release_verification(state, ticket)
    return False


async def _release_verification(state, ticket):
    await run_in_threadpool(admission.release, state.db, ticket.request_id, "verification")
    ticket.release_ext()


async def _send(state, ticket: Ticket, client, url: str, headers: dict, payload: dict, stream: bool, model) -> httpx.Response:
    """Sends the request to the model. On failure it closes the ticket (nothing is charged) and raises ApiError."""
    # When streaming, `read` is the silence limit between chunks (a hung request must not hold a slot).
    read = state.config.stream_idle_timeout if stream else state.config.upstream_read_timeout
    timeout = httpx.Timeout(connect=5.0, read=float(read), write=30.0, pool=5.0)
    try:
        guarded = (model["provider"] == "local" and not stream and url.endswith("/chat/completions")
                   and (payload.get("reasoning_effort") in ("medium", "high") or payload.get("think") is True
                        or isinstance(payload.get("reasoning"), dict)))
        outgoing = {**payload, "stream": True, "stream_options": {"include_usage": True}} if guarded else payload
        req = client.build_request("POST", url, json=outgoing, headers=headers, timeout=timeout)
        ticket.attribution["attempts"] = ticket.attribution.get("attempts", 0) + 1
        resp = await client.send(req, stream=stream or guarded)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        await ticket.finish(status_code=503, error="upstream_unavailable")
        raise ApiError(503, "model_offline", "Model serveri hozir ulanmagan. Birozdan keyin qayta urinib ko'ring.", "server_error",
                       {"Retry-After": "30"})
    except httpx.TimeoutException:
        await ticket.finish(status_code=504, error="upstream_timeout")
        raise ApiError(504, "upstream_timeout", "Model javob berishga ulgurmadi.", "server_error")
    except httpx.HTTPError as e:
        await ticket.finish(status_code=502, error=f"upstream_error: {type(e).__name__}")
        raise ApiError(502, "upstream_error", "Model serveri bilan aloqada xatolik.", "server_error")
    if resp.status_code != 200:
        err_body = await resp.aread()
        await resp.aclose()
        status, code, message, err_headers, message_en = upstream_error(resp, err_body, model)
        await ticket.finish(status_code=status, error=f"upstream_{resp.status_code}:{code}")
        raise ApiError(status, code, message,
                       "invalid_request_error" if status == 400 else "server_error", err_headers,
                       en=message_en)
    if guarded:
        try:
            data = await local_guard.collect(resp, min(state.config.max_stream_seconds, state.config.local_generation_seconds),
                                             state.config.local_reasoning_seconds)
        except local_guard.GuardStopped as exc:
            ticket.attribution["guard_reason"] = str(exc)
            data = {"choices": [{"message": {"role": "assistant", "content": ""}, "finish_reason": "length"}],
                    "gateway_guard": str(exc)}
        except httpx.TimeoutException:
            await resp.aclose()
            data = {"choices": [{"message": {"role": "assistant", "content": ""}, "finish_reason": "length"}],
                    "gateway_guard": "reasoning_budget_exceeded"}
        resp = httpx.Response(200, json=data, request=req)
    return resp


async def _retry_if_starved(state, ticket: Ticket, client, url: str, headers: dict, payload: dict, model, resp: httpx.Response) -> httpx.Response:
    """If thinking tokens ate the whole budget and the answer is empty (finish_reason=length), retries once without thinking.
    Only when we set the reasoning level. The first (empty) attempt is not charged: billing covers the retry only."""
    if model["provider"] != "local" or ticket.retried:
        return resp
    try:
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        content = ((choice.get("message") or {}).get("content") or "").strip()
        starved = not content and choice.get("finish_reason") == "length" and not (choice.get("message") or {}).get("tool_calls")
        repeated = any(loops.is_looping((choice.get("message") or {}).get(k) or "") for k in ("content", "reasoning", "reasoning_content"))
        starved = bool(data.get("gateway_guard")) or repeated or (starved and ticket.reasoning_auto)
    except Exception:
        return resp
    if not starved:
        return resp
    ticket.reasoning, ticket.reasoning_auto, ticket.retried = "none", False, True
    return await _send(state, ticket, client, url, headers,
                       {k: v for k, v in payload.items() if k not in ("think", "reasoning")} | {"reasoning_effort": "none"}, False, model)


def _scrub(obj: dict, public_model: str) -> dict:
    """Keeps the real model name and service traces away from the client."""
    if "model" in obj:
        obj["model"] = public_model
    obj.pop("system_fingerprint", None)
    obj.pop("timings", None)             # Ollama's internal timing stats
    return obj


async def _execute(request: Request, endpoint: str, ticket: Ticket, body: dict, model, max_out: int, stream: bool):
    state = request.app.state
    request_id = ticket.request_id
    if not ticket.attribution:
        ticket.attribution = attribution.request_meta(request.headers, body)
    try:
        settings = await run_in_threadpool(state.db.get_settings)
        # Fast path: a trivial message to a web-chat model ("Hey", "ishlayapsanmi", "sen kimsan")
        # is answered by the fast GPU model — decided BEFORE queueing, otherwise it would also
        # wait 30-60 s in the web window queue (app/fastpath.py). Priced as the requested model.
        fast = (endpoint == "chat/completions" and model["provider"] == "cluster"
                and model["upstream_name"].lower() not in ("deepseek", "deepseek-chat", "deepseek-reasoner", "deepseek-v3", "deepseek-r1")
                and settings.get("fastpath_enabled", "1") == "1" and fastpath.eligible(body))
        if fast:
            model = {**model, "provider": "local", "refine": 0,
                     "upstream_name": settings.get("fastpath_model") or "qwen3:14b"}
            log.info("fast path: %s -> %s (%s)", model.get("public_id") or model["id"],
                     model["upstream_name"], request_id)
        client, base, headers, sem = _target(state, model, settings)
        ticket.attribution.update(provider=model["provider"], route_model=_public_id(model))
        queued_at = time.monotonic()
        await _acquire(state, ticket, sem)
        ticket.attribution["queue_ms"] += round((time.monotonic() - queued_at) * 1000)
        ticket.attribution.update(provider=model["provider"], route_model=_public_id(model))

        payload = fastpath.payload(body, model["upstream_name"], max_out) if fast else dict(body)
        payload["model"] = model["upstream_name"]
        payload["max_tokens"] = min(max_out, payload.get("max_tokens") or max_out)
        payload.pop("max_completion_tokens", None)
        # Clients cannot send internal cluster fields (e.g. a forced model variant)
        for k in [k for k in payload if k.startswith("sf_")]:
            payload.pop(k)
        if model["provider"] == "cluster":
            payload["sf_allow_fallback"] = settings.get("cluster_fallback_enabled") == "1"
        if model["provider"] == "local" and endpoint == "chat/completions":
            # Local models skip the cluster compose layer — the rule against instructions
            # embedded in documents is added here (app/untrusted.py).
            payload["messages"] = untrusted.protect(payload.get("messages"))

        if model["refine"] and endpoint == "chat/completions":
            return await _execute_refined(state, ticket, body, payload, model, max_out, stream, settings, (client, base, headers))

        wants_usage = bool((body.get("stream_options") or {}).get("include_usage")) if stream else False
        if stream and model["provider"] == "local":
            payload["stream_options"] = {**(body.get("stream_options") or {}), "include_usage": True}

        resp = await _send(state, ticket, client, f"{base}/{endpoint}", headers, payload, stream, model)
        if not stream:
            resp = await _retry_if_starved(state, ticket, client, f"{base}/{endpoint}", headers, payload, model, resp)
            return await _respond_json(resp, ticket, _public_id(model))
        ticket.resp = resp
        return StreamingResponse(
            _stream_body(resp, ticket, wants_usage, _public_id(model)),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Request-Id": request_id},
        )
    except BaseException as exc:
        if isinstance(exc, ApiError):
            exc.headers = {**exc.headers, "X-Request-Id": request_id}
        # Error or client disconnect: release the hold and the semaphore.
        if not ticket.done:
            await ticket.finish(status_code=499 if isinstance(exc, asyncio.CancelledError) else 500,
                                error="cancelled" if isinstance(exc, asyncio.CancelledError) else "internal_error")
        raise


async def _execute_refined(state, ticket: Ticket, body: dict, payload: dict, model, max_out: int, stream: bool,
                           settings: dict, target):
    """Draft (local model) -> external verification -> final answer to the client."""
    client, base, headers = target
    if stream:
        req_ok, req_why = refine.request_eligible(body)
        if not req_ok:
            return await _stream_unverified(state, ticket, body, payload, model, target, req_why)
    payload["stream"] = False
    payload.pop("stream_options", None)
    wants_usage = bool((body.get("stream_options") or {}).get("include_usage")) if stream else False

    resp = await _send(state, ticket, client, f"{base}/chat/completions", headers, payload, False, model)
    resp = await _retry_if_starved(state, ticket, client, f"{base}/chat/completions", headers, payload, model, resp)
    ticket.ttfb_ms = int((time.monotonic() - ticket.t0) * 1000)
    try:
        data = resp.json()
    except Exception:
        await ticket.finish(status_code=502, error="bad_upstream_json")
        raise ApiError(502, "upstream_error", "Model serveri noto'g'ri javob qaytardi.", "server_error")
    await run_in_threadpool(admission.release, state.db, ticket.request_id, "primary")
    ticket.release_sem()   # the GPU is free now: verification runs externally

    attribution.observe(ticket.attribution, data)
    usage = usage_of(data)
    estimated = usage is None
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    if usage is None:
        usage = (ticket.est_prompt, max(1, math.ceil(len(json.dumps(data.get("choices", ""), ensure_ascii=False)) / CHARS_PER_TOKEN_EST)))

    final, why, ms, cand = None, "", 0, None
    ok, why = refine.eligible(body, message, choice.get("finish_reason"))
    if ok:
        refine_tokens = min(int(settings["max_max_tokens"]), max(512, usage[1] * 2 + 256))
        if not await _acquire_verification(state, ticket):
            why = "verification_busy"
        elif stream:
            try:
                cand, why, ms = await refine.open_stream(state.ext_http, settings, body.get("messages") or [], message["content"], refine_tokens)
            finally:
                if cand is None:
                    await _release_verification(state, ticket)
        else:
            try:
                final, why, ms = await refine.refine_answer(state.ext_http, settings, body.get("messages") or [], message["content"], refine_tokens)
            finally:
                await _release_verification(state, ticket)
    verified = final is not None or cand is not None
    ticket.refined = "ok" if verified else f"fallback:{why}"
    ticket.refine_ms = ms if ok else None   # only when verification was actually attempted (streaming: up to the first chunk)
    if cand is not None:
        ticket.ttfb_ms = int((time.monotonic() - ticket.t0) * 1000)
        ticket.resp = cand.resp
        return StreamingResponse(
            _refined_stream_body(cand, ticket, data, usage, estimated, wants_usage, _public_id(model)), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Request-Id": ticket.request_id, "X-SF-Verified": "true"})
    if final is not None:
        message["content"] = final
        choice["message"] = message
    else:
        await _apply_fallback_price(state, ticket, model)

    settled = await ticket.finish(status_code=200, prompt_tokens=usage[0], completion_tokens=usage[1], estimated=estimated)
    _scrub(data, _public_id(model))
    data["gateway"] = {"verified": verified}
    out_headers = {"X-Request-Id": ticket.request_id, "X-SF-Verified": "true" if verified else "false"}
    if settled is not None:
        out_headers["X-SF-Cost"] = fmt_money(settled.cost)
        out_headers["X-SF-Balance"] = fmt_money(settled.balance_after)
    if not stream:
        return JSONResponse(data, headers=out_headers)
    return StreamingResponse(_replay_stream(data, wants_usage, usage), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", **out_headers})


async def _apply_fallback_price(state, ticket: Ticket, model) -> None:
    """When verification did not run, the price comes from the `fallback_price_from` model (stays 0 in free mode)."""
    if model["fallback_price_from"] and not ticket.free_mode:
        base_model = await run_in_threadpool(state.db.one, "SELECT price_in_per_1m, price_out_per_1m FROM models WHERE id=?", (model["fallback_price_from"],))
        if base_model:
            ticket.price_in, ticket.price_out = base_model["price_in_per_1m"], base_model["price_out_per_1m"]


async def _stream_unverified(state, ticket: Ticket, body: dict, payload: dict, model, target, why: str):
    """A streaming request that cannot be verified (tools, JSON mode, images, n>1): the local model's answer is streamed
    directly. Buffering the draft and replaying it would lose function calls (tool_calls)."""
    client, base, headers = target
    wants_usage = bool((body.get("stream_options") or {}).get("include_usage"))
    payload["stream"] = True
    payload["stream_options"] = {**(body.get("stream_options") or {}), "include_usage": True}
    ticket.refined, ticket.refine_ms = f"fallback:{why}", None
    await _apply_fallback_price(state, ticket, model)
    resp = await _send(state, ticket, client, f"{base}/chat/completions", headers, payload, True, model)
    ticket.resp = resp
    return StreamingResponse(_stream_body(resp, ticket, wants_usage, _public_id(model)), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Request-Id": ticket.request_id, "X-SF-Verified": "false"})


async def _refined_stream_body(cand: refine.RefineStream, ticket: Ticket, data: dict, usage: tuple[int, int], estimated: bool,
                               wants_usage: bool, public_model: str):
    """Streams the verifier's answer. Billing closes when the stream ends (on the draft's tokens, as before)."""
    meta = {"id": data.get("id", "chatcmpl-sf"), "object": "chat.completion.chunk",
            "created": data.get("created") or int(time.time()), "model": public_model}

    def sse(obj: dict) -> bytes:
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()

    def err_event(message: str, code: str) -> bytes:
        return sse({"error": {"message": message, "type": "server_error", "code": code}})

    cfg = ticket.state.config
    error: str | None = None
    started = time.monotonic()
    try:
        yield sse({**meta, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]})
        async for piece in cand.pieces():
            yield sse({**meta, "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]})
            if time.monotonic() - started > cfg.max_stream_seconds:
                error = "stream_too_long"
                yield err_event("Javob juda uzoq davom etdi va to'xtatildi.", "stream_too_long")
                break
        if error is None:
            yield sse({**meta, "choices": [{"index": 0, "delta": {}, "finish_reason": cand.finish_reason or "stop"}]})
            if wants_usage:
                yield sse({**meta, "choices": [], "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1], "total_tokens": usage[0] + usage[1]}})
            yield b"data: [DONE]\n\n"
    except httpx.TimeoutException:
        error = "stream_stalled"
        yield err_event("Tekshiruvchi model jim qoldi, javob to'liq emas.", "stream_stalled")
    except (httpx.HTTPError, OSError) as e:
        error = f"stream_interrupted: {type(e).__name__}"
        yield err_event("Tekshiruvchi model bilan aloqa uzildi, javob to'liq emas.", "stream_interrupted")
    except BaseException:
        error = "client_disconnected"
        raise
    finally:
        await cand.aclose()
        if error:
            ticket.refined = f"partial:{error.split(':')[0]}"
        await ticket.finish(status_code=200, prompt_tokens=usage[0], completion_tokens=usage[1], estimated=estimated, error=error)


def _replay_stream(data: dict, wants_usage: bool, usage: tuple[int, int]):
    """Emits a finished answer as an OpenAI stream (SSE)."""
    choice = (data.get("choices") or [{}])[0]
    text = (choice.get("message") or {}).get("content") or ""
    base = {"id": data.get("id", "chatcmpl-sf"), "object": "chat.completion.chunk",
            "created": data.get("created") or int(time.time()), "model": data.get("model")}

    def sse(obj: dict) -> bytes:
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()

    yield sse({**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]})
    message = choice.get("message") or {}
    if message.get("reasoning_content"):
        yield sse({**base, "choices": [{"index": 0, "delta": {"reasoning_content": message["reasoning_content"]}, "finish_reason": None}]})
    if message.get("tool_calls"):
        calls = [{**tc, "index": i} for i, tc in enumerate(message["tool_calls"]) if isinstance(tc, dict)]
        yield sse({**base, "choices": [{"index": 0, "delta": {"tool_calls": calls}, "finish_reason": None}]})
    words = re.findall(r"\S+\s*", text) or [text]
    for i in range(0, len(words), 6):
        yield sse({**base, "choices": [{"index": 0, "delta": {"content": "".join(words[i:i + 6])}, "finish_reason": None}]})
    yield sse({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": choice.get("finish_reason") or "stop"}]})
    if wants_usage:
        yield sse({**base, "choices": [], "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1], "total_tokens": usage[0] + usage[1]}})
    yield b"data: [DONE]\n\n"


async def _respond_json(resp: httpx.Response, ticket: Ticket, public_model: str) -> JSONResponse:
    ticket.ttfb_ms = int((time.monotonic() - ticket.t0) * 1000)
    try:
        data = resp.json()
    except Exception:
        await ticket.finish(status_code=502, error="bad_upstream_json")
        raise ApiError(502, "upstream_error", "Model serveri noto'g'ri javob qaytardi.", "server_error")
    attribution.observe(ticket.attribution, data)
    if ticket.attribution.get("provider") == "local":
        messages = [ch.get("message") or {} for ch in data.get("choices") or []]
        if data.get("gateway_guard") or any(loops.is_looping(m.get(k) or "") for m in messages for k in ("content", "reasoning", "reasoning_content")):
            await ticket.finish(status_code=502, error="repetition_loop")
            raise ApiError(502, "generation_failed", "Model javobi takrorlanib qoldi. Qayta urinib ko'ring.", "server_error")
    usage = usage_of(data)
    estimated = usage is None
    if usage is None:  # rare: without usage, estimate from the text length
        text = json.dumps(data.get("choices", ""), ensure_ascii=False)
        usage = (ticket.est_prompt, max(1, math.ceil(len(text) / CHARS_PER_TOKEN_EST)))
    # The provider's fallback model was used: visible in the usage log and a header
    fallback = bool((data.get("gateway") or {}).get("fallback_model")) if isinstance(data, dict) else False
    settled = await ticket.finish(status_code=200, prompt_tokens=usage[0], completion_tokens=usage[1], estimated=estimated,
                                  error="fallback_model" if fallback else None)
    _scrub(data, public_model)
    headers = {"X-Request-Id": ticket.request_id}
    if fallback:
        headers["X-SF-Fallback-Model"] = "1"
    if settled is not None:
        headers["X-SF-Cost"] = fmt_money(settled.cost)
        headers["X-SF-Balance"] = fmt_money(settled.balance_after)
    return JSONResponse(data, headers=headers)


async def _stream_body(resp: httpx.Response, ticket: Ticket, wants_usage: bool, public_model: str):
    usage: tuple[int, int] | None = None
    chars = 0
    first = True
    skip_blank = False
    error: str | None = None
    started = time.monotonic()                 # the duration limit counts from the stream start (queue and verification time excluded)
    tails = {"content": "", "reasoning": ""}   # recent tails for repetition-loop detection
    since_check = 0
    meta: dict = {}
    looped = False
    local = ticket.attribution.get("provider") == "local"
    budget = local_guard.Budget(ticket.state.config.local_reasoning_seconds) if local else None
    seconds = min(ticket.state.config.max_stream_seconds, ticket.state.config.local_generation_seconds) if local else ticket.state.config.max_stream_seconds
    try:
        async for line in local_guard.bounded_lines(resp, seconds, budget):
            if first:
                ticket.ttfb_ms = int((time.monotonic() - ticket.t0) * 1000)
                first = False
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload and payload != "[DONE]":
                    try:
                        obj = json.loads(payload)
                    except ValueError:
                        obj = None
                    if isinstance(obj, dict):
                        if (obj.get("gateway") or {}).get("fallback_model") and error is None:
                            error = "fallback_model"   # note in the usage log (status stays 200)
                        if isinstance(obj.get("error"), dict):
                            # The model server sent an error after the stream started. The event
                            # is passed to the client as is (SDKs raise it as an exception),
                            # and billing records the real reason instead of "client disconnected".
                            error = f"stream_upstream_error:{obj['error'].get('code') or 'unknown'}"
                        if budget:
                            budget.observe(obj, repetition=False)
                        attribution.observe(ticket.attribution, obj)
                        usage = usage_of(obj) or usage
                        meta = {"id": obj.get("id") or meta.get("id"), "created": obj.get("created") or meta.get("created")}
                        for ch in obj.get("choices") or []:
                            d = ch.get("delta") or {}
                            chars += len(d.get("content") or "") + len(d.get("reasoning") or "") + len(d.get("reasoning_content") or "")
                            for kind, text in (("content", d.get("content")), ("reasoning", d.get("reasoning") or d.get("reasoning_content"))):
                                if text:
                                    tails[kind] = (tails[kind] + text)[-loops.TAIL:]
                                    since_check += len(text)
                            for tc in d.get("tool_calls") or []:       # function calls cost tokens too: count them
                                fn = tc.get("function") or {}
                                chars += len(fn.get("name") or "") + len(str(fn.get("arguments") or ""))
                        # Hide the "usage" chunk we added for billing unless the client asked for it.
                        if not wants_usage and not obj.get("choices") and obj.get("usage"):
                            skip_blank = True
                            continue
                        line = "data: " + json.dumps(_scrub(obj, public_model), ensure_ascii=False)
            elif line == "" and skip_blank:
                skip_blank = False
                continue
            yield (line + "\n").encode()
            if since_check >= 150:
                since_check = 0
                if any(loops.is_looping(t) for t in tails.values()):
                    # The model is stuck repeating itself: stop generation (frees the GPU) and end the stream cleanly for the client.
                    looped, error = True, "repetition_loop"
                    done_chunk = {"id": meta.get("id") or "chatcmpl-sf", "object": "chat.completion.chunk", "created": meta.get("created") or int(time.time()),
                                  "model": public_model, "choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]}
                    yield f"\ndata: {json.dumps(done_chunk, ensure_ascii=False)}\n\n".encode()
                    if wants_usage:
                        ct = max(1, math.ceil(chars / CHARS_PER_TOKEN_EST))
                        u = {"id": done_chunk["id"], "object": "chat.completion.chunk", "created": done_chunk["created"], "model": public_model, "choices": [],
                             "usage": {"prompt_tokens": ticket.est_prompt, "completion_tokens": ct, "total_tokens": ticket.est_prompt + ct}}
                        yield f"data: {json.dumps(u, ensure_ascii=False)}\n\n".encode()
                    yield b"data: [DONE]\n\n"
                    break
            if time.monotonic() - started > ticket.state.config.max_stream_seconds:
                error = "stream_too_long"
                err = {"error": {"message": "Javob juda uzoq davom etdi va to'xtatildi.", "type": "server_error", "code": "stream_too_long"}}
                yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n".encode()
                break
    except local_guard.GuardStopped as exc:
        error = str(exc)
        yield ("data: " + json.dumps({"error": {"message": "Generation stopped to prevent a reasoning loop.", "type": "server_error", "code": error}}) + "\n\n").encode()
        yield b"data: [DONE]\n\n"
    except TimeoutError:
        error = "generation_budget_exceeded"
        yield ("data: " + json.dumps({"error": {"message": "Generation time budget exceeded.", "type": "server_error", "code": error}}) + "\n\n").encode()
        yield b"data: [DONE]\n\n"
    except httpx.TimeoutException:
        error = "stream_stalled"
        err = {"error": {"message": f"Model {ticket.state.config.stream_idle_timeout} soniya jim qoldi, so'rov to'xtatildi.", "type": "server_error", "code": "stream_stalled"}}
        yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n".encode()
    except (httpx.HTTPError, OSError) as e:
        error = f"stream_interrupted: {type(e).__name__}"
        err = {"error": {"message": "Model bilan aloqa uzildi.", "type": "server_error", "code": "stream_interrupted"}}
        yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n".encode()
    except BaseException:
        error = "client_disconnected"
        raise
    finally:
        with anyio.CancelScope(shield=True):
            try:
                await resp.aclose()
            except Exception:
                pass
        if usage is not None and not looped:
            await ticket.finish(status_code=200, prompt_tokens=usage[0], completion_tokens=usage[1], error=error)
        elif chars > 0:
            await ticket.finish(status_code=200, prompt_tokens=ticket.est_prompt,
                                completion_tokens=max(1, math.ceil(chars / CHARS_PER_TOKEN_EST)),
                                estimated=True, error=error)
        else:
            await ticket.finish(status_code=502 if error and error.startswith("stream_") else 499,
                                error=error or "empty_stream")


# --- routes ----------------------------------------------------------------------------------------------------
@router.post("/chat/completions")
async def chat_completions(request: Request):
    return await proxy(request, "chat/completions")


@router.post("/completions")
async def completions(request: Request):
    return await proxy(request, "completions")


@router.post("/images/generations")
async def images_generations(request: Request):
    return await proxy(request, "images/generations")


@router.post("/images/edits")
async def images_edits(request: Request):
    return await proxy(request, "images/edits")


@router.get("/models")
async def list_models(request: Request):
    db: Database = request.app.state.db
    key = await run_in_threadpool(lookup_key, db, request)
    await check_rate(request, key)
    rows = await run_in_threadpool(db.all, "SELECT * FROM models WHERE enabled=1 AND listed=1 ORDER BY id")
    s = await run_in_threadpool(db.get_settings)
    hard_max = int(s["max_max_tokens"])
    free = s.get("free_mode") == "1"
    maintenance = frozenset(x.strip() for x in (s.get("maintenance_models") or "").split(",") if x.strip())
    health = await run_in_threadpool(
        catalog.health_map, db, (getattr(request.app.state, "health", None) or {}).get("state"))
    sunset = (s.get("legacy_models_sunset") or "").strip()
    by_id = {r["id"]: r for r in rows}
    data = []
    # Directions (model-free/core/code/pro): with the primary row's price, capabilities and privacy terms
    for d in directions.DIRECTIONS.values():
        if d.primary in by_id:
            m = catalog.openai_model(by_id[d.primary], hard_max, free=free, maintenance=maintenance, health=health)
            m.update(id=d.id, display_name=d.title, description=d.summary, replaces=list(d.replaces))
            m["maintenance"] = d.id in maintenance or m["maintenance"]
            data.append(m)
    for r in rows:
        m = catalog.openai_model(r, hard_max, free=free, maintenance=maintenance, health=health)
        rep = directions.replacement(r["id"])
        if rep:
            m["replacement"] = rep
            m["deprecated"] = bool(sunset)
            if sunset:
                m["sunset"] = sunset
        data.append(m)
    return {"object": "list", "data": data}


@router.get("/balance")
async def balance(request: Request):
    db: Database = request.app.state.db
    key = await run_in_threadpool(lookup_key, db, request)
    await check_rate(request, key)
    w = await run_in_threadpool(db.one, "SELECT balance, held FROM wallets WHERE user_id=?", (key["user_id"],))
    bal, held = (w["balance"], w["held"]) if w else (0, 0)
    settings = await run_in_threadpool(db.get_settings)
    free = settings.get("free_mode") == "1"
    # Account without a deposit: today's usage of the free model (otherwise null — no limit)
    quota = None if key["free"] else await run_in_threadpool(billing.free_quota, db, user_id=key["user_id"], settings=settings)
    return {"currency": "UZS", "balance": bal / billing.MICRO, "held": held / billing.MICRO,
            "available": (bal - held) / billing.MICRO, "unlimited": bool(key["free"]) or free, "free_mode": free,
            "free_daily": quota.out() if quota else None}


async def janitor(app) -> None:
    """Closes requests left open (e.g. the client disconnected before the stream started)."""
    state = app.state
    limit = janitor_limit(state.config)
    while True:
        await asyncio.sleep(60)
        try:
            for t in [t for t in list(state.open_tickets) if time.monotonic() - t.started > limit]:
                await t.finish(status_code=499, error="abandoned")
                if t.resp is not None:
                    try:
                        await t.resp.aclose()
                    except Exception:
                        pass
            state.rpm.prune()
            state.auth_limiter.prune()
            state.signup_limiter.prune()
        except Exception:
            pass
