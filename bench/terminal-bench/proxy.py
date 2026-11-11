"""Bench proxy: paces browser-cluster models, injects the real key, logs every call.

Containers talk to this proxy with a local token; the proxy swaps it for the real
gateway key (read from ~/.config/ai-gateway-bench/key, never logged).
Browser-cluster models: one request at a time, >= GAP seconds from the end of one
request to the start of the next. A streak of captcha / 429 / server_busy stops the
model (later requests get a local 503) until the proxy restarts.
"""
import asyncio
import hashlib
import json
import os
import pathlib
import sys
import time
import uuid

from aiohttp import ClientSession, ClientTimeout, web

UPSTREAM = os.environ["BENCH_UPSTREAM"]
KEY = pathlib.Path(os.path.expanduser("~/.config/ai-gateway-bench/key")).read_text().strip()
TOKEN = os.environ["BENCH_PROXY_TOKEN"]
LOG_DIR = pathlib.Path(os.environ.get("BENCH_LOG_DIR", "proxylog"))
GAP = float(os.environ.get("BENCH_GAP", "30"))
BROWSER = set(os.environ.get("BENCH_BROWSER_MODELS", "sable-0.5,sable-3.1,sable-3.5,sable-3.7,sable-4.0").split(","))
LOCAL_CONC = int(os.environ.get("BENCH_LOCAL_CONC", "2"))
STREAK_STOP = 3

(LOG_DIR / "bodies").mkdir(parents=True, exist_ok=True)
browser_lock = asyncio.Lock()
local_sem = asyncio.Semaphore(LOCAL_CONC)
last_browser_end = 0.0
bad_streak: dict[str, int] = {}
stopped: dict[str, str] = {}
session: ClientSession | None = None

HOP = {"host", "content-length", "authorization", "x-api-key", "connection", "accept-encoding", "transfer-encoding"}
KEEP_REQ_HEADERS = ("user-agent", "anthropic-version", "anthropic-beta", "session_id", "x-claude-code-session-id",
                    "originator", "conversation_id", "x-stainless-package-version")


