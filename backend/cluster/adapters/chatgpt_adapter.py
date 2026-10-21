import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

from .. import cdp
from .. import settings as cfg
from .base_adapter import READ_TEXT_JS, BaseAIAdapter, Capabilities

log = logging.getLogger("cluster.adapter")

CHATGPT_INPUT = "#prompt-textarea, div.ProseMirror"

# ───────────────────────── Registry of chats created by the gateway ─────────────────────────
#
# Why: this ChatGPT account is the owner's PERSONAL account — its history holds
# their personal work. A "keep the newest N and delete the rest" rule cannot be
# used here.
#
# Measured (live DOM): the ChatGPT sidebar is a FLAT list — there are NO date
# group headers like "Today"/"Yesterday" at all. So DeepSeek's "protect by group
# header" scheme does not apply, and a "newest date group only" rule is impossible.
#
# So protection rests on a DIFFERENT, stronger basis: the gateway deletes ONLY
# chats IT CREATED ITSELF. After every request, if the chat got saved to history
# (the URL switched to `/c/<id>`), its ID is added to this file. Cleanup works
# only with the IDs in this file — the owner's chats CAN NEVER be in the
# registry, so they are never deleted.
_STATE_DIR = Path(os.getenv("SF_CLUSTER_STATE_DIR", "/var/lib/ai-gateway/cluster"))
_LEDGER_PATH = Path(os.getenv("SF_CHATGPT_LEDGER_FILE", str(_STATE_DIR / "chatgpt_owned_chats.json")))

# Second layer of protection: the FIRST 8 hex digits of a chat ID are its
# creation time (Unix). MEASURED (238 chats): the decoded dates matched the
# sidebar order exactly —
# `6ad6680d` → 2026-10-19, `67dbcd12` → 2025-03, `67b44942` → 2025-02.
# A chat created by the gateway is always A FEW MINUTES old, while the owner's
# chats are much older. So a chat older than this is never deleted.
# This layer only PROTECTS: if the time cannot be read the chat is kept.
_MAX_DELETE_AGE = 7 * 24 * 3600

# Lifetime of a registry entry — 30 days. Older entries are dropped
# (the chat may have been deleted manually or already cleaned up).
_LEDGER_TTL = 30 * 24 * 3600

# The ID in `/c/<id>` — only UUID-shaped values are accepted
_CHAT_ID_RE = re.compile(r"/c/([0-9a-fA-F-]{16,64})")


def _chat_age(chat_id: str) -> Optional[int]:
    """Returns the chat's age (seconds) from its ID. None if unreadable."""
    try:
        ts = int(chat_id[:8], 16)
    except Exception:
        return None
    # Sanity range: from 2020-01-01 to one day past now
    now = int(time.time())
    if not (1577836800 < ts < now + 86400):
        return None
    return now - ts


def _ledger_load() -> dict:
    """Reads the registry: {chat_id: added_at}. On error — an EMPTY dict.

    Empty dict = "nothing belongs to us" = nothing gets deleted.
    A corrupt or unreadable file also fails on the safe side.
    """
    try:
        data = json.loads(_LEDGER_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items() if _CHAT_ID_RE.search("/c/" + str(k))}


def _ledger_save(data: dict) -> None:
    try:
        _LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _LEDGER_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(_LEDGER_PATH)
    except Exception as e:
        log.warning("could not write the chatgpt registry: %s", e)


def _ledger_add(chat_id: str) -> None:
    data = _ledger_load()
    if chat_id in data:
        return
    data[chat_id] = int(time.time())
    _ledger_save(data)
    log.info("chatgpt: chat saved to history added to the registry id=%s (temporary mode did not hold)",
             chat_id[:8])


