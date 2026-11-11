"""Google Flow (flow.google.com): video generation, not chat.

Measured 2026-11-12 with a free account in the browser:
  * the Gemini app's own "Создание видео" tool is locked on the free tier (upgrade
    prompt), but Flow gives every account a daily allowance of 50 "bonus" credits,
    refreshed once a day; Veo 3.1 Lite costs 10 per 8 s 720p clip (about 5 a day);
  * a project page has an agent chat on the right (`[contenteditable]` labelled
    "Что вы хотите создать?" / "What do you want to create?"). The agent turns the
    prompt into a generation and, unless the project setting "Подтверждать перед
    созданием" is "Никогда", asks "Сгенерировать 1 видео за бонусы (10)?" with an
    "Одобрить" button. No credit is spent and nothing is generated before approval;
  * results are tiles (`flow-grid-tile-container`, newest first) that show a
    percentage while rendering, ~45-60 s for Veo 3.1 Lite. Opening a tile goes to
    `/project/<id>/edit/<uuid>`; its scene editor (a canvas, no <video>) loads the mp4
    from a signed `https://flow-content.google/video/<uuid>?Expires=...` URL valid for
    ~6 h, which can be fetched inside the page (720p, ~5 MB);
  * the composer "+" opens a media picker with "Загрузить" (upload) and
    "Добавить в запрос" (use in the prompt) — how an image becomes the first frame.
The site uses reCAPTCHA; requests stay sequential per tab and we never solve
challenges.

Flow is NEVER given chat requests: `caps.chat` is False and the balancer filters it
out (see `ClusterBalancer.pick_candidates`).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import shutil
import tempfile
import time
from typing import Optional

import websockets

from .. import cdp
from .. import settings as cfg
from .base_adapter import BaseAIAdapter, Capabilities

log = logging.getLogger("cluster.flow")

COMPOSER = ('[contenteditable="true"][aria-label*="создать"], [contenteditable="true"][aria-label*="create"], '
            '[contenteditable="true"]')
APPROVE = ["Одобрить", "Approve"]
NO_CREDITS = ["недостаточно бонус", "not enough credit", "out of credits", "сменить тарифный план", "upgrade your plan"]
UPLOAD = ["Загрузить", "Upload"]
ADD_TO_PROMPT = ["Добавить в запрос", "Add to prompt"]
NEW_PROJECT = ["Новый проект", "New project"]
MAX_VIDEO_BYTES = 40 * 1024 * 1024


class FlowError(RuntimeError):
    """A generation that did not produce a clip; `code` is safe to show to clients."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class FlowAdapter(BaseAIAdapter):
    caps = Capabilities(chat=False, video_gen=True)

    def __init__(self):
        super().__init__("flow")

    # --- page helpers ------------------------------------------------------------------------------
    async def _js(self, ws, expr: str, timeout: float = 15.0, await_promise: bool = False):
        return await cdp.evaluate(ws, expr, timeout=timeout, await_promise=await_promise)

    async def _project_url(self, ws) -> Optional[str]:
        return await self._js(ws, """(() => {
            const m = location.pathname.match(/^\\/project\\/[0-9a-f-]{20,}/);
            return m ? location.origin + m[0] : null; })()""")

    async def ensure_project(self, ws, timeout: float = 25.0) -> str:
        """Lands on a project's main page (not a tile editor), creating one when on the home page."""
        url = await self._project_url(ws)
        here = await self._js(ws, "location.href")
        if url and here.rstrip("/") != url:
            await self.goto(ws, url)
        elif not url:
            if not (here or "").startswith("https://flow.google.com"):
                await self.goto(ws, "https://flow.google.com/")
            await self.wait_until(ws, "document.readyState === 'complete'", 15)
            if not await cdp.click_text(ws, NEW_PROJECT, timeout=10):
                raise FlowError("flow_unavailable", "Flow home page has no new-project button")
            await self.wait_until(ws, "/\\/project\\//.test(location.pathname)", 20)
            url = await self._project_url(ws)
        if not url:
            raise FlowError("flow_unavailable", "Flow did not open a project")
        if not await self.wait_until(ws, f"!!document.querySelector({json.dumps(COMPOSER)})", timeout):
            raise FlowError("flow_unavailable", "Flow composer did not appear")
        return url

    async def check_session(self, ws, timeout: float = 5.0, recover: bool = True) -> tuple[bool, str]:
        try:
            val = await self._js(ws, """(() => ({
                host: location.host, path: location.pathname,
                composer: !!document.querySelector('[contenteditable="true"]'),
                signin: /accounts\\.google\\.com/.test(location.host)
            }))()""", timeout=timeout)
        except Exception as e:
            return False, f"probe_failed:{type(e).__name__}"
        if not isinstance(val, dict):
            return False, "probe_bad_result"
        if val.get("signin"):
            return False, "login"
        if val.get("host") != "flow.google.com":
            return False, "wrong_page"
        if await self.detect_challenge(ws):
            return False, "captcha"
        # The home page and a tile editor have no composer; both are fine between jobs.
        return True, "ok"

    async def wait_for_input(self, ws, timeout: float = 10.0, *args, **kwargs) -> bool:
        return await self.wait_until(ws, "document.readyState === 'complete'", timeout)

    async def reset_chat(self, ws, timeout: float = 10.0) -> None:
        # There is no chat to clear; keep the tab on its project.
        try:
            await self.ensure_project(ws, timeout=timeout)
        except FlowError:
            pass

    async def is_fresh_chat(self, ws) -> bool:
        return True

    # Chat is never routed here (caps.chat is False); these exist to satisfy the interface.
    async def send_prompt(self, ws, prompt_text: str) -> bool:
        raise FlowError("unsupported", "Flow tabs do not take chat requests")

    async def read_response(self, ws, timeout: int = 45):
        return None

    # --- tiles ------------------------------------------------------------------------------------
    TILES_JS = """(() => Array.from(document.querySelectorAll('flow-grid-tile-container')).map(t => {
        const txt = (t.innerText || '');
        const pct = txt.match(/(\\d{1,3})\\s*%/);
        return { label: t.getAttribute('aria-label') || '', pct: pct ? +pct[1] : null,
                 thumb: !!t.querySelector('img[src*="/asb/"]'),
                 failed: /не удалось|failed|ошибка|error/i.test(txt) };
    }))()"""

    async def tiles(self, ws) -> list[dict]:
        return await self._js(ws, self.TILES_JS) or []

    # --- image as the first frame ---------------------------------------------------------------
    async def _wait_event(self, ws, method: str, timeout: float) -> Optional[dict]:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, end - time.monotonic()))
            except (asyncio.TimeoutError, websockets.exceptions.WebSocketException):
                return None
            msg = json.loads(raw)
            if msg.get("method") == method:
                return msg.get("params") or {}
        return None

    async def attach_image(self, ws, path: str, timeout: float = 45.0) -> None:
        """Uploads `path` through the composer's media picker and adds it to the prompt."""
        # The composer's "+" is an icon button whose text is the ligature "add". The page header
        # has another one, so look for it in the composer's own container, walking up from it.
        opened = await self._js(ws, """(() => {
            let box = document.querySelector('[contenteditable="true"]');
            for (let i = 0; box && i < 8; i++, box = box.parentElement) {
                const b = Array.from(box.querySelectorAll('button')).find(x => (x.innerText || '').trim() === 'add');
                if (b) { b.click(); return true; }
            }
            return false; })()""")
        if not opened:
            raise FlowError("image_upload_failed", "Flow attach button not found")
        if not await self.wait_until(ws, "document.body.innerText.includes('Загрузить') || document.body.innerText.includes('Upload')", 8):
            raise FlowError("image_upload_failed", "Flow media picker did not open")
        await cdp.call(ws, "Page.enable")
        try:
            await cdp.call(ws, "Page.setInterceptFileChooserDialog", {"enabled": True})
            await cdp.click_text(ws, UPLOAD, timeout=5)
            ev = await self._wait_event(ws, "Page.fileChooserOpened", 8)
            if not ev or not ev.get("backendNodeId"):
                raise FlowError("image_upload_failed", "Flow did not open a file chooser")
            await cdp.call(ws, "DOM.setFileInputFiles", {"files": [path], "backendNodeId": ev["backendNodeId"]})
        finally:
            try:
                await cdp.call(ws, "Page.setInterceptFileChooserDialog", {"enabled": False})
                await cdp.call(ws, "Page.disable")
            except Exception:
                pass
        # The upload shows up in the picker; "Добавить в запрос" becomes active once it is selected
        name = os.path.basename(path)
        await self.wait_until(ws, f"document.body.innerText.includes({json.dumps(name[:20])}) || "
                                  "!!document.querySelector('img[src^=\"blob:\"]')", timeout)
        if not await cdp.click_text(ws, ADD_TO_PROMPT, timeout=10):
            raise FlowError("image_upload_failed", "Flow did not accept the image")
        await asyncio.sleep(1.0)

    # --- generation ----------------------------------------------------------------------------
    async def _send(self, ws, text: str) -> None:
        ok = await self._js(ws, f"""(() => {{ const c = document.querySelector({json.dumps(COMPOSER)});
            if (!c) return false; c.focus(); return true; }})()""")
        if not ok:
            raise FlowError("flow_unavailable", "Flow composer not found")
        await cdp.insert_text(ws, text)
        await asyncio.sleep(0.3)
        sent = await self._js(ws, """(() => {
            const b = Array.from(document.querySelectorAll('button')).reverse()
              .find(x => /arrow_forward/.test(x.innerText || '') && !x.disabled);
            if (b) { b.click(); return true; } return false; })()""")
        if not sent:
            await cdp.key(ws)

    async def generate_video(self, ws, prompt: str, *, image_path: Optional[str] = None,
                             model_label: str = "Veo 3.1 Lite", aspect: str = "16:9",
                             timeout: float = 300.0) -> tuple[bytes, dict]:
        """One clip for `prompt` (optionally starting from `image_path`). Returns (mp4 bytes, meta)."""
        t0 = time.monotonic()
        await self.ensure_project(ws)
        before = await self.tiles(ws)
        if image_path:
            await self.attach_image(ws, image_path)
        # Explicit and short: the agent otherwise brainstorms variations (slower, more credits)
        await self._send(ws, f"Generate exactly 1 video with {model_label}, aspect ratio {aspect}. "
                             f"Do not ask questions and do not make variations. Video description: {prompt}")
        # Approval (skipped when the project is set to never confirm) or a new rendering tile
        approved = False
        end = time.monotonic() + 60
        while time.monotonic() < end:
            body = (await self._js(ws, "document.body.innerText.toLowerCase()") or "")
            if any(n in body for n in NO_CREDITS):
                raise FlowError("quota_exceeded", "Flow daily credits are used up on this account")
            now = await self.tiles(ws)
            if len(now) > len(before):
                break
            if not approved and await cdp.click_text(ws, APPROVE, timeout=3):
                approved = True
            await asyncio.sleep(1.0)
        else:
            raise FlowError("generation_not_started", "Flow did not start a generation")
        # Wait for the newest tile to finish rendering
        while time.monotonic() - t0 < timeout:
            now = await self.tiles(ws)
            top = now[0] if now else {}
            if top.get("failed"):
                raise FlowError("generation_failed", "Flow reported a failed generation")
            if top.get("thumb") and top.get("pct") is None:
                break
            await asyncio.sleep(2.0)
        else:
            raise FlowError("timeout", "The clip was not ready in time")
        return await self._download_newest(ws), {"label": top.get("label"), "seconds": 8,
                                                 "elapsed": round(time.monotonic() - t0, 1)}

    async def _download_newest(self, ws) -> bytes:
        """The newest tile's mp4.

        Fast path: the first time a clip is opened, the editor fetches it from a signed
        `flow-content.google/video/...` URL that shows up in the page's resource timings,
        so it can be re-read inside the page. Later opens are served from the player's own
        cache (a worker), which the page cannot see; then the clip is saved the way a user
        would: download menu -> "720p" with Chrome's download folder pointed at a temporary
        directory of ours (same container as Chrome).
        """
        await self._js(ws, "performance.clearResourceTimings(); 1")
        opened = await self._js(ws, """(() => { const t = document.querySelector('flow-grid-tile-container');
            const target = t && (t.querySelector('img') || t); if (!target) return false;
            target.click(); return true; })()""")
        if not opened or not await self.wait_until(ws, "/\\/edit\\//.test(location.pathname)", 15):
            raise FlowError("download_failed", "No result tile to open")
        data = None
        if await self.wait_until(ws, "performance.getEntriesByType('resource')"
                                     ".some(e => e.name.includes('flow-content.google/video/'))", 15, interval=0.5):
            url = await self._js(ws, "performance.getEntriesByType('resource').map(e => e.name)"
                                     ".filter(n => n.includes('flow-content.google/video/')).pop()")
            got = await cdp.fetch_as_base64(ws, url, timeout=90, max_bytes=MAX_VIDEO_BYTES)
            if got:
                data = base64.b64decode(got[1])
        if data is None:
            data = await self._save_via_menu(ws)
        try:
            await self.ensure_project(ws)        # leave the editor for the next job
        except FlowError:
            pass
        if data[4:8] != b"ftyp":
            raise FlowError("download_failed", "Unexpected clip format")
        return data

    async def _save_via_menu(self, ws, timeout: float = 90.0) -> bytes:
        folder = tempfile.mkdtemp(prefix="flow-dl-", dir=cfg.ATTACHMENT_DIR or None)
        try:
            await cdp.call(ws, "Page.setDownloadBehavior", {"behavior": "allow", "downloadPath": folder})
            clicked = await self._js(ws, """(() => {
                const b = Array.from(document.querySelectorAll('button'))
                  .find(x => (x.innerText || '').trim() === 'download' && x.getBoundingClientRect().top < 120);
                if (b) { b.click(); return true; } return false; })()""")
            if not clicked or not await cdp.click_text(ws, ["720p"], timeout=10):
                raise FlowError("download_failed", "Download menu not found")
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                done = [f for f in os.listdir(folder) if not f.endswith(".crdownload")]
                if done:
                    path = os.path.join(folder, done[0])
                    with open(path, "rb") as f:
                        return f.read()
                await asyncio.sleep(0.5)
            raise FlowError("download_failed", "The download did not finish")
        finally:
            try:
                await cdp.call(ws, "Page.setDownloadBehavior", {"behavior": "default"})
            except Exception:
                pass
            shutil.rmtree(folder, ignore_errors=True)
