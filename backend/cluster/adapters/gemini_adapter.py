import asyncio
import json
import re
from typing import Optional

from .. import cdp, logs
from .base_adapter import READ_TEXT_JS, BaseAIAdapter, Capabilities

# gemini.google.com (measured on the live page):
#   input    : rich-textarea div.ql-editor[contenteditable] (aria "Enter a prompt for Gemini")
#   send     : button[aria-label="Send message"] (.send-button)
#   stop     : the aria-label "Stop ..." button — present while the answer is being written
#   answer   : message-content .markdown inside the last <model-response>
#   new chat : navigate to /app; after the first answer the URL becomes /app/<id>
# Also works logged out (Gemini 3.5 Flash); "17*23" answered "391" in ~4 s.
# Logged in (account UI in Russian): the "Авто" (Gemini 3) model is available,
# "3.1 Pro" and "4 Argon" are locked. There is a "Временный чат" (temporary chat) button —
# a temporary chat does not appear in "recent chats" and is deleted after 72 hours; so every
# request runs in that mode and the owner's chats are never touched.

log = logs.get("gemini")

GEMINI_HOME = "https://gemini.google.com/app"
GEMINI_INPUT = 'rich-textarea div.ql-editor[contenteditable="true"], div.ql-editor[contenteditable="true"]'
GEMINI_SEND = 'button.send-button, button[aria-label="Send message"], button[aria-label*="Отправить" i]'
# The account UI may be in Russian — button labels are matched in both languages
_STOP_RE = "/stop|останов/i"
_TEMP_RE = "/temporary chat|временный чат/i"
# Centre of the "Временный чат" (temporary chat) button (null if not visible)
_TEMP_BUTTON_JS = f"""(() => {{
    const b = [...document.querySelectorAll('button')].filter(e => e.offsetParent)
        .find(b => {_TEMP_RE}.test(b.getAttribute('aria-label') || ''));
    if (!b) return null; const r = b.getBoundingClientRect();
    return {{ x: r.left + r.width / 2, y: r.top + r.height / 2 }};
}})()"""
# Logged-out marker: a visible "Войти" / "Sign in" button or link
_LOGGED_OUT_JS = """(() => [...document.querySelectorAll('a, button')].filter(e => e.offsetParent)
    .some(e => /^(войти|sign in)$/i.test((e.innerText || e.getAttribute('aria-label') || '').trim())))()"""
# Temporary mode marker. MEASURED: the `is-temporary-chat` class is added to `chat-window`
# within ~250 ms of the click (together with the banner) — the primary marker.
# Fallback: the explanatory text on the page (ru/en). NOTE: "Временные чаты" is a link: it is
# followed by a line break and the invisible text "Откроется в новом окне" (opens in a new window).
_TEMP_STATE_JS = """(() => {
    if (document.querySelector('chat-window.is-temporary-chat')) return true;
    const t = (document.body.innerText || '').replace(/\\s+/g, ' ');
    // The link carries invisible "Откроется в новом окне" text — allow a short gap in between
    return /Временные чаты.{0,60}не появляются|Temporary chats.{0,60}(won.t|don.t|aren.t|are not|do not)/i.test(t);
})()"""