class ChatGPTAdapter(BaseAIAdapter):
    # Measured DOM: 4 hidden file inputs — one with `accept=""` (any file), the
    # rest `accept="image/*"`. Using the dedicated image input makes the site take
    # it as an image (vision); documents go to the generic input.
    FILE_INPUT = 'input[type="file"]:not([accept]), input[type="file"][accept=""]'
    IMAGE_INPUT = 'input[type="file"][accept*="image"]'

    # Measured: does not follow the tool protocol — asks for files and answers in text.
    # So requests with `tools` are NOT sent to this model.
    # Measured (live test): vision 24 s, document reading 26 s,
    # image generation 28 s (1 MB PNG) — all three work.
    # `delete_chats` is ENABLED AGAIN, but on an entirely different basis:
    # `delete_old_chats` now deletes ONLY chats the gateway created itself (IDs in
    # the registry) — see the registry comment above. The owner's personal chats
    # can never be in the registry.
    #
    # The key point: the main solution is not cleanup but TEMPORARY MODE. It was
    # measured that `reset_chat` used to click the sidebar "New chat" button, which
    # took the page from `?temporary-chat=true` to `/` — so after the first request
    # EVERY chat was saved to history. Now `reset_chat` restores temporary mode every
    # time, so in normal operation nothing is saved and the registry stays empty.
    caps = Capabilities(tool_calling=False, vision=True, files=True, image_gen=True,
                        temporary_chat=True, delete_chats=True)
    # Manual cleanup from the admin panel is allowed ONLY for adapters with this
    # policy: deletion is limited to IDs in the gateway's registry.
    CLEANUP_POLICY = "registry"

    def __init__(self):
        super().__init__("chatgpt")

    @staticmethod
    def registry_size() -> int:
        """Number of chats in the registry (created by the gateway) — for the admin panel."""
        return len(_ledger_load())

    # Temporary mode marker — two reliable signals measured in the DOM:
    # the banner text and the "Turn off temporary chat" button.
    _TEMP_STATE_JS = """(() => {
        const banner = Array.from(document.querySelectorAll('div, p, span')).some(e =>
            /this chat won.t appear in your conversation history/i.test(e.textContent || '')
            && (e.textContent || '').length < 200);
        const off = Array.from(document.querySelectorAll('button')).some(e =>
            /^turn off temporary chat$/i.test(e.getAttribute('aria-label') || ''));
        return { temporary: banner || off, url: location.href.slice(0, 120) };
    })()"""

    async def reset_chat(self, ws, timeout: float = 10.0) -> None:
        """Reset for ChatGPT — a new chat + TEMPORARY MODE.

        NOTE: `location.href` or `Page.navigate` on chatgpt.com is expensive —
        ChatGPT may recreate the CDP target on a full load, and a measured full
        load takes ~4-6 s (a click within the SPA ~1.5 s). So we act within the
        SPA first; a full load is only the fallback.

        MEASURED (live page) — why it is written this way:
          * The page opens with `?temporary-chat=true` and the mode works
            (banner: "This chat won't appear in your conversation history").
          * Clicking the sidebar "New chat" button moves the URL to `/` and the
            banner DISAPPEARS — i.e. the mode TURNS OFF. That is exactly why API
            test chats showed up in the history.
          * To restore the mode the SPA has an `aria-label="Temporary chat"`
            button (it becomes `"Turn off temporary chat"` when the mode is on).
            Clicking it returns the URL to `?temporary-chat=true` WITHOUT
            reloading the page.
        Hence the order: "New chat" → TURN ON the mode → verify.
        """
        reset_js = """(async () => {
            const sleep = ms => new Promise(r => setTimeout(r, ms));
            const isTemp = () =>
                Array.from(document.querySelectorAll('div, p, span')).some(e =>
                    /this chat won.t appear in your conversation history/i.test(e.textContent || '')
                    && (e.textContent || '').length < 200)
                || Array.from(document.querySelectorAll('button')).some(e =>
                    /^turn off temporary chat$/i.test(e.getAttribute('aria-label') || ''));

            // 1) New chat — through the React router, no reload
            const sels = [
                'a[data-testid="create-new-chat-button"]',
                'button[data-testid="create-new-chat-button"]',
                'button[aria-label*="New chat" i]',
                'a[aria-label*="New chat" i]',
                'button[aria-label*="Новый чат" i]',
                'a[aria-label*="Новый чат" i]',
            ];
            let opened = null;
            for (const s of sels) {
                const el = document.querySelector(s);
                if (el) { el.click(); opened = s; break; }
            }
            if (!opened) {
                document.dispatchEvent(new KeyboardEvent('keydown', {
                    key: 'O', code: 'KeyO', ctrlKey: true, shiftKey: true, bubbles: true
                }));
            }
            await sleep(900);

            // 2) TURN ON temporary mode — "New chat" switches it off
            for (let i = 0; i < 3; i++) {
                if (isTemp()) return { opened: opened, temporary: true, toggled: i > 0 };
                const t = Array.from(document.querySelectorAll('button')).find(e =>
                    /^(turn on )?temporary chat$/i.test(e.getAttribute('aria-label') || ''));
                if (!t) break;
                t.click();
                await sleep(900);
            }
            return { opened: opened, temporary: isTemp(), toggled: true };
        })()"""
        state = None
        try:
            # NOTE: `reset_js` is an async IIFE, i.e. it returns a Promise.
            # Without `await_promise=True` `cdp.evaluate` returns an empty `{}`
            # and the code assumes the mode is off (this silent bug happened
            # once exactly like that).
            state = await cdp.evaluate(ws, reset_js, timeout=20, await_promise=True)
        except Exception:
            return
        await asyncio.sleep(0.4)

        if isinstance(state, dict) and state.get("temporary"):
            return

        # Could not turn the mode on within the SPA — fallback: a full load.
        # It is expensive (~4-6 s) and may recreate the target, but
        # `WorkerTab._reset_phase` treats that as expected.
        log.warning("chatgpt: temporary mode did not turn on within the SPA (%s) — full reload",
                    state)
        await self.goto(ws, "https://chatgpt.com/?temporary-chat=true")
        await self.wait_for_input(ws, timeout=timeout)

    async def reset_chat_for_images(self, ws, timeout: float = 10.0) -> None:
        """A REGULAR (non-temporary) new chat for an image request.

        MEASURED (live page): ChatGPT does not draw images in a temporary chat —
        it replies asking to switch to a regular (non-temporary) chat to create
        images, and the request waited 180 s and came back empty.
        So image requests run in a regular chat; after the answer the chat ID is
        written to the gateway's registry (`after_image_response`) and cleanup
        deletes it later — the owner's chats are never touched.
        """
        js = """(async () => {
            const sleep = ms => new Promise(r => setTimeout(r, ms));
            const isTemp = () =>
                Array.from(document.querySelectorAll('div, p, span')).some(e =>
                    /this chat won.t appear in your conversation history/i.test(e.textContent || '')
                    && (e.textContent || '').length < 200)
                || Array.from(document.querySelectorAll('button')).some(e =>
                    /^turn off temporary chat$/i.test(e.getAttribute('aria-label') || ''));
            const sels = [
                'a[data-testid="create-new-chat-button"]',
                'button[data-testid="create-new-chat-button"]',
                'button[aria-label*="New chat" i]',
                'a[aria-label*="New chat" i]',
            ];
            let opened = null;
            for (const s of sels) {
                const el = document.querySelector(s);
                if (el) { el.click(); opened = s; break; }
            }
            await sleep(900);
            for (let i = 0; i < 3 && isTemp(); i++) {
                const t = Array.from(document.querySelectorAll('button')).find(e =>
                    /^turn off temporary chat$/i.test(e.getAttribute('aria-label') || ''));
                if (!t) break;
                t.click();
                await sleep(900);
            }
            const u = new URL(location.href);
            return { opened: opened, temporary: isTemp(),
                     tempUrl: u.searchParams.get('temporary-chat') === 'true',
                     fresh: !document.querySelector('[data-message-author-role]') };
        })()"""
        state = None
        try:
            state = await cdp.evaluate(ws, js, timeout=20, await_promise=True)
        except Exception:
            state = None
        await asyncio.sleep(0.4)
        if (isinstance(state, dict) and state.get("opened") and state.get("fresh")
                and not state.get("temporary") and not state.get("tempUrl")):
            return
        log.warning("chatgpt: could not open a regular chat for images within the SPA (%s) — full reload",
                    state)
        await self.goto(ws, "https://chatgpt.com/")
        await self.wait_for_input(ws, timeout=timeout)

    async def finished_without_image(self, ws) -> bool:
        # MEASURED: on a refusal ("switch to a regular chat") or an image analysis
        # answer the stop button disappears and no image ever appears.
        # Signal: generation finished and no "creating image" label on the page.
        # (The last assistant message text used to be required too — it was not
        # read for an analysis answer and the wait dragged on for 110 s.)
        js = """(() => {
            const stop = document.querySelector('button[data-testid="stop-button"], button[aria-label*="Stop" i]');
            const main = document.querySelector('main') || document.body;
            const tail = (main.innerText || '').slice(-1500);
            const busy = /creating image|generating image|making (an |your )?image|rasm yaratilmoqda|создаю изображение|создание изображения/i.test(tail);
            return !stop && !busy;
        })()"""
        try:
            return bool(await cdp.evaluate(ws, js, timeout=5))
        except Exception:
            return False

    async def generation_done(self, ws) -> bool:
        js = """(() => !document.querySelector('button[data-testid="stop-button"], button[aria-label*="Stop" i]'))()"""
        try:
            return bool(await cdp.evaluate(ws, js, timeout=5))
        except Exception:
            return False

    async def after_image_response(self, ws) -> None:
        """Writes the image chat's ID to the registry — WITHOUT waiting for the sidebar.

        This chat was opened in regular mode by `reset_chat_for_images` itself, so
        it certainly belongs to the gateway. Waiting for it to show up in the
        sidebar (~6 s until a title is generated) would slow every image request.
        """
        js = """(() => { const m = location.pathname.match(/\\/c\\/([0-9a-fA-F-]{16,64})/); return m ? m[1] : null; })()"""
        try:
            cid = await cdp.evaluate(ws, js, timeout=5)
        except Exception:
            cid = None
        if isinstance(cid, str) and cid:
            _ledger_add(cid)
            log.info("chatgpt: image chat added to the registry id=%s", cid[:8])

    async def send_prompt(self, ws, prompt_text: str) -> bool:
        # NOTE: the ChatGPT page has TWO `div.ProseMirror` at once (one is a hidden
        # duplicate holding old text). So only the VISIBLE editor is chosen —
        # otherwise the text lands in the hidden editor, React thinks the composer
        # is empty and the Send button never appears.
        #
        # Text is entered via `execCommand('insertText')`: it produces real
        # beforeinput/input events and ProseMirror updates its own state.
        # `innerHTML` or CDP `Input.insertText` do not work here.
        send_script = f"""(() => {{
            const all = Array.from(document.querySelectorAll('div.ProseMirror, textarea#prompt-textarea'));
            const vis = all.filter(e => {{
                const r = e.getBoundingClientRect();
                return r.width > 50 && r.height > 5 && e.offsetParent !== null;
            }});
            const el = vis.find(e => String(e.className).includes('ProseMirror-focused'))
                    || vis[vis.length - 1] || all[all.length - 1];
            if (!el) return {{ error: 'Text input field not found' }};

            el.focus();
            if (el.tagName === 'TEXTAREA') {{
                const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
                setter.call(el, {json.dumps(prompt_text)});
                el.dispatchEvent(new Event('input', {{ bubbles: true }}));
            }} else {{
                // Select all content — an old draft gets replaced this way too
                const range = document.createRange();
                range.selectNodeContents(el);
                const sel = window.getSelection();
                sel.removeAllRanges();
                sel.addRange(range);
                if (!document.execCommand('insertText', false, {json.dumps(prompt_text)})) {{
                    return {{ error: "execCommand could not insert the text" }};
                }}
            }}
            return {{ filled: true }};
        }})()"""

        val = await cdp.evaluate(ws, send_script)
        if not isinstance(val, dict) or not val.get("filled"):
            return False

        # Short pause so React shows the Send button
        await asyncio.sleep(0.6)

        click_script = """(() => {
            const btn = Array.from(document.querySelectorAll('button')).find(b => {
                if (b.disabled || b.getBoundingClientRect().width < 10) return false;
                const a = b.getAttribute('aria-label') || '';
                return b.getAttribute('data-testid') === 'send-button'
                    || /^(Send|Отправить)/i.test(a) || a.includes('Отправить сообщение');
            });
            if (!btn) return false;
            btn.click();
            return true;
        })()"""
        if await cdp.evaluate(ws, click_script):
            return True

        # No button found — press Enter inside the editor
        press_enter = """(() => {
            const all = Array.from(document.querySelectorAll('div.ProseMirror'));
            const el = all.filter(e => e.offsetParent !== null).pop();
            if (!el) return false;
            el.dispatchEvent(new KeyboardEvent('keydown',
                { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }));
            return true;
        })()"""
        return bool(await cdp.evaluate(ws, press_enter))

    # Reasoning effort (Plus account, measured on the live page):
    # the composer's `button[data-selected-reasoning-effort]` (aria-label "Select ChatGPT
    # model") opens a menu with a `[role=slider]` 0..2 (2 of 3 = medium), moved with
    # ArrowLeft/Right. The current level is in the button's `data-selected-reasoning-effort` attribute.
    _EFFORT_BTN_JS = """(() => { const b = document.querySelector('button[data-selected-reasoning-effort]');
        if (!b || !(b.offsetWidth || b.offsetHeight)) return null; const r = b.getBoundingClientRect();
        return { effort: b.getAttribute('data-selected-reasoning-effort'), x: r.left + r.width / 2, y: r.top + r.height / 2 }; })()"""
    _SLIDER_JS = """(() => { const s = [...document.querySelectorAll('[role=slider]')].find(e => e.offsetWidth || e.offsetHeight);
        if (!s) return null; s.focus();
        return { now: +s.getAttribute('aria-valuenow'), min: +(s.getAttribute('aria-valuemin') || 0),
                 max: +(s.getAttribute('aria-valuemax') || 0) }; })()"""
    EFFORT_POS = {"none": 0, "minimal": 0, "low": 0, "medium": 1, "high": 2, "xhigh": 2}

    async def set_effort(self, ws, effort: Optional[str]) -> Optional[str]:
        """Sets the requested (or default) reasoning effort on the slider.

        If the client does not specify one, `cfg.CHATGPT_DEFAULT_EFFORT` is used: the
        level persists on the account and must not carry over from another client. It
        never moves onto a locked step ("Locked, opens access options") — stops if the
        slider does not move.
        """
        effort = (effort or cfg.CHATGPT_DEFAULT_EFFORT or "").strip().lower()
        target = self.EFFORT_POS.get(effort)
        if target is None:
            return None
        btn = await cdp.evaluate(ws, self._EFFORT_BTN_JS, timeout=10)
        if not isinstance(btn, dict):
            return None   # no control (e.g. not Plus) — the slider is left alone
        # SPEED: if the level is already right the menu is not opened (it cost ~0.8–1.5 s per request).
        # The button attribute gives the step name: 0 = "none", 1 = "medium", 2 = "high".
        names = {0: "none", 1: "medium", 2: "high"}
        if (btn.get("effort") or "").lower() == names.get(target):
            return btn.get("effort")
        await cdp.click_at(ws, btn["x"], btn["y"])
        sl = None
        for _ in range(12):
            await asyncio.sleep(0.25)
            sl = await cdp.evaluate(ws, self._SLIDER_JS, timeout=10)
            if isinstance(sl, dict):
                break
        if not isinstance(sl, dict):
            await cdp.key(ws, "Escape", "Escape", 27)
            return btn.get("effort")
        target = max(sl["min"], min(sl["max"], target))
        now = sl["now"]
        while now != target:
            key, code, vk = ("ArrowRight", "ArrowRight", 39) if target > now else ("ArrowLeft", "ArrowLeft", 37)
            await cdp.key(ws, key, code, vk)
            await asyncio.sleep(0.35)
            cur = await cdp.evaluate(ws, self._SLIDER_JS, timeout=10)
            if not isinstance(cur, dict) or cur["now"] == now:
                break   # locked, or the menu closed
            now = cur["now"]
        await cdp.key(ws, "Escape", "Escape", 27)
        await asyncio.sleep(0.3)
        # If a locked step opened the subscription dialog — close it too
        await self.dismiss_overlays(ws)
        after = await cdp.evaluate(ws, self._EFFORT_BTN_JS, timeout=10)
        return (after or {}).get("effort") if isinstance(after, dict) else None

    _FRESH_JS = """(() => {
        const u = new URL(location.href);
        const tmp = u.searchParams.get('temporary-chat') === 'true' && (u.pathname === '/' || u.pathname === '');
        const msgs = document.querySelectorAll('[data-message-author-role], [data-markdown-text-style="assistant-message"]').length;
        const said = [...document.querySelectorAll('h4, h5')].filter(h => /(ChatGPT|You) (said|сказал)/i.test(h.innerText || '')).length;
        return tmp && msgs === 0 && said === 0;
    })()"""

    async def is_fresh_chat(self, ws) -> bool:
        return await cdp.evaluate(ws, self._FRESH_JS, timeout=8) is True

    # A logged-out ChatGPT page: visible "Log in" / "Sign up for free" buttons.
    # A logged-in user never has them.
    _LOGGED_OUT_JS = """(() => [...document.querySelectorAll('button, a')].some(b => {
        const t = (b.innerText || '').trim().toLowerCase();
        return ['log in', 'sign up for free', 'sign up'].includes(t) && !!(b.offsetWidth || b.offsetHeight);
    }))()"""

    async def check_session(self, ws, timeout: float = 5.0, recover: bool = True) -> tuple[bool, str]:
        """The generic check + ChatGPT's logged-out state.

        Once the session cookie was lost the page opened in anonymous mode — it
        had an input field and none of the generic login phrases, so the check
        reported the session as fine and nobody was alerted, while requests kept
        failing because the text could not be typed into the input field.
        """
        try:
            logged_out = await cdp.evaluate(ws, self._LOGGED_OUT_JS, timeout=timeout)
        except Exception as e:
            return False, f"probe_failed:{type(e).__name__}"
        if logged_out is True:
            return False, "login_required"
        return await super().check_session(ws, timeout=timeout, recover=recover)

    async def read_response(self, ws, timeout: int = 45) -> Optional[str]:
        # Take only the .markdown child — so UI texts like "I prefer this response",
        # "Response 2", "Think", "Response complete", "ChatGPT can make mistakes"
        # do not leak into the answer.
        # Text is read with READ_TEXT_JS: the code block header ("Python", "Run")
        # stays out of the answer, code is wrapped in ``` and lines are kept exactly
        # (previously a stray "Python" line in generated code raised NameError and
        # none of 77 cases passed).
        read_script = """(() => {
            const READ = __READ__;
            const generating = !!document.querySelector(
                'button[aria-label*="Stop" i], button[aria-label*="Остановить" i], button[data-testid*="stop"]'
            );
            let answer = '';
            const assistants = document.querySelectorAll('[data-message-author-role="assistant"]');
            if (assistants.length > 0) {
                const last = assistants[assistants.length - 1];
                const md = last.querySelector('.markdown, .prose, .markdown-prose, [data-markdown-text-style]');
                answer = READ(md || last);
            }
            if (!answer) {
                const h4s = Array.from(document.querySelectorAll('h4')).filter(h =>
                    /ChatGPT (said|сказал)/i.test(h.innerText || '')
                );
                if (h4s.length > 0) {
                    const sib = h4s[h4s.length - 1].nextElementSibling;
                    if (sib) {
                        const md = sib.querySelector('[data-markdown-text-style], .markdown');
                        answer = READ(md || sib);
                    }
                }
            }
            return { generating, answer };
        })()""".replace("__READ__", READ_TEXT_JS)
        raw = await self.poll_answer(ws, read_script, timeout=timeout, initial_delay=1.0)
        await self._record_if_persisted(ws)
        return self.clean_text(self._strip_ui_noise(raw or ""))

    async def _record_if_persisted(self, ws, tries: int = 3) -> None:
        """If the chat GOT SAVED TO HISTORY, adds its ID to the registry.

        MEASURED — why looking at the URL is NOT ENOUGH: a temporary chat also
        moves to `/c/<id>` after the answer and the banner disappears, so the URL
        shape does NOT mean "saved". The difference is elsewhere: a temporary chat
        NEVER APPEARS IN THE SIDEBAR. In a test three temporary chat IDs
        (`6ad66a5e`, `6ad66aba`, `6ad66af0`) were not in the panel, and the newest
        history row was still a chat created BEFORE the fix.

        So the signal is: is the current chat ID among the sidebar links. If not —
        nothing is recorded (the safe side: a chat not in the registry is never
        deleted either).
        """
        js = """(() => {
            const m = location.pathname.match(/\\/c\\/([0-9a-fA-F-]{16,64})/);
            if (!m) return null;
            const id = m[1];
            const inSidebar = Array.from(document.querySelectorAll('a[href*="/c/"]'))
                .some(a => (a.getAttribute('href') || '').includes(id));
            return { id: id, inSidebar: inSidebar };
        })()"""
        try:
            for _attempt in range(tries):
                val = await cdp.evaluate(ws, js, timeout=5)
                if not isinstance(val, dict):
                    return
                if val.get("inSidebar"):
                    _ledger_add(str(val["id"]))
                    return
                await asyncio.sleep(0.8)
        except Exception:
            pass

    @staticmethod
    def _strip_ui_noise(text: str) -> str:
        """Strips ChatGPT UI texts (A/B comparison, regenerate, buttons)."""
        if not text:
            return text
        noise_tails = [
            "I prefer this response",
            "Response 1", "Response 2",
            "ChatGPT can make mistakes",
            "Latest response",
            "Response complete",
            "Regenerate",
        ]
        for n in noise_tails:
            idx = text.find(n)
            if idx > 0:
                text = text[:idx].strip()
        if text.endswith("Think"):
            text = text[:-5].strip()
        return text

    # Reading sidebar rows: the ID from the href, the title, the pin state.
    #
    # Measured: a row = `a[href*="/c/<id>"]` with an `aria-label="Chat actions"`
    # button inside one of its ancestors. The pin state is read from the button
    # label within that same ancestor:
    #   `Pin chat`   → not pinned
    #   `Unpin chat` → PINNED
    # If neither is found the UI has changed, the row comes back with
    # `pinKnown: false` and cleanup DOES NOT TOUCH it.
    #
    # Titles REPEAT (measured: the same title twice), so the target is always
    # chosen ONLY by the ID in the href.
    _ROWS_JS = """(() => {
        const rows = [];
        for (const a of document.querySelectorAll('a[href*="/c/"]')) {
            const m = (a.getAttribute('href') || '').match(/\\/c\\/([0-9a-fA-F-]{16,64})/);
            if (!m) continue;
            let row = a, btn = null;
            for (let i = 0; i < 6 && row; i++) {
                btn = Array.from(row.querySelectorAll('button')).find(e =>
                    /^chat actions$/i.test(e.getAttribute('aria-label') || ''));
                if (btn) break;
                row = row.parentElement;
            }
            let pinned = null;
            if (row) {
                const labels = Array.from(row.querySelectorAll('button'))
                    .map(e => (e.getAttribute('aria-label') || '').toLowerCase());
                if (labels.includes('unpin chat')) pinned = true;
                else if (labels.includes('pin chat')) pinned = false;
            }
            rows.push({
                id: m[1],
                title: (a.innerText || '').trim().slice(0, 80),
                hasActions: !!btn,
                pinKnown: pinned !== null,
                pinned: pinned === true
            });
        }
        return rows;
    })()"""

    async def _read_rows(self, ws) -> list:
        rows = await cdp.evaluate(ws, self._ROWS_JS, timeout=20)
        return rows if isinstance(rows, list) else []

    async def _open_sidebar(self, ws, max_rounds: int = 24) -> bool:
        """Opens the sidebar and loads the chat list.

        Returns: whether the list was FULLY loaded (nothing more left to load).
        This matters — reading a partial list as "not in the panel" would make
        the registry get cleaned up wrongly.

        MEASURED — loading works in two parts:
          * the panel first shows only 20 rows with a **"Show more"** button at
            the end;
          * after clicking it scrolling works and each scroll adds +5 rows, then
            it stops again and "Show more" comes back.
        `scrollIntoView` on the last link is NOT ENOUGH (the list gets stuck at
        10-20 rows) — the `scrollTop` of the scrollable ANCESTOR container has to
        be moved.

        This account has 288 chats; loading them all takes ~1 minute and is NOT
        NEEDED FOR CLEANUP: chats created by the gateway are the newest, i.e. at
        the top of the list. So we cap it with `max_rounds` and report that the
        list was not fully loaded.
        """
        await cdp.evaluate(ws, """(() => {
            const b = Array.from(document.querySelectorAll('button')).find(e =>
                /show sidebar/i.test(e.getAttribute('aria-label') || ''));
            if (b) { b.click(); return true; } return false;
        })()""")
        await asyncio.sleep(1.5)

        step_js = """(() => {
            const more = Array.from(document.querySelectorAll('button, a')).find(e =>
                /^show more$/i.test((e.innerText || '').trim()));
            if (more) {
                more.click();
            } else {
                let el = document.querySelector('a[href*="/c/"]');
                let cont = null;
                while (el) {
                    if (el.scrollHeight > el.clientHeight + 20 && el.clientHeight > 100) { cont = el; break; }
                    el = el.parentElement;
                }
                if (cont) cont.scrollTop = cont.scrollHeight;
            }
            return { n: document.querySelectorAll('a[href*="/c/"]').length, more: !!more };
        })()"""

        stable = 0
        prev = -1
        for _ in range(max_rounds):
            val = await cdp.evaluate(ws, step_js)
            if not isinstance(val, dict):
                break
            n, has_more = val.get("n", 0), bool(val.get("more"))
            stable = stable + 1 if n == prev else 0
            prev = n
            # Fully loaded: not growing AND no "Show more" left
            if stable >= 5 and not has_more:
                return True
            await asyncio.sleep(0.8)
        return False

    async def plan_old_chats(self, ws, keep: int = 0) -> dict:
        """DRY RUN: computes what would be deleted, deletes nothing.

        Returns: {"delete": [...], "protect": [...], "skip": [...]}.
        `delete_old_chats` uses exactly this computation, so the dry-run list
        matches live behaviour 1:1.
        """
        complete = await self._open_sidebar(ws)
        rows = await self._read_rows(ws)
        registry = _ledger_load()

        to_delete, protected, skipped = [], [], []
        for r in rows:
            rid = r.get("id")
            if rid not in registry:
                # POSITIVE CONFIRMATION: not in the registry = not our chat = LEAVE IT
                protected.append(r)
            elif r.get("pinned"):
                protected.append(r)
            elif not r.get("pinKnown") or not r.get("hasActions"):
                # The UI changed — could not read the pin state or the menu
                skipped.append(r)
            elif (age := _chat_age(rid)) is None or age > _MAX_DELETE_AGE:
                # Second layer: an old chat or one whose time is unreadable — LEAVE IT
                protected.append(r)
            else:
                to_delete.append(r)

        # Sidebar order = recency order; keep the newest `keep`
        if keep > 0 and to_delete:
            protected = to_delete[:keep] + protected
            to_delete = to_delete[keep:]

        return {"delete": to_delete, "protect": protected, "skip": skipped,
                "ledger_size": len(registry), "rows": len(rows), "complete": complete}

    async def delete_old_chats(self, ws, keep: int = 0, max_per_run: int = 40) -> int:
        """Deletes old chats the gateway CREATED ITSELF. Touches nothing else.

        PROTECTION by POSITIVE CONFIRMATION (see the registry comment at the top):
        a chat is deleted ONLY when all of these hold —
          1. its ID is in the gateway's registry (i.e. the gateway created it),
          2. it is not pinned AND the pin state was read reliably,
          3. the row has a "Chat actions" button,
          4. the creation time decoded from the ID is newer than 7 days.
        If any condition fails the chat is kept. If the UI changes or the
        registry is empty/corrupt, cleanup STOPS SILENTLY (returns 0) — far
        better than deleting the owner's personal chat.

        Mechanism (measured and tested) — two subtle points:
          1. The "Chat actions" button only appears while the mouse is over the
             row — so a REAL hover + click is needed (`cdp.hover_click`), not
             `.click()`.
          2. After each deletion the list re-renders and positions shift — so
             rows are re-read at every step and the target is found ONLY by the
             ID in the href (titles repeat — measured).
        """
        plan = await self.plan_old_chats(ws, keep=keep)
        targets = [r["id"] for r in plan["delete"]][:max_per_run]
        if not targets:
            return 0

        log.info("chatgpt cleanup: registry %d, panel %d, targets %d, protected %d, skipped %d",
                 plan["ledger_size"], plan["rows"], len(targets),
                 len(plan["protect"]), len(plan["skip"]))

        deleted = 0
        for chat_id in targets:
            locator = """(() => {
                const want = %s;
                const a = Array.from(document.querySelectorAll('a[href*="/c/"]'))
                    .find(e => (e.getAttribute('href') || '').includes(want));
                if (!a) return null;
                a.scrollIntoView({ block: 'center' });
                let row = a, b = null;
                for (let i = 0; i < 6 && row; i++) {
                    b = Array.from(row.querySelectorAll('button')).find(e =>
                        /^chat actions$/i.test(e.getAttribute('aria-label') || ''));
                    if (b) break;
                    row = row.parentElement;
                }
                if (!b) return null;
                const br = b.getBoundingClientRect();
                return { x: Math.round(br.left + br.width / 2),
                         y: Math.round(br.top + br.height / 2) };
            })()""" % json.dumps(chat_id)

            success = False
            for _attempt in (1, 2):
                try:
                    if not await cdp.hover_click(ws, locator):
                        await asyncio.sleep(0.8)
                        continue
                    await asyncio.sleep(1.6)
                    if not await cdp.click_text(ws, ["Delete"]):
                        # The menu did not open — close it in case it stayed open
                        await cdp.key(ws, "Escape", "Escape", 27)
                        await asyncio.sleep(1.0)
                        continue
                    await asyncio.sleep(1.4)
                    await cdp.click_text(ws, ["Delete chat", "Delete"])
                    await asyncio.sleep(2.0)
                    remaining = {r.get("id") for r in await self._read_rows(ws)}
                    if chat_id not in remaining:
                        success = True
                        break
                    await asyncio.sleep(1.0)
                except Exception:
                    break

            if success:
                deleted += 1
                registry = _ledger_load()
                registry.pop(chat_id, None)
                _ledger_save(registry)

        # Keep the registry from growing forever. It is pruned by age ON PURPOSE,
        # NOT by "missing from the panel": the panel list may be partially loaded
        # (see `_open_sidebar`), and dropping an entry based on a partial list would
        # make that chat "not ours" afterwards — the gateway could no longer clean
        # up its own leftovers.
        try:
            cutoff = int(time.time()) - _LEDGER_TTL
            registry = _ledger_load()
            kept = {k: v for k, v in registry.items()
                     if not isinstance(v, int) or v > cutoff}
            if len(kept) != len(registry):
                _ledger_save(kept)
        except Exception:
            pass

        return deleted
