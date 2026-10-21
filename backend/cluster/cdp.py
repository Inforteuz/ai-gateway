"""Helpers for working with CDP (Chrome DevTools Protocol).

PROBLEM: the CDP WebSocket channel carries two kinds of messages:
  1) replies to our requests (`{"id": N, "result": {...}}`)
  2) browser events (`{"method": "Page.frameNavigated", "params": {...}}`)

Reading `ws.recv()` directly lets an event slip in place of the reply, and the
code parses the wrong structure (that is where errors like `'bool' object has
no attribute 'get'` come from). This module gives every request a unique ID,
waits for the reply with exactly that ID, and discards events.
"""
from __future__ import annotations

import asyncio
import itertools
import json
from typing import Any, Optional

_counter = itertools.count(1)

# Maximum number of events discarded while waiting for one reply.
# The limit is LARGE: with `DOM.enable` on, or during a file upload, the page
# sends thousands of mutation events and a small limit leaves our reply unread
# (measured: on ChatGPT a "too many events" error after attaching a file killed
# the request). The real limit is `timeout`; this number only guards against
# an endless loop.
_MAX_SKIPPED_EVENTS = 20000


async def call(ws, method: str, params: Optional[dict] = None, timeout: float = 20.0) -> dict:
    """Call a CDP method and return the reply to EXACTLY this call.

    Returns the reply's `result` part (may be an empty dict).
    Raises TimeoutError, or RuntimeError if CDP returns an error.
    """
    mid = next(_counter)
    await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))

    deadline = asyncio.get_event_loop().time() + timeout
    for _ in range(_MAX_SKIPPED_EVENTS):
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError(f"CDP {method}: no reply")
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(msg, dict):
            continue
        if msg.get("id") != mid:
            continue  # an event or another call's reply: discard
        if "error" in msg:
            raise RuntimeError(f"CDP {method}: {msg['error']}")
        result = msg.get("result")
        return result if isinstance(result, dict) else {}
    raise RuntimeError(f"CDP {method}: too many events, reply not found")


async def evaluate(ws, expression: str, timeout: float = 20.0,
                   await_promise: bool = False) -> Any:
    """Run JS in the page and return the result value.

    Unwraps the inner `result.result.value` of the `Runtime.evaluate` reply.
    Returns None when there is no value.

    `await_promise=True`: if the expression returns a Promise (async function,
    `fetch`), wait for it to settle. Without it the Promise object is serialized
    into an empty result, which causes a silent failure.
    """
    res = await call(ws, "Runtime.evaluate",
                     {"expression": expression, "returnByValue": True,
                      "awaitPromise": bool(await_promise)},
                     timeout=timeout)
    # Do not let a JS exception in the page go silent; otherwise the result is
    # just `None` and the cause stays unknown (bad selector, syntax error, etc.).
    exc = res.get("exceptionDetails")
    if exc:
        text = (exc.get("exception") or {}).get("description") or exc.get("text") or "JS error"
        raise RuntimeError(f"Runtime.evaluate: {str(text).splitlines()[0][:200]}")
    inner = res.get("result")
    if not isinstance(inner, dict):
        return None
    return inner.get("value")


async def key(ws, key_name: str = "Enter", code: str = "Enter", vk: int = 13) -> None:
    """Press a keyboard key (down + up)."""
    for ev_type in ("rawKeyDown", "keyUp"):
        await call(ws, "Input.dispatchKeyEvent", {
            "type": ev_type, "key": key_name, "code": code,
            "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk,
        })


async def insert_text(ws, text: str) -> None:
    """Insert text into the focused field."""
    await call(ws, "Input.insertText", {"text": text})