class GeminiAdapter(BaseAIAdapter):
    # The input field truncates everything beyond 32 000 characters (MEASURED) — the pool
    # fits long requests to this limit with compose.fit_prompt.
    MAX_INPUT_CHARS = 31000
    FILE_INPUT = 'input[type="file"]'
    # MEASURED: the `input[type=file]` elements only exist in the DOM while the
    # "Загрузка и инструменты" (➕, upload and tools) menu is open, whereas a `paste` into
    # the composer uploads the file at once (`uploader-file-preview` + blob <img>). Hence paste first.
    UPLOAD_METHODS = ("paste", "drop", "input")
    # Remove-attachment button: aria-label "закрыть прикрепленный файл" (close attached file)
    ATTACHMENT_REMOVE = (BaseAIAdapter.ATTACHMENT_REMOVE
                         + ', button[aria-label*="закрыть прикрепленный" i], .gem-attachment-close-button button')

    # Capabilities measured live (production, temporary chat, staging adapter):
    #   vision    — image (paste) + question: ~2 s upload, correct answer;
    #   files     — txt/pdf (paste), a random code from the file came back verbatim;
    #   image_gen — draws EVEN in a temporary chat (unlike ChatGPT), ~12-13 s,
    #               1024 px PNG; editing (sent image + instruction) works the same way.
    # `delete_chats=True` — the owner allowed deleting all chats on this account
    # (see `delete_old_chats`).
    #   tool_calling — via the cluster's text tool protocol (cluster/tools.py); since it
    #               serves as the main model, agent CLIs (with tools) depend on
    #               it. Measured in a live test.
    caps = Capabilities(tool_calling=True, vision=True, files=True, image_gen=True, delete_chats=True)

    def __init__(self):
        super().__init__("gemini")

    # How long to wait for the "Временный чат" button on a cold load (seconds)
    TEMP_BUTTON_WAIT = 8.0

    async def reset_chat(self, ws, timeout: float = 12.0) -> None:
        """Every request runs in a new TEMPORARY chat.

        Navigating to /app opens a regular chat — temporary mode is re-enabled with the
        button every time (it does not survive navigation). Logged out there is no button:
        then a plain new chat is used (history is not saved anyway).
        """
        # SPEED: if the conversation has a "Новый чат" (new chat) button, open it within the SPA
        # (a full load took 3-4 s and grew renderer memory); otherwise navigate to /app.
        spa = await cdp.evaluate(ws, """(() => {
            const b = [...document.querySelectorAll('button, a')].filter(e => e.offsetParent)
                .find(e => /^(новый чат|new chat)$/i.test((e.getAttribute('aria-label') || '').trim()));
            if (!b) return false; b.click(); return true;
        })()""", timeout=5)
        if spa:
            await asyncio.sleep(0.8)
            if not await cdp.evaluate(ws, """(() => document.querySelectorAll('model-response').length === 0
                    && /^\\/app\\/?$/.test(location.pathname))()""", timeout=5):
                spa = False
        if not spa:
            await self.goto(ws, GEMINI_HOME)
        await self.wait_for_input(ws, timeout=timeout, selector=GEMINI_INPUT)
        if await cdp.evaluate(ws, _TEMP_STATE_JS, timeout=5):
            return
        # MEASURED: after the click the explanatory text appears with a delay —
        # previously the button was clicked a second time and the mode got switched OFF
        # again. Now it is clicked once (with a real mouse) and the state is awaited for 3 s.
        # MEASURED: on a cold load after a Chrome restart the input field is there, but the
        # "Временный чат" button appears a few seconds LATER. Previously a button not found
        # right away was treated as logged-out mode and the first request after a restart ran
        # in a regular chat and landed in history. Now the button is awaited.
        # MEASURED: in a narrow window a side panel (drawer) left open covers the page —
        # a real mouse click hits its backdrop instead of the button and the mode never
        # turns on. So the panel is closed first.
        if await cdp.evaluate(ws, self._DRAWER_OPEN_JS, timeout=5):
            await cdp.evaluate(ws, self._CLOSE_PANEL_JS, timeout=5)
            await asyncio.sleep(0.8)
        pos = None
        for _ in range(int(self.TEMP_BUTTON_WAIT / 0.5)):
            pos = await cdp.evaluate(ws, _TEMP_BUTTON_JS, timeout=5)
            if isinstance(pos, dict) or await cdp.evaluate(ws, _LOGGED_OUT_JS, timeout=5):
                break
            await asyncio.sleep(0.5)
        if not isinstance(pos, dict):
            if not await cdp.evaluate(ws, _LOGGED_OUT_JS, timeout=5):
                log.warning("gemini: 'Временный чат' button not found — the request goes to a regular chat")
            return   # logged out: no button, history is not saved anyway
        await cdp.click_at(ws, pos["x"], pos["y"])
        for _ in range(20):
            await asyncio.sleep(0.25)
            if await cdp.evaluate(ws, _TEMP_STATE_JS, timeout=5):
                # Enabling the mode re-renders the input field — wait for the new one,
                # otherwise the text would land in the old (hidden) field and never be sent.
                await asyncio.sleep(0.5)
                await self.wait_for_input(ws, timeout=timeout, selector=GEMINI_INPUT)
                return
        log.warning("gemini: temporary chat did not turn on — the request goes to a regular chat")

    # Reasoning level (logged in, the "Уровень рассуждений" menu; measured):
    # Низкая (default) / Средний / Высокая. In the English UI: Low / Medium / High.
    EFFORT_LABELS = {
        "none": ("Низкая", "Low"), "minimal": ("Низкая", "Low"), "low": ("Низкая", "Low"),
        "medium": ("Средний", "Medium"),
        "high": ("Высокая", "High"), "xhigh": ("Высокая", "High"),
    }
    DEFAULT_EFFORT = "low"

    async def set_effort(self, ws, effort: Optional[str]) -> Optional[str]:
        """Picks the reasoning level from the menu. The level persists on the account, so the
        last one set is remembered and the menu is not opened if unchanged (~2 s saved)."""
        effort = (effort or self.DEFAULT_EFFORT).strip().lower()
        labels = self.EFFORT_LABELS.get(effort)
        if not labels:
            return None
        if getattr(self, "_effort_set", None) == effort:
            return effort
        pos = await cdp.evaluate(ws, """(() => {
            const b = [...document.querySelectorAll('button')]
                .find(b => /Выбор режима|mode picker|choose.*mode|select.*mode/i.test(b.getAttribute('aria-label') || ''));
            if (!b) return null; const r = b.getBoundingClientRect();
            return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
        })()""", timeout=8)
        if not isinstance(pos, dict):
            return None   # the menu differs when logged out — no level is selected
        await cdp.click_at(ws, pos["x"], pos["y"])
        item = None
        expanded = False
        for _ in range(10):
            await asyncio.sleep(0.25)
            item = await cdp.evaluate(ws, """(() => {
                const want = %s;
                const it = [...document.querySelectorAll('.cdk-overlay-pane [role=menuitem]')]
                    .filter(e => e.offsetParent)
                    .find(e => want.some(w => (e.innerText || '').trim().startsWith(w)));
                if (!it) return null; const r = it.getBoundingClientRect();
                return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
            })()""" % json.dumps(list(labels)), timeout=8)
            if isinstance(item, dict):
                break
            if not expanded:
                # Expand the "Уровень рассуждений" (reasoning level) section if it is collapsed
                sub = await cdp.evaluate(ws, """(() => {
                    const e = [...document.querySelectorAll('.cdk-overlay-pane [role=menuitem]')].filter(e => e.offsetParent)
                        .find(e => /^(Уровень рассуждений|Thinking level|Reasoning)/i.test((e.innerText || '').trim())
                                   && (e.innerText || '').trim().length < 40);
                    if (!e) return null; const r = e.getBoundingClientRect();
                    return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
                })()""", timeout=8)
                if isinstance(sub, dict):
                    expanded = True
                    await cdp.click_at(ws, sub["x"], sub["y"])
        if isinstance(item, dict):
            await cdp.click_at(ws, item["x"], item["y"])
            await asyncio.sleep(0.4)
            self._effort_set = effort
        # Close the menu if it stayed open
        if await cdp.evaluate(ws, "!!document.querySelector('.cdk-overlay-pane [role=menuitem]')", timeout=5):
            await cdp.key(ws, "Escape", "Escape", 27)
        return effort if isinstance(item, dict) else None

    async def is_fresh_chat(self, ws) -> bool:
        ok = await cdp.evaluate(ws, """(() => {
            const p = location.pathname.replace(/\\/+$/, '');
            return (p === '/app' || p === '') && document.querySelectorAll('model-response').length === 0
                && !!document.querySelector('rich-textarea .ql-editor');
        })()""", timeout=8)
        if ok is not True:
            return False
        # A pre-warmed page must be in temporary mode. If the button has not appeared yet
        # (cold load) the page does NOT count as clean — the only exception is a definite logged-out mode.
        if await cdp.evaluate(ws, _TEMP_STATE_JS, timeout=5):
            return True
        return bool(await cdp.evaluate(ws, _LOGGED_OUT_JS, timeout=5))

    async def send_prompt(self, ws, prompt_text: str) -> bool:
        """Types the text into the Quill editor and sends it.

        MEASURED (agent CLI): on a large request such as a 12k-character system prompt +
        12 tools + a 200-line Glob result, `insertText` took 11-19 s while send confirmation
        waited only ~1 s — the text stayed in the field and the request ended as "not sent".
        Now: (1) the text is set in one go (Quill accepts it quickly), falling back to
        `insertText`; (2) we wait for the Send button to become enabled and click it with
        a real mouse; (3) we wait up to 10 s for the send to register, retrying with Enter
        if needed.
        """
        # 1) Focus the field and select its contents (so leftover text gets replaced)
        focus_js = f"""(() => {{
            const vis = e => e.offsetParent !== null;
            const el = Array.from(document.querySelectorAll({json.dumps(GEMINI_INPUT)})).filter(vis).pop();
            if (!el) return false;
            el.focus();
            const range = document.createRange();
            range.selectNodeContents(el);
            const sel = window.getSelection();
            sel.removeAllRanges(); sel.addRange(range);
            return true;
        }})()"""
        if not await cdp.evaluate(ws, focus_js, timeout=10):
            return False
        want = len("".join(prompt_text.split()))
        # 2) FASTEST: the editor's own Quill object (`.ql-container.__quill`) — `setText`
        #    sets 47k characters in 0.12 s (MEASURED; Input.insertText took 16-44 s).
        #    With the 'user' source the Angular state and the Send button get updated.
        how = "quill"
        quill_js = f"""(() => {{
            const ed = document.querySelector({json.dumps(GEMINI_INPUT)});
            const c = ed && ed.closest('.ql-container');
            const q = c && (c.__quill || (window.Quill && window.Quill.find && window.Quill.find(c)));
            if (!q || !q.setText) return -1;
            q.setText({json.dumps(prompt_text)}, 'user');
            try {{ q.setSelection(q.getLength(), 0, 'user'); }} catch (e) {{}}
            return (ed.innerText || '').replace(/\\s+/g, '').length;
        }})()"""
        try:
            qgot = int(await cdp.evaluate(ws, quill_js, timeout=30) or -1)
        except Exception:
            qgot = -1
        if qgot < want * 0.98:
            # 3) Fallback: the browser's own Input.insertText command (slow but reliable)
            how = "cdp"
            await cdp.evaluate(ws, focus_js, timeout=10)
            try:
                await cdp.call(ws, "Input.insertText", {"text": prompt_text}, timeout=60)
            except Exception:
                how = "insert"
        got_js = f"""(() => {{ const el = document.querySelector({json.dumps(GEMINI_INPUT)});
            return el ? (el.innerText || '').replace(/\\s+/g, '').length : 0; }})()"""
        got = 0
        for _ in range(10):
            got = int(await cdp.evaluate(ws, got_js, timeout=10) or 0)
            if got >= want * 0.98:
                break
            await asyncio.sleep(0.3)
        if got < want * 0.98:
            how = "insert"
            await cdp.evaluate(ws, focus_js, timeout=10)
            ok = await cdp.evaluate(ws, f"document.execCommand('insertText', false, {json.dumps(prompt_text)})", timeout=60)
            if not ok:
                return False

        sent_js = f"""(() => {{
            if (document.querySelectorAll('model-response').length > 0) return true;
            if ([...document.querySelectorAll('button')].some(b => {_STOP_RE}.test(b.getAttribute('aria-label') || ''))) return true;
            const el = document.querySelector({json.dumps(GEMINI_INPUT)});
            return el ? (el.innerText || '').trim().length === 0 : false;
        }})()"""
        btn_js = f"""(() => {{
            const b = document.querySelector({json.dumps(GEMINI_SEND)});
            if (!b || b.disabled || b.getAttribute('aria-disabled') === 'true') return null;
            const r = b.getBoundingClientRect();
            return {{ x: r.left + r.width / 2, y: r.top + r.height / 2 }};
        }})()"""

        async def confirm_sent(sec: float) -> bool:
            for _ in range(int(sec / 0.5)):
                await asyncio.sleep(0.5)
                try:
                    if await cdp.evaluate(ws, sent_js, timeout=8):
                        return True
                except Exception:
                    pass
            return False

        for attempt in range(2):
            pos = None
            for _ in range(20):                       # wait for the button to become enabled (large text)
                pos = await cdp.evaluate(ws, btn_js, timeout=8)
                if isinstance(pos, dict):
                    break
                await asyncio.sleep(0.5)
            if isinstance(pos, dict):
                await cdp.click_at(ws, pos["x"], pos["y"])
            else:
                await cdp.key(ws, "Enter", "Enter", 13)
            if await confirm_sent(10.0 if attempt == 0 else 6.0):
                return True
            log.warning("gemini: send not confirmed (%s, attempt %d)", how, attempt + 1)
        return False

    async def read_response(self, ws, timeout: int = 45) -> Optional[str]:
        script = """(() => {
            const READ = __READ__;
            const generating = [...document.querySelectorAll('button')]
                .some(b => __STOP__.test(b.getAttribute('aria-label') || ''));
            const rs = document.querySelectorAll('model-response');
            const last = rs[rs.length - 1];
            let answer = '';
            if (last) {
                const md = last.querySelector('message-content .markdown')
                        || last.querySelector('.markdown')
                        || last.querySelector('message-content');
                answer = md ? READ(md) : '';
            }
            // If a limit/error block shows up instead of an answer, return it as text —
            // the pool classifies it as a quota/provider error (no idle 45 s wait).
            if (!answer && !generating) {
                // Only the model answer and error/alert blocks — the user's question
                // (user-query) is excluded from the search: otherwise a word like "лимит"
                // in the question would fake a "quota exhausted".
                const tail = [...document.querySelectorAll('model-response, [role=alert], mat-snack-bar-container, .error-message')]
                    .map(e => e.innerText || '').join(' ').slice(-800);
                const HINTS = ["reached your limit", "limit reached", "try again later",
                               "something went wrong", "too many requests",
                               "достигли лимита", "попробуйте позже", "что-то пошло не так"];
                for (const h of HINTS) {
                    if (tail.toLowerCase().indexOf(h) >= 0) return { answer: h, generating: false };
                }
            }
            return { answer: answer, generating: generating };
        })()""".replace("__READ__", READ_TEXT_JS).replace("__STOP__", _STOP_RE)
        raw = await self.poll_answer(ws, script, timeout=timeout, initial_delay=1.0)
        return _fix_code_labels(raw) if raw else raw

    def partial_text(self, text: str) -> str:
        text = self.clean_text(text)
        return _fix_code_labels(text) if text else text

    # --- File attachment ------------------------------------------------------

    # On the account's first upload Google shows the "Создание контента на основе изображений и
    # файлов" agreement dialog ("Отмена" / "Принять"). The owner accepts it (it was accepted
    # via VNC) — the adapter NEVER ACCEPTS the agreement ITSELF: it closes the dialog and
    # reports the upload as failed.
    _CONSENT_JS = """(() => {
        const d = [...document.querySelectorAll('mat-dialog-container')].find(e => e.offsetParent
            && /на основе изображений и файлов|based on images and files/i.test(e.innerText || ''));
        if (!d) return false;
        const b = [...d.querySelectorAll('button')].find(b => /^(Отмена|Cancel)$/i.test((b.innerText || '').trim()));
        if (b) b.click();
        return true;
    })()"""

    # MEASURED: the base `upload_state` also counted HIDDEN `mat-progress-spinner` elements
    # on the page as "uploading" — every attachment waited out the 40 s timeout. Here the
    # signal is the `uploader-file-preview` chips in the composer (images and documents alike),
    # and busy means only a visible spinner (it shows for ~1 s after a paste).
    _UPLOAD_STATE_JS = """(() => {
        const area = document.querySelector('input-area-v2') || document.body;
        const chips = area.querySelectorAll('uploader-file-preview').length;
        const busy = [...area.querySelectorAll('mat-spinner, mat-progress-spinner, mat-progress-bar, [role="progressbar"]')]
            .some(e => e.offsetParent !== null);
        return { imgs: chips, named: 0, busy };
    })()"""

    async def upload_state(self, ws, names) -> dict:
        try:
            st = await cdp.evaluate(ws, self._UPLOAD_STATE_JS, timeout=15)
        except Exception:
            st = None
        if not isinstance(st, dict):
            return {"imgs": 0, "named": 0, "busy": False}
        return {"imgs": int(st.get("imgs") or 0), "named": 0, "busy": bool(st.get("busy"))}

    _ATTACH_DIAG_JS = """(() => ({
        path: location.pathname,
        editors: [...document.querySelectorAll('rich-textarea .ql-editor')].filter(e => e.offsetParent).length,
        chips: document.querySelectorAll('uploader-file-preview').length,
        overlays: [...document.querySelectorAll('mat-dialog-container, .cdk-overlay-pane, mat-snack-bar-container')]
            .filter(e => e.offsetParent).map(e => (e.innerText || '').trim().slice(0, 80)),
    }))()"""

    async def _consent_blocked(self, ws) -> bool:
        try:
            return bool(await cdp.evaluate(ws, self._CONSENT_JS, timeout=5))
        except Exception:
            return False

    async def attach_files(self, ws, paths, images_only: bool = False, timeout: float = 40.0) -> bool:
        """The base mechanism + two Gemini-specific cases.

        * Agreement dialog — never accepted (see above), the request is aborted.
        * MEASURED: once both paste and drop failed, while the next 4 attempts
          (new chat + paste) each worked in ~2.4 s. Cause unknown — the state is
          logged, the page is reloaded from /app and it retries ONCE.
        """
        for attempt in (1, 2):
            ok = await super().attach_files(ws, paths, images_only=images_only, timeout=timeout)
            if await self._consent_blocked(ws):
                log.warning("gemini: upload agreement not accepted — the owner must click "
                            "'Принять' via VNC; attachment cancelled")
                await self.clear_attachments(ws)
                return False
            if ok or attempt == 2:
                return ok
            try:
                state = await cdp.evaluate(ws, self._ATTACH_DIAG_JS, timeout=5)
            except Exception as e:
                state = repr(e)
            log.warning("gemini: attachment failed (%s) — reloading the page and retrying", state)
            await self.clear_attachments(ws)
            await self.goto(ws, GEMINI_HOME)
            await asyncio.sleep(1.0)
            await self.reset_chat(ws)
        return False

    async def clear_attachments(self, ws, timeout: float = 8.0) -> int:
        """Removes chips ONE BY ONE. The base loop clicks 8 times inside one JS call —
        Gemini removes a chip asynchronously, so the same button got clicked 8 times and
        the remaining chips stayed (measured: "8 removed" for a single chip)."""
        js = """(() => {
            const chip = document.querySelector('input-area-v2 uploader-file-preview');
            const b = chip && chip.querySelector(%s);
            if (!b) return false; b.click(); return true;
        })()""" % json.dumps(self.ATTACHMENT_REMOVE)
        n = 0
        for _ in range(8):
            try:
                if not await cdp.evaluate(ws, js, timeout=timeout):
                    break
            except Exception:
                break
            n += 1
            await asyncio.sleep(0.4)
        if n:
            log.info("gemini: removed %d attachment(s) from the composer", n)
        return n

    # --- Image generation result ---------------------------------------------

    # Images are taken ONLY from the last <model-response>: an image the user sent
    # (for editing) lives in <user-query> and must not be part of the result.
    _IMAGES_JS = """(() => {
        const rs = document.querySelectorAll('model-response');
        const last = rs[rs.length - 1];
        if (!last) return [];
        return [...last.querySelectorAll('img')].filter(i => {
            if (i.closest('user-query')) return false;
            if (!i.complete || i.naturalWidth < 200 || i.naturalHeight < 200) return false;
            const s = i.currentSrc || i.src || '';
            return s.startsWith('http') || s.startsWith('blob:') || s.startsWith('data:image');
        }).map(i => i.currentSrc || i.src).slice(-4);
    })()"""

    async def read_images(self, ws, timeout: float = 15.0) -> list[str]:
        try:
            out = await cdp.evaluate(ws, self._IMAGES_JS, timeout=timeout)
        except Exception:
            return []
        return [u for u in (out or []) if isinstance(u, str)]

    _GENERATING_JS = ("[...document.querySelectorAll('button')].some(b => __STOP__.test(b.getAttribute('aria-label') || ''))"
                      .replace("__STOP__", _STOP_RE))

    async def generation_done(self, ws) -> bool:
        try:
            return not await cdp.evaluate(ws, self._GENERATING_JS, timeout=5)
        except Exception:
            return False

    async def finished_without_image(self, ws) -> bool:
        """The answer is finished but there is no image (the model refused or answered in text).

        MEASURED: the stop button disappears BEFORE the image finishes loading, and the
        answer contains a hidden "Ответ Gemini" heading — so a present `generated-image`
        counts as unfinished and the text is taken from .markdown only.
        """
        try:
            return bool(await cdp.evaluate(ws, """(() => {
                const generating = %s;
                const rs = document.querySelectorAll('model-response');
                const last = rs[rs.length - 1];
                if (generating || !last) return false;
                if (last.querySelector('generated-image, single-image')) return false;
                const md = last.querySelector('message-content .markdown');
                return !!md && (md.innerText || '').trim().length > 0;
            })()""" % self._GENERATING_JS, timeout=5))
        except Exception:
            return False

    async def download_images(self, ws, urls) -> list[tuple[str, str]]:
        """MEASURED: the Gemini image sits at a `blob:` URL that is revoked once loaded —
        `fetch` fails with "Failed to fetch". The image is drawn from the page's <img> onto a
        canvas and exported as PNG (the blob belongs to this page — the canvas is not "tainted")."""
        out: list[tuple[str, str]] = []
        rest = []
        for u in urls:
            if not u.startswith("blob:"):
                rest.append(u)
                continue
            data = await cdp.evaluate(ws, """(() => {
                const i = [...document.querySelectorAll('model-response img')]
                    .find(i => (i.currentSrc || i.src) === %s);
                if (!i || !i.naturalWidth) return null;
                const c = document.createElement('canvas');
                c.width = i.naturalWidth; c.height = i.naturalHeight;
                c.getContext('2d').drawImage(i, 0, 0);
                try { return c.toDataURL('image/png'); } catch (e) { return null; }
            })()""" % json.dumps(u), timeout=60)
            if isinstance(data, str) and data.startswith("data:image"):
                head, _, payload = data.partition(",")
                out.append((head[5:].split(";")[0], payload))
            else:
                log.warning("gemini: could not extract the image via canvas")
        return out + (await super().download_images(ws, rest) if rest else [])

    # --- Chat history cleanup --------------------------------------------------
    #
    # POLICY: "all" — confirmed by the owner: ALL chats on the Gemini account are
    # disposable. Requests run in temporary chats and never reach the list, so every
    # chat in the list is test leftovers or a request where temporary mode failed to
    # turn on: background cleanup keeps none of them either (`CLEANUP_KEEP = 0`).
    CLEANUP_POLICY = "all"
    CLEANUP_KEEP = 0

    # MEASURED (production, Russian UI):
    #   * the Chrome window is narrow (viewport ~952x449) — Gemini serves the "mobile" layout:
    #     the list lives in an overlay drawer (`mat-sidenav`) whose open state persists and
    #     blocks the page on later requests. So during cleanup the viewport is set to
    #     1280x800 (`Emulation.setDeviceMetricsOverride`): there the side panel is
    #     `bard-sidenav` (`collapsed` class when closed), the open button is
    #     `[data-test-id=side-nav-sparkle-button]` ("Открыть боковую панель");
    #   * row: `a[href^="/app/<id>"]` inside `[data-test-id=conversation]` — with the panel
    #     closed the row is EMPTY (only Angular comments). Scrolling the list down loads
    #     more rows (28 -> 46);
    #   * menu: the row's `[data-test-id=actions-menu-button] button` ("<name>: дополнительные
    #     параметры") — opens with a DOM click without hover; items Поделиться / Закрепить /
    #     Переименовать / Добавить в блокнот / Удалить (`[data-test-id=delete-button]`);
    #   * confirmation: `mat-dialog-container` "Удалить чат?" — buttons "Отмена" / "Удалить"
    #     (no data-test-id, matched by text). After the click the row disappears in ~0.5 s.
    #   * notebooks in the "Блокноты" section are not chats — they are left alone.

    _ROW_SEL = '[data-test-id=conversation] a[href^="/app/"]'

    _OPEN_PANEL_JS = """(() => {
        const sn = document.querySelector('bard-sidenav');
        if (!sn) return 'no_sidenav';
        if (!/\\bcollapsed\\b/.test(String(sn.className))) return 'open';
        const b = document.querySelector('[data-test-id=side-nav-sparkle-button]')
            || [...document.querySelectorAll('button')].find(e => e.offsetParent
                && /Открыть боковую панель|Open sidebar|Expand menu/i.test(e.getAttribute('aria-label') || ''));
        if (!b) return 'no_button';
        b.click(); return 'opened';
    })()"""

    _CLOSE_PANEL_JS = """(() => {
        const b = [...document.querySelectorAll('button')].find(e => e.offsetParent
            && /Закрыть боковую панель|Close sidebar|Collapse menu/i.test(e.getAttribute('aria-label') || ''));
        if (!b) return false; b.click(); return true;
    })()"""

    # Whether the narrow-window drawer is left open (it blocks the page)
    _DRAWER_OPEN_JS = "!!document.querySelector('mat-sidenav.mat-drawer-opened')"

    _ROWS_JS = """(() => [...document.querySelectorAll('%s')].filter(a => a.offsetParent)
        .map(a => ({ href: a.getAttribute('href'), title: (a.innerText || '').trim().slice(0, 80) })))()""" % _ROW_SEL

    _COUNT_JS = "[...document.querySelectorAll('%s')].filter(a => a.offsetParent).length" % _ROW_SEL

    # Scrolls the list to the end (to load further rows) and returns the count
    _SCROLL_JS = """(() => {
        const rows = [...document.querySelectorAll('%s')].filter(a => a.offsetParent);
        let el = rows[rows.length - 1], cont = null;
        while (el) { if (el.scrollHeight > el.clientHeight + 20 && el.clientHeight > 100) { cont = el; break; }
                     el = el.parentElement; }
        if (cont) cont.scrollTop = cont.scrollHeight;
        else if (rows.length) rows[rows.length - 1].scrollIntoView({ block: 'end' });
        return rows.length;
    })()""" % _ROW_SEL

    _MENU_DELETE_JS = """(() => {
        const it = [...document.querySelectorAll('.cdk-overlay-pane [data-test-id=delete-button], .cdk-overlay-pane [role=menuitem]')]
            .find(e => e.offsetParent !== null && (e.getAttribute('data-test-id') === 'delete-button'
                       || /^(Удалить|Delete)$/i.test((e.innerText || '').trim())));
        if (!it) return false; it.click(); return true; })()"""

    _CONFIRM_JS = """(() => {
        const b = [...document.querySelectorAll('mat-dialog-container button')]
            .find(e => e.offsetParent !== null && /^(Удалить|Delete)$/i.test((e.innerText || '').trim()));
        if (!b) return false; b.click(); return true; })()"""

    _CANCEL_JS = """(() => {
        const b = [...document.querySelectorAll('mat-dialog-container button')]
            .find(e => e.offsetParent !== null && /^(Отмена|Cancel)$/i.test((e.innerText || '').trim()));
        if (b) { b.click(); return true; } return false; })()"""

    @classmethod
    def _has_href_js(cls, href: str) -> str:
        return "!!document.querySelector('[data-test-id=conversation] a[href=%s]')" % json.dumps(href)

    @classmethod
    def _open_menu_js(cls, href: str) -> str:
        return """(() => {
            const a = document.querySelector('[data-test-id=conversation] a[href=%s]');
            const row = a && a.closest('[data-test-id=conversation]');
            if (!row) return false;
            row.scrollIntoView({ block: 'center' });
            const b = row.querySelector('[data-test-id=actions-menu-button] button');
            if (!b) return false; b.click(); return true; })()""" % json.dumps(href)

    async def _wide_panel(self, ws) -> bool:
        """Wide viewport and an open side panel. `True` — chat rows are visible."""
        await cdp.call(ws, "Emulation.setDeviceMetricsOverride",
                       {"width": 1280, "height": 800, "deviceScaleFactor": 1, "mobile": False}, timeout=10)
        state = None
        for _ in range(20):
            await asyncio.sleep(0.5)
            if await self._count(ws):
                return True
            if state != "opened":
                state = await cdp.evaluate(ws, self._OPEN_PANEL_JS, timeout=10)
        log.info("gemini: cleanup — chat rows not visible (panel: %s)", state)
        return False

    async def _restore_view(self, ws) -> None:
        """Closes the panel and restores the viewport (so the drawer does not block the page)."""
        try:
            await self._close_overlays(ws)
            await cdp.evaluate(ws, self._CLOSE_PANEL_JS, timeout=10)
            await asyncio.sleep(0.8)
        except Exception as e:
            log.warning("gemini: could not close the panel: %s", e)
        try:
            await cdp.call(ws, "Emulation.clearDeviceMetricsOverride", {}, timeout=10)
            await asyncio.sleep(1.0)
            if await cdp.evaluate(ws, self._DRAWER_OPEN_JS, timeout=10):
                await cdp.evaluate(ws, self._CLOSE_PANEL_JS, timeout=10)
                await asyncio.sleep(0.8)
                if await cdp.evaluate(ws, self._DRAWER_OPEN_JS, timeout=10):
                    log.warning("gemini: the side panel in the narrow window stayed open")
        except Exception as e:
            log.warning("gemini: could not restore the viewport: %s", e)

    async def _count(self, ws) -> int:
        return int(await cdp.evaluate(ws, self._COUNT_JS, timeout=10) or 0)

    async def _load_rows(self, ws, max_rounds: int = 12) -> tuple[list, bool]:
        """Loads the list to the end. Returns: (rows, fully loaded)."""
        prev, stable = -1, 0
        for _ in range(max_rounds):
            n = int(await cdp.evaluate(ws, self._SCROLL_JS, timeout=10) or 0)
            stable = stable + 1 if n == prev else 0
            if stable >= 2:
                break
            prev = n
            await asyncio.sleep(0.8)
        rows = await cdp.evaluate(ws, self._ROWS_JS, timeout=10)
        return (rows if isinstance(rows, list) else []), stable >= 2

    async def _close_overlays(self, ws) -> None:
        try:
            if not await cdp.evaluate(ws, self._CANCEL_JS, timeout=5):
                await cdp.key(ws, "Escape", "Escape", 27)
        except Exception:
            pass
        await asyncio.sleep(0.5)

    async def plan_old_chats(self, ws, keep: int = 0) -> dict:
        """DRY RUN: counts what would be deleted. The top of the list holds the newest chats."""
        try:
            rows, complete = ([], True)
            if await self._wide_panel(ws):
                rows, complete = await self._load_rows(ws)
        finally:
            await self._restore_view(ws)
        keep = max(0, keep)
        return {"delete": rows[keep:], "protect": rows[:keep], "skip": [],
                "ledger_size": None, "rows": len(rows), "complete": complete}

    async def _delete_href(self, ws, href: str) -> bool:
        """Deletes one chat. Success means that `href` disappeared from the list."""
        if not await cdp.evaluate(ws, self._open_menu_js(href)):
            log.warning("gemini: delete — menu button not found %s", href[-8:])
            return False
        await asyncio.sleep(0.8)
        if not await cdp.evaluate(ws, self._MENU_DELETE_JS):
            log.warning("gemini: delete — no 'Удалить' in the menu %s", href[-8:])
            await self._close_overlays(ws)
            return False
        await asyncio.sleep(0.8)
        if not await cdp.evaluate(ws, self._CONFIRM_JS):
            log.warning("gemini: delete — confirmation dialog did not appear %s", href[-8:])
            await self._close_overlays(ws)
            return False
        for _ in range(20):
            await asyncio.sleep(0.5)
            if not await cdp.evaluate(ws, self._has_href_js(href)):
                return True
        log.warning("gemini: delete — still in the list after 10 s %s", href[-8:])
        await self._close_overlays(ws)
        return False

    async def delete_old_chats(self, ws, keep: int = 0, max_per_run: int = 40) -> int:
        """Deletes chats starting from the END of the list (the oldest); the top `keep` stay.

        Policy "all" (see above). At most `max_per_run` per call — so the worker lock
        is not held for long. Stops after three consecutive failures (the UI has
        changed). Finally the panel is closed and the viewport restored.
        """
        keep = max(0, keep)
        deleted, failures = 0, 0
        try:
            if not await self._wide_panel(ws):
                return 0
            await self._load_rows(ws)
            while deleted < max_per_run:
                rows = await cdp.evaluate(ws, self._ROWS_JS, timeout=10)
                rows = rows if isinstance(rows, list) else []
                if len(rows) <= keep:
                    # None left on screen — are there more not yet loaded below?
                    await cdp.evaluate(ws, self._SCROLL_JS, timeout=10)
                    await asyncio.sleep(1.0)
                    if await self._count(ws) <= keep:
                        break
                    continue
                try:
                    ok = await self._delete_href(ws, str(rows[-1].get("href") or ""))
                except Exception as e:
                    log.warning("gemini: error while deleting a chat: %s", e)
                    await self._close_overlays(ws)
                    ok = False
                if ok:
                    deleted, failures = deleted + 1, 0
                else:
                    failures += 1
                    if failures >= 3:
                        log.warning("gemini: chat was not deleted (the UI may have changed) — stopping")
                        break
                await asyncio.sleep(0.3)
        finally:
            await self._restore_view(ws)
        if deleted:
            log.info("gemini: deleted %d chat(s)", deleted)
        return deleted


# MEASURED: the language name in a Gemini code block header ("Python") is read as a
# separate line and the fence has no language: "Python\n```\n...". The language name is
# moved onto the fence: "```python\n...".
_LABEL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+#.\- ]{0,19}$")


def _fix_code_labels(text: str) -> str:
    """Only the language line before an OPENING fence is moved (code lines such as `fi` or
    `end` before a closing fence are left alone)."""
    lines = text.split("\n")
    out: list[str] = []
    in_code = False
    for line in lines:
        if line.strip().startswith("```"):
            if (not in_code and line.strip() == "```" and out
                    and _LABEL_RE.match(out[-1].strip()) and out[-1] == out[-1].lstrip()):
                lang = out.pop().strip().lower().replace(" ", "")
                line = "```" + lang
            in_code = not in_code
        out.append(line)
    return "\n".join(out)