def log(rec: dict) -> None:
    with open(LOG_DIR / "requests.jsonl", "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def instruction_tag(body: dict) -> str:
    """Short hash of the first user message: lets the analysis group calls by trial."""
    def first_user(items):
        for m in items or []:
            if isinstance(m, dict) and m.get("role") == "user":
                c = m.get("content")
                if isinstance(c, list):
                    c = " ".join(str(b.get("text", "")) for b in c if isinstance(b, dict))
                return str(c)[:4000]
        return ""
    text = first_user(body.get("messages")) or first_user(body.get("input") if isinstance(body.get("input"), list) else [])
    return hashlib.sha1(text.encode()).hexdigest()[:10] if text else ""


def analyse(endpoint: str, raw: bytes, stream: bool) -> dict:
    """Protocol checks on a finished answer: tool-call JSON validity, stop reason, error events."""
    a = {"tool_calls": 0, "bad_tool_json": 0, "error_events": 0, "stop": None, "text_chars": 0, "events": 0}
    try:
        if not stream:
            obj = json.loads(raw)
            if endpoint.endswith("messages"):
                for b in obj.get("content") or []:
                    if b.get("type") == "tool_use":
                        a["tool_calls"] += 1
                        if "_raw" in (b.get("input") or {}):
                            a["bad_tool_json"] += 1
                    elif b.get("type") == "text":
                        a["text_chars"] += len(b.get("text") or "")
                a["stop"] = obj.get("stop_reason")
            elif endpoint.endswith("responses"):
                for it in obj.get("output") or []:
                    if it.get("type") == "function_call":
                        a["tool_calls"] += 1
                        try:
                            json.loads(it.get("arguments") or "")
                        except ValueError:
                            a["bad_tool_json"] += 1
                    elif it.get("type") == "custom_tool_call":
                        a["tool_calls"] += 1
                a["stop"] = obj.get("status")
            else:
                ch = (obj.get("choices") or [{}])[0]
                for tc in (ch.get("message") or {}).get("tool_calls") or []:
                    a["tool_calls"] += 1
                    try:
                        json.loads(tc["function"]["arguments"] or "")
                    except (ValueError, KeyError, TypeError):
                        a["bad_tool_json"] += 1
                a["text_chars"] = len((ch.get("message") or {}).get("content") or "")
                a["stop"] = ch.get("finish_reason")
            return a
        # SSE
        tool_args: dict = {}
        for block in raw.split(b"\n\n"):
            data = [l[5:].strip() for l in block.split(b"\n") if l.startswith(b"data:")]
            if not data or data[0] == b"[DONE]":
                continue
            try:
                ev = json.loads(data[0])
            except ValueError:
                a["error_events"] += 1
                continue
            a["events"] += 1
            t = ev.get("type")
            if endpoint.endswith("messages"):
                if t == "content_block_start" and ev["content_block"]["type"] == "tool_use":
                    tool_args[ev["index"]] = ""
                elif t == "content_block_delta" and ev["delta"].get("type") == "input_json_delta":
                    tool_args[ev["index"]] = tool_args.get(ev["index"], "") + ev["delta"].get("partial_json", "")
                elif t == "content_block_delta" and ev["delta"].get("type") == "text_delta":
                    a["text_chars"] += len(ev["delta"].get("text", ""))
                elif t == "message_delta":
                    a["stop"] = ev["delta"].get("stop_reason")
                elif t == "error":
                    a["error_events"] += 1
                    a["error"] = ev.get("error")
            elif endpoint.endswith("responses"):
                if t == "response.function_call_arguments.done":
                    tool_args[ev["item_id"]] = ev.get("arguments", "")
                elif t == "response.custom_tool_call_input.done":
                    a["tool_calls"] += 1
                elif t == "response.output_text.delta":
                    a["text_chars"] += len(ev.get("delta", ""))
                elif t in ("response.completed", "response.failed", "response.incomplete"):
                    a["stop"] = ev["response"].get("status")
                    if t == "response.failed":
                        a["error_events"] += 1
                        a["error"] = ev["response"].get("error")
            else:
                if isinstance(ev.get("error"), dict):
                    a["error_events"] += 1
                    a["error"] = ev["error"]
                for ch in ev.get("choices") or []:
                    d = ch.get("delta") or {}
                    a["text_chars"] += len(d.get("content") or "")
                    for tc in d.get("tool_calls") or []:
                        i = tc.get("index", 0)
                        tool_args[i] = tool_args.get(i, "") + ((tc.get("function") or {}).get("arguments") or "")
                    if ch.get("finish_reason"):
                        a["stop"] = ch["finish_reason"]
        for v in tool_args.values():
            a["tool_calls"] += 1
            try:
                json.loads(v or "{}")
            except ValueError:
                a["bad_tool_json"] += 1
    except Exception as exc:  # analysis must never break the proxy
        a["analyse_error"] = repr(exc)[:200]
    return a


SHIMS = set(filter(None, os.environ.get("BENCH_SHIMS", "").split(",")))


def shim_body(path: str, body: dict) -> list[str] | None:
    """Emulates gateway fixes that are not deployed yet (so the pilot can go past a known bug).
    Every applied shim is recorded in the log; results with shims are reported as such."""
    done = []
    if "system-role" in SHIMS and path.endswith("/messages") and isinstance(body.get("messages"), list):
        sys_msgs = [m for m in body["messages"] if isinstance(m, dict) and m.get("role") == "system"]
        if sys_msgs:
            def txt(c):
                return c if isinstance(c, str) else "\n".join(b.get("text", "") for b in c or [] if isinstance(b, dict))
            top = body.get("system")
            top_list = [{"type": "text", "text": top}] if isinstance(top, str) else list(top or [])
            body["system"] = top_list + [{"type": "text", "text": txt(m.get("content"))} for m in sys_msgs]
            body["messages"] = [m for m in body["messages"] if m not in sys_msgs]
            done.append("system-role")
    return done or None


def is_bad(status: int, raw: bytes) -> bool:
    low = raw[:20000].lower()
    return status == 429 or b"captcha" in low or b"server_busy" in low


async def handle(request: web.Request) -> web.StreamResponse:
    global last_browser_end
    auth = request.headers.get("authorization", "").removeprefix("Bearer ").strip() or request.headers.get("x-api-key", "")
    if auth != TOKEN:
        return web.json_response({"error": {"message": "bench proxy: bad token"}}, status=401)
    raw = await request.read()
    path = request.path
    if "ws426" in SHIMS and request.method == "GET" and path.endswith("/v1/responses"):
        log({"id": uuid.uuid4().hex[:12], "t": time.time(), "path": path, "model": "", "method": "GET",
             "status": 426, "shim": ["ws426"], "hdr": {k: request.headers[k] for k in KEEP_REQ_HEADERS if k in request.headers}})
        return web.json_response({"error": {"message": "HTTP only", "code": "websocket_not_supported"}}, status=426)
    endpoint = path.split("/v1/", 1)[-1]
    body = {}
    if raw:
        try:
            body = json.loads(raw)
        except ValueError:
            pass
    model = str(body.get("model") or "")
    rid = uuid.uuid4().hex[:12]
    shim = shim_body(path, body) if body else None
    if shim:
        raw = json.dumps(body, ensure_ascii=False).encode()
    rec = {"id": rid, "t": time.time(), "path": path, "model": model, "stream": bool(body.get("stream")),
           "tag": instruction_tag(body) if body else "", "req_bytes": len(raw),
           "n_msgs": len(body.get("messages") or body.get("input") or []) if isinstance(body.get("messages") or body.get("input"), list) else None,
           "n_tools": len(body.get("tools") or []),
           "hdr": {k: request.headers[k] for k in KEEP_REQ_HEADERS if k in request.headers},
           "method": request.method, "upgrade": request.headers.get("upgrade"), "shim": shim}
    if model in stopped:
        rec.update(status=503, local="stopped", reason=stopped[model])
        log(rec)
        return web.json_response({"error": {"message": f"bench proxy: {model} stopped ({stopped[model]})",
                                            "type": "overloaded_error"}}, status=503)
    paced = model in BROWSER and request.method == "POST" and not path.endswith("count_tokens")
    guard = browser_lock if paced else (local_sem if request.method == "POST" and not path.endswith("count_tokens") else None)
    if guard:
        await guard.acquire()
    try:
        if paced:
            wait = last_browser_end + GAP - time.time()
            rec["paced_wait"] = round(max(0.0, wait), 1)
            if wait > 0:
                await asyncio.sleep(wait)
        (LOG_DIR / "bodies" / f"{rid}.req.json").write_bytes(raw)
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
        headers["authorization"] = f"Bearer {KEY}"
        t0 = time.time()
        rec["t_send"] = t0
        resp = web.StreamResponse()
        chunks: list[bytes] = []
        status, first = 0, None
        try:
            async with session.request(request.method, UPSTREAM + path, data=raw or None, headers=headers,
                                       params=request.query) as up:
                resp.set_status(up.status)
                for k, v in up.headers.items():
                    if k.lower() not in ("content-length", "transfer-encoding", "content-encoding", "connection"):
                        resp.headers[k] = v
                await resp.prepare(request)
                async for chunk in up.content.iter_any():
                    if first is None:
                        first = time.time()
                    chunks.append(chunk)
                    try:
                        await resp.write(chunk)
                    except (ConnectionResetError, RuntimeError):
                        rec["client_gone"] = True
                        break
                status = up.status
        except asyncio.CancelledError:
            rec["client_gone"] = True
            raise
        except Exception as exc:
            rec["upstream_exc"] = repr(exc)[:300]
            status = 599
            if not resp.prepared:
                resp = web.json_response({"error": {"message": f"bench proxy upstream error: {exc!r}"[:300]}}, status=502)
        finally:
            full = b"".join(chunks)
            (LOG_DIR / "bodies" / f"{rid}.resp.txt").write_bytes(full)
            rec.update(status=status if "status" not in rec else rec["status"], dur=round(time.time() - t0, 2),
                       ttfb=round((first or time.time()) - t0, 2) if chunks else None, resp_bytes=len(full),
                       x_route=resp.headers.get("x-sf-routed-model"), x_fallback=resp.headers.get("x-sf-fallback-model"))
            rec["status"] = status
            if request.method == "POST" and full:
                rec.update(analyse(endpoint, full, rec["stream"] and full.lstrip()[:1] != b"{"))
            if model in BROWSER:
                if is_bad(status, full):
                    bad_streak[model] = bad_streak.get(model, 0) + 1
                    rec["bad"] = True
                    if bad_streak[model] >= STREAK_STOP:
                        stopped[model] = f"{bad_streak[model]} captcha/429/busy in a row"
                        print(f"STOPPED {model}: {stopped[model]}", file=sys.stderr, flush=True)
                else:
                    bad_streak[model] = 0
            log(rec)
        await resp.write_eof()
        return resp
    finally:
        if paced:
            last_browser_end = time.time()
        if guard:
            guard.release()


async def main() -> None:
    global session
    session = ClientSession(timeout=ClientTimeout(total=None, sock_read=900), auto_decompress=True)
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.router.add_route("*", "/{tail:.*}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    host = os.environ.get("BENCH_BIND", "127.0.0.1")
    port = int(os.environ.get("BENCH_PORT", "8788"))
    await web.TCPSite(runner, host, port).start()
    print(f"bench proxy on {host}:{port} -> upstream, gap={GAP}s browser={sorted(BROWSER)}", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