async def click_at(ws, x: float, y: float) -> None:
    """Send a REAL mouse click at the given point.

    `element.click()` does not work in some SPAs (Vue/React): if the listener is
    on another element or the framework expects real pointer events, a DOM click
    is ignored. A CDP mouse event covers those cases because it is dispatched at
    the browser level.
    """
    base = {"x": x, "y": y, "button": "left", "clickCount": 1}
    await call(ws, "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
    await call(ws, "Input.dispatchMouseEvent", {**base, "type": "mousePressed"})
    await call(ws, "Input.dispatchMouseEvent", {**base, "type": "mouseReleased"})


async def click_text(ws, texts, timeout: float = 10.0) -> bool:
    """Click the innermost element whose text EQUALS one of `texts`.

    Picking the innermost element matters: sidebar buttons are usually several
    nested divs and the outer container has no click handler. A real mouse
    event is sent to the center of the element found.
    """
    import json as _json
    finder = """(() => {
        const want = %s;
        const all = Array.from(document.querySelectorAll('div, a, button, span, li'));
        const hits = all.filter(e => want.includes((e.innerText || '').trim()));
        if (!hits.length) return null;
        // Pick the innermost element (one that contains no other match)
        const deepest = hits.filter(e => !hits.some(o => o !== e && e.contains(o)));
        const el = deepest[0] || hits[hits.length - 1];
        const r = el.getBoundingClientRect();
        if (r.width < 2 || r.height < 2) return null;
        return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
    })()""" % _json.dumps(list(texts))
    pos = await call(ws, "Runtime.evaluate",
                     {"expression": finder, "returnByValue": True}, timeout=timeout)
    inner = pos.get("result")
    val = inner.get("value") if isinstance(inner, dict) else None
    if not isinstance(val, dict):
        return False
    await click_at(ws, val["x"], val["y"])
    return True

# --- File upload --------------------------------------------------------------

async def set_file_input(ws, selector: str, paths: list[str], timeout: float = 20.0) -> bool:
    """Attach files to an `<input type="file">`.

    Bypasses the browser's file picker: `DOM.setFileInputFiles` binds the file
    straight to the input and the site receives a `change` event, as if the
    user had picked it. The files MUST be on the machine running Chrome (here
    the cluster and Chrome share one host).

    Returns True if attached, False if the input was not found.
    """
    # Enable the DOM domain ONLY for this job and disable it right away: while
    # enabled, the page emits an event on every mutation and subsequent
    # `Runtime.evaluate` replies get buried under the flood.
    await call(ws, "DOM.enable", timeout=timeout)
    try:
        doc = await call(ws, "DOM.getDocument", {"depth": 0}, timeout=timeout)
        root = (doc.get("root") or {}).get("nodeId")
        if not root:
            return False
        found = await call(ws, "DOM.querySelector",
                           {"nodeId": root, "selector": selector}, timeout=timeout)
        node_id = found.get("nodeId")
        if not node_id:
            return False
        await call(ws, "DOM.setFileInputFiles",
                   {"files": paths, "nodeId": node_id}, timeout=timeout)
        return True
    finally:
        try:
            await call(ws, "DOM.disable", timeout=5)
        except Exception:
            pass


async def file_input_selector(ws, extra: Optional[str] = None) -> Optional[str]:
    """Return a CSS selector for the first matching `input[type=file]` on the page.

    Sites usually hide the file input (`display:none`), so visibility is not
    checked. `extra` is a more specific selector from the adapter; it is tried
    first.
    """
    candidates = []
    if extra:
        candidates.append(extra)
    candidates += [
        'input[type="file"][accept*="image"]',
        'input[type="file"]',
    ]
    for sel in candidates:
        try:
            if await evaluate(ws, f"!!document.querySelector({sel!r})", timeout=10):
                return sel
        except Exception:
            continue
    return None

async def fetch_as_base64(ws, url: str, timeout: float = 60.0,
                          max_bytes: int = 12 * 1024 * 1024) -> Optional[tuple[str, str]]:
    """Fetch a URL in the page context and return (mime, base64).

    Why inside the page: provider image URLs are usually `blob:` URLs or links
    that need an auth cookie. They cannot be fetched from outside (from our
    server); they only work in that page's context. So the fetch runs in the
    browser and the result comes back as base64.
    """
    js = """(async () => {
        try {
            const r = await fetch(%s);
            if (!r.ok) return { error: 'status ' + r.status };
            const b = await r.blob();
            if (b.size > %d) return { error: 'too large: ' + b.size };
            const buf = new Uint8Array(await b.arrayBuffer());
            let s = '';
            const chunk = 0x8000;
            for (let i = 0; i < buf.length; i += chunk) {
                s += String.fromCharCode.apply(null, buf.subarray(i, i + chunk));
            }
            return { mime: b.type || 'image/png', b64: btoa(s) };
        } catch (e) {
            return { error: String(e).slice(0, 120) };
        }
    })()""" % (json.dumps(url), max_bytes)
    res = await call(ws, "Runtime.evaluate",
                     {"expression": js, "returnByValue": True, "awaitPromise": True},
                     timeout=timeout)
    inner = res.get("result")
    val = inner.get("value") if isinstance(inner, dict) else None
    if not isinstance(val, dict) or not val.get("b64"):
        return None
    return str(val.get("mime") or "image/png"), str(val["b64"])


# --- File injection (inside the page) -----------------------------------------
#
# Why this is needed: `DOM.setFileInputFiles` fails in two cases:
#   1) the site has no `change` listener on its hidden `input[type=file]`
#      (measured: on Qwen the file lands in the input and the site never notices);
#   2) the file is not visible in Chrome's mount namespace (when the cluster
#      service runs with `PrivateTmp=true`, its /tmp DOES NOT EXIST for Chrome).
# So the file bytes are sent to the page in base64 chunks, a `File` object is
# built there and delivered via a `paste` or `drop` event, which is exactly
# what a user would do.

_CHUNK = 512 * 1024  # base64 chunk that fits in one Runtime.evaluate


async def clear_file_payload(ws, timeout: float = 10.0) -> None:
    """Delete file bytes left in the page (to free memory)."""
    try:
        await evaluate(ws, "(() => { delete window.__gwFiles; delete window.__gwChunks; return 1; })()",
                       timeout=timeout)
    except Exception:
        pass


async def push_file(ws, path: str, mime: str, name: str, timeout: float = 30.0) -> bool:
    """Send a file to the page in base64 chunks and append it to `window.__gwFiles`."""
    import base64 as _b64
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return False
    b64 = _b64.b64encode(data).decode()

    await evaluate(ws, "(() => { window.__gwChunks = []; return 1; })()", timeout=timeout)
    for i in range(0, len(b64), _CHUNK):
        await evaluate(ws, f"(() => {{ window.__gwChunks.push({json.dumps(b64[i:i + _CHUNK])}); return 1; }})()",
                       timeout=timeout)
    ok = await evaluate(ws, """(() => {
        window.__gwFiles = window.__gwFiles || [];
        window.__gwFiles.push({ b64: (window.__gwChunks || []).join(''),
                                name: %s, mime: %s });
        window.__gwChunks = [];
        return window.__gwFiles.length;
    })()""" % (json.dumps(name), json.dumps(mime or "application/octet-stream")), timeout=timeout)
    return bool(ok)


# Shared JS snippet that builds a `DataTransfer` from the bytes sent to the page.
_MAKE_DT = """
    const mk = () => {
        const dt = new DataTransfer();
        for (const it of (window.__gwFiles || [])) {
            const bin = atob(it.b64);
            const arr = new Uint8Array(bin.length);
            for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
            dt.items.add(new File([arr], it.name, { type: it.mime }));
        }
        return dt;
    };
    const editor = () => Array.from(
        document.querySelectorAll('textarea, [contenteditable="true"]')
    ).find(e => e.offsetParent) || document.querySelector('textarea');
"""


async def inject_files(ws, mode: str, timeout: float = 60.0) -> bool:
    """Deliver the files in `window.__gwFiles` to the page.

    `mode`:
      - "paste": paste into the chat field (as if the user pressed Ctrl+V);
      - "drop":  drop onto the composer (drag & drop);
      - "input": set them on `input[type=file]` via `DataTransfer` and fire `change`.
    """
    bodies = {
        "paste": """
            const ed = editor();
            if (!ed) return false;
            ed.focus();
            ed.dispatchEvent(new ClipboardEvent('paste', {
                bubbles: true, cancelable: true, clipboardData: mk() }));
            return true;
        """,
        "drop": """
            const ed = editor();
            if (!ed) return false;
            let root = ed;
            for (let i = 0; i < 5 && root.parentElement; i++) root = root.parentElement;
            const dt = mk();
            for (const t of ['dragenter', 'dragover', 'drop']) {
                root.dispatchEvent(new DragEvent(t, {
                    bubbles: true, cancelable: true, dataTransfer: dt }));
            }
            return true;
        """,
        "input": """
            const inp = document.querySelector('input[type=file]');
            if (!inp) return false;
            inp.files = mk().files;
            inp.dispatchEvent(new Event('input', { bubbles: true }));
            inp.dispatchEvent(new Event('change', { bubbles: true }));
            return true;
        """,
    }
    body = bodies.get(mode)
    if not body:
        return False
    js = "(() => {%s\n try {%s\n } catch (e) { return false; }\n})()" % (_MAKE_DT, body)
    try:
        return bool(await evaluate(ws, js, timeout=timeout))
    except Exception:
        return False

async def hover_click(ws, locator_js: str, timeout: float = 15.0) -> bool:
    """Scroll an element into view, hover it, and click it with a REAL mouse.

    `locator_js` is a JS expression that finds the element, calls
    `scrollIntoView` on it and returns `{rowX, rowY, x, y}` (`null` if not found).

    Why so involved: on many sites (ChatGPT sidebar, Qwen chat list) the action
    button only appears when the row is HOVERED and does not respond to
    `element.click()`. Also, stale coordinates (the list has shifted) make the
    click land on empty space, so coordinates are re-read AFTER scrolling ends.
    """
    # Call 1: scroll. Call 2: fresh coordinates once scrolling is done.
    await evaluate(ws, locator_js, timeout=timeout)
    await asyncio.sleep(0.8)
    pos = await evaluate(ws, locator_js, timeout=timeout)
    if not isinstance(pos, dict) or pos.get("x") is None:
        return False
    # First over the row (that reveals the button), then over the button
    for key_x, key_y in (("rowX", "rowY"), ("x", "y")):
        x, y = pos.get(key_x), pos.get(key_y)
        if x is None or y is None:
            continue
        await call(ws, "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        await asyncio.sleep(0.4)
    await click_at(ws, pos["x"], pos["y"])
    return True


async def visible_texts(ws) -> set:
    """Set of short visible texts on the page, used to detect that a menu
    appeared (before/after diff)."""
    js = """(() => { const out = [];
        for (const e of document.querySelectorAll('div,button,span,a,li')) {
            if (e.offsetParent === null) continue;
            const r = e.getBoundingClientRect();
            if (r.width < 12 || r.height < 10) continue;
            const t = (e.innerText || '').trim();
            if (t && t.length < 30 && !out.includes(t)) out.push(t);
        } return out; })()"""
    try:
        out = await evaluate(ws, js, timeout=10)
    except Exception:
        return set()
    return set(out or [])
