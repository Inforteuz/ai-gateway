import asyncio
import time
import json
from typing import Optional

from .. import cdp, logs, runmeta
from .. import settings as cfg
from .base_adapter import READ_TEXT_JS, BaseAIAdapter, Capabilities

log = logs.get("qwen")

# chat.qwen.ai (2026+ UI) DOM structure:
#   .qwen-chat-message-assistant                       <- message container
#     .chat-response-message-render-flow               <- answer flow
#       .qwen-chat-thinking-tool-status-card-wraper    <- "Thinking completed" (NOT THE ANSWER)
#       div                                            <- THE ACTUAL ANSWER
#
# Thinking blocks are hidden when the answer is read.

QWEN_INPUT = "textarea.message-input-textarea"


class QwenAdapter(BaseAIAdapter):
    # Measured DOM: a single hidden `#filesUpload` (multiple, empty accept),
    # BUT the site does not notice a file set on it — there is no `change`
    # listener (measured: nothing in the DOM changes after setFileInputFiles).
    # So only in-page event methods are used: with paste the file is uploaded
    # to the Qwen server and shows up in the composer.
    FILE_INPUT = '#filesUpload, input[type="file"]'
    UPLOAD_METHODS = ("paste", "drop")

    # Qwen's composer silently refuses a very large paste and the request then
    # fails with "could not type into the input field". Measured: ~112k characters
    # still go through, ~150k do not. Above this the oldest history is trimmed
    # (the account takes no files, so there is no context.txt fallback here).
    MAX_INPUT_CHARS = 100000

    # Measured (live test):
    #   vision=True     — recognised the image correctly (13 s): "a blue circle on a red background";
    #   files=False     — a document (txt) could not be attached: `setFileInputFiles`,
    #                     `paste` and `drop` all failed (images do upload via
    #                     paste — only images are accepted);
    #   image_gen=True — measured 2026-10-26: the separate Create Image mode
    #                    (Qwen-Image 3.0) produced an image in 56.4 seconds.
    #   image_edit=False — attachment upload succeeded, but the live edit probe
    #                    returned no image in 90 s. Enable only after a complete
    #                    edit has been verified, not merely an accepted upload.
    caps = Capabilities(tool_calling=True, vision=True, files=False, image_gen=True,
                        image_edit=False,
                        temporary_chat=True, delete_chats=True)
    IMAGE_DONE_RELIABLE = True
    _IMAGE_MODE_JS = "!!document.querySelector('textarea[placeholder=\"Describe the image you want to generate.\"]')"

    def __init__(self):
        super().__init__("qwen")

    # Temporary chat (measured on the live page): opened with `?temporary-chat=true`
    # the page sits in a clean chat that is not saved to history; markers —
    # `.temporary-chat-entry[aria-pressed="true"]`, icon `#qwpcicon-temporaryDialogueSelected`,
    # text "Temporary Chat — This chat won't appear in history".
    _send_pref: str = "enter_key"   # send method that worked last (tried first)
    TEMP_URL = "https://chat.qwen.ai/?temporary-chat=true"
    _TEMP_STATE_JS = """(() => {
        const e = document.querySelector('.temporary-chat-entry');
        const u = e && e.querySelector('use');
        const href = (u && (u.getAttribute('xlink:href') || u.getAttribute('href'))) || '';
        return {
            on: !!e && (e.getAttribute('aria-pressed') === 'true' || /Selected/.test(href)),
            hasInput: !!document.querySelector('textarea.message-input-textarea, textarea'),
            msgs: document.querySelectorAll('.qwen-chat-message-assistant').length,
        };
    })()"""

    # How long to wait for that marker ONCE THE PAGE HAS RENDERED (its input is
    # there). The marker is painted with the page or not at all, so past that point
    # waiting buys nothing — that is why this is short.
    #
    # The page itself may take far longer to render: measured 2026-10-25, a Qwen
    # tab under load needed ~20 s after navigation. With one 4-second budget for
    # both, a loading page was declared "not confirmed", the request fell through
    # to the New Chat path on a half-drawn page, found no model menu and failed —
    # while the same tab, fully loaded, carried the marker exactly as expected.
    TEMP_CONFIRM_WAIT = 2.0
    TEMP_PAGE_LOAD_WAIT = 20.0

    # Is the open page a chat a request may reuse? A temporary chat is NOT required:
    # what has to hold is what `_reset_saved_chat` itself guarantees — the composer is
    # there, the chat is EMPTY, and we are not inside a saved chat (`/c/<id>`) whose
    # history belongs to someone else. The two paths below are the ones Qwen serves a
    # brand-new chat on; anything else is not reused.
    _FRESH_STATE_JS = """(() => ({
        hasInput: !!document.querySelector('textarea.message-input-textarea, textarea'),
        imageMode: !!document.querySelector('textarea[placeholder="Describe the image you want to generate."]'),
        msgs: document.querySelectorAll(
            '.qwen-chat-message-assistant, .qwen-chat-message-user, .qwen-chat-message').length,
        newPath: location.pathname === '/' || location.pathname === '/c/guest',
    }))()"""

    # Model selection (measured on the live page): the `.wms-trigger` in the header
    # (ant-dropdown) opens a list where each model name sits in a leaf element
    # inside `.wms-list__name-info`. Variants: Qwen3.7-Plus, Qwen3.8-Max,
    # Qwen3.8-Omni-Flash.
    _CUR_MODEL_JS = "((document.querySelector('.wms-trigger__text') || {}).innerText || '').trim()"
    _TRIGGER_JS = """(() => { const e = document.querySelector('.wms-trigger');
        if (!e) return null; const r = e.getBoundingClientRect();
        return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; })()"""
    _ITEM_JS = """(name => { const el = [...document.querySelectorAll('.wms-list__name-info *, .wms-list__name-info')]
        .find(e => e.children.length === 0 && (e.innerText || '').trim() === name && (e.offsetWidth || e.offsetHeight));
        if (!el) return null; const r = el.getBoundingClientRect();
        return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; })"""

    async def is_fresh_chat(self, ws) -> bool:
        """Can the prewarmed page be used as it is, without opening a new chat?

        This used to demand the temporary-chat marker, and since Qwen stopped setting
        it the answer was always "no": the prewarmed page was thrown away and every
        request paid ~23 s to open a new chat, while gemini and chatgpt — same pool,
        same prewarm — paid 0.1 s.
        """
        st = await cdp.evaluate(ws, self._FRESH_STATE_JS, timeout=8)
        if not isinstance(st, dict):
            return False
        return bool(st.get("hasInput")) and st.get("msgs") == 0 and bool(st.get("newPath")) and not st.get("imageMode")

    def has_variant(self, variant: str) -> bool:
        return variant == "fallback" and bool(cfg.QWEN_FALLBACK_MODEL) \
            and cfg.QWEN_FALLBACK_MODEL != cfg.QWEN_PRIMARY_MODEL

    async def verify_clean_chat(self, ws) -> bool:
        return await self.is_fresh_chat(ws)

    async def select_variant(self, ws, variant: Optional[str]) -> Optional[str]:
        """Selects the requested Qwen model.

        The main model (variant=None) is CHECKED on every request: the choice
        persists on the account, and if not switched back after a fallback the
        following requests would silently run on another model. If the requested
        model cannot be selected — an error (the request is retried another
        way); there is no "silent switch".
        """
        if await cdp.evaluate(ws, self._IMAGE_MODE_JS, timeout=5):
            return "Qwen-Image 3.0"
        if runmeta.get().get("image_requested"):
            raise RuntimeError("qwen: image mode was not confirmed after navigation")
        target = {None: cfg.QWEN_PRIMARY_MODEL, "fallback": cfg.QWEN_FALLBACK_MODEL}.get(variant, variant)
        if not target:
            return None
        # The header (and its model menu) renders after the composer: on a page still
        # drawing itself the menu was not there yet and the request failed at once
        # with "model not selected (current: ?)". Give it a few seconds to appear.
        cur = ""
        for _ in range(20):
            cur = await cdp.evaluate(ws, self._CUR_MODEL_JS, timeout=10) or ""
            if cur:
                break
            await asyncio.sleep(0.4)
        if cur == target:
            return target
        # The list opens with an animation: the first position found is still
        # moving and the click misses (measured: because of this the 3.8 variants
        # and switching back to the main model failed after ~5 s).
        # So it clicks only once two reads give the same position; if nothing is
        # selected the list is closed and it tries once more.
        for _ in range(2):
            pos = await cdp.evaluate(ws, self._TRIGGER_JS, timeout=10)
            if not isinstance(pos, dict):
                break
            await cdp.click_at(ws, pos["x"], pos["y"])
            prev = None
            for _ in range(15):
                await asyncio.sleep(0.25)
                item = await cdp.evaluate(ws, f"({self._ITEM_JS})({json.dumps(target)})", timeout=10)
                if isinstance(item, dict) and item == prev:
                    await cdp.click_at(ws, item["x"], item["y"])
                    break
                prev = item if isinstance(item, dict) else None
            for _ in range(12):
                await asyncio.sleep(0.25)
                cur = await cdp.evaluate(ws, self._CUR_MODEL_JS, timeout=10) or ""
                if cur == target:
                    log.info("qwen: model selected — %s", target)
                    return target
            await cdp.key(ws, "Escape", "Escape", 27)
            await asyncio.sleep(0.3)
        raise RuntimeError(f"qwen: kerakli model tanlanmadi (hozirgisi: {cur or '?'})")

    async def reset_chat(self, ws, timeout: float = 20.0) -> None:
        """A clean chat for every request: a TEMPORARY chat first, otherwise a regular new chat.

        A temporary chat is not saved to history — chats do not pile up and need
        no deleting later (background cleanup is just a fallback). If the mode is
        NOT CONFIRMED by its marker on the page, the regular "New Chat" flow is
        used: two users' requests must never land in the same chat.
        """
        if cfg.QWEN_TEMPORARY_CHAT:
            await self.goto(ws, f"{self.TEMP_URL}&_sf={int(time.time() * 1000)}")
            started = time.monotonic()
            rendered_at = None
            st = None
            while True:
                await asyncio.sleep(0.5)
                elapsed = time.monotonic() - started
                try:
                    st = await cdp.evaluate(ws, self._TEMP_STATE_JS, timeout=5)
                except Exception:
                    st = None
                if isinstance(st, dict) and st.get("hasInput"):
                    if st.get("on") and st.get("msgs") == 0:
                        return
                    rendered_at = rendered_at or time.monotonic()
                    if time.monotonic() - rendered_at >= self.TEMP_CONFIRM_WAIT:
                        break
                elif elapsed >= self.TEMP_PAGE_LOAD_WAIT:
                    break
            log.warning("qwen: temporary chat not confirmed after %.1f s (rendered=%s) — "
                        "opening a regular new chat", time.monotonic() - started, rendered_at is not None)
        await self._reset_saved_chat(ws, timeout)

    async def _reset_saved_chat(self, ws, timeout: float = 20.0) -> None:
        """Reset for Qwen: guarantees every request runs in a SEPARATE chat.

        Why it matters: navigating to `https://chat.qwen.ai/` immediately redirects
        Qwen to the last chat (`/c/<id>`). If no new chat is opened, EVERY user's
        request piles into one conversation and they see each other's context.

        The "New Chat" button is built from several nested divs and the outer
        container has NO click handler — so instead of `element.click()` a real
        CDP mouse event is sent to the centre of the innermost element.
        Proof that a new chat opened: the URL changed OR the message count dropped to zero.
        """
        before = await cdp.evaluate(ws, "location.pathname") or ""

        clicked = await cdp.click_text(ws, ["New Chat", "Новый чат", "Yangi chat"])
        if not clicked:
            # Fallback: DOM click (works on some UI versions)
            await cdp.evaluate(ws, """(() => {
                const items = Array.from(document.querySelectorAll('div, a, button'));
                const hits = items.filter(e => {
                    const t = (e.innerText || '').trim();
                    return t === 'New Chat' || t === 'Новый чат' || t === 'Yangi chat';
                });
                const deepest = hits.filter(e => !hits.some(o => o !== e && e.contains(o)));
                const el = deepest[0] || hits[hits.length - 1];
                if (el) { el.click(); return true; }
                return false;
            })()""")

        state_js = ("(() => ({ path: location.pathname,"
                    " hasInput: !!document.querySelector('textarea.message-input-textarea'),"
                    " msgs: document.querySelectorAll('.qwen-chat-message-assistant').length }))()")

        fresh = False
        for _ in range(int(timeout * 2)):
            await asyncio.sleep(0.5)
            st = await cdp.evaluate(ws, state_js, timeout=10)
            if not isinstance(st, dict):
                continue
            if st.get("hasInput") and (st.get("path") != before or st.get("msgs") == 0):
                fresh = True
                break

        if not fresh:
            # The button did not work — last resort: change the chat URL directly.
            # Qwen opens a new chat on the root URL (not `/c/<id>`).
            log.warning("qwen: 'New Chat' did not work, forcing a new chat via the URL")
            await self.goto(ws, "https://chat.qwen.ai/")
            await self.wait_for_input(ws, timeout=timeout, selector=QWEN_INPUT)
            st = await cdp.evaluate(ws, state_js, timeout=10)
            if isinstance(st, dict) and st.get("msgs", 0) > 0:
                # Still the old chat — do not run the request, otherwise
                # users' contexts would get mixed.
                raise RuntimeError(
                    "qwen: yangi chat ochib bo'lmadi (kontekst aralashishi xavfi)")

        await asyncio.sleep(0.5)
        await self._set_fast_mode(ws)

    async def _set_fast_mode(self, ws) -> None:
        """Switches the answer mode to "Fast".

        Next to the Qwen input there is a `.qwen-thinking-selector`: Auto / Thinking / Fast.
        The default "Auto" often triggers a long "thinking" phase (60+ seconds).
        In "Fast" mode the answer arrives in ~5-10 seconds.
        """
        check = """(() => {
            const sel = document.querySelector('.qwen-thinking-selector');
            if (!sel) return 'missing';
            return (sel.innerText || '').trim();
        })()"""
        try:
            cur = await cdp.evaluate(ws, check, timeout=10)
        except Exception:
            return
        if cur in ("missing", "Fast"):
            return

        # Open the dropdown
        await cdp.evaluate(ws, "(() => { const s = document.querySelector('.qwen-thinking-selector');"
                               " if (!s) return false;"
                               " const t = s.querySelector('.qwen-chat-v2-dropdown-menu-trigger') || s;"
                               " t.click(); return true; })()")
        await asyncio.sleep(0.8)

        # Pick the "Fast" option
        pick = """(() => {
            const items = Array.from(document.querySelectorAll('.qwen-chat-v2-dropdown-menu-item'));
            const fast = items.find(e => /^Fast$/i.test((e.innerText || '').trim()));
            if (fast) { fast.click(); return true; }
            return false;
        })()"""
        await cdp.evaluate(ws, pick)
        await asyncio.sleep(0.5)

    async def reset_chat_for_images(self, ws, timeout: float = 20.0) -> None:
        # Image mode has its own model. A normal Fast prompt just answers in text.
        # Use a verified blank page, never the previous user's saved conversation.
        # Measured: the menu ignores clicks in a background tab (29.6 s failure);
        # bringing the controlled tab forward selects the mode in 8.3 s.
        await cdp.call(ws, "Page.bringToFront")
        # The old composer remains present briefly after location.href changes.
        # Wait for a NEW document, not that stale input on the outgoing page.
        nonce = str(time.time_ns())
        await cdp.evaluate(ws, f"window.__sf_image_reset = {json.dumps(nonce)}")
        await self.goto(ws, "https://chat.qwen.ai/?_sf_img=" + nonce)
        ready = f"""(() => window.__sf_image_reset !== {json.dumps(nonce)}
            && document.readyState !== 'loading'
            && !!document.querySelector('textarea.message-input-textarea')
            && !!document.querySelector('.mode-select-open'))()"""
        if not await self.wait_until(ws, ready, timeout=timeout, interval=0.25):
            raise RuntimeError("qwen: fresh image composer did not load")
        if not await self.is_fresh_chat(ws):
            await self._reset_saved_chat(ws, timeout=timeout)
        if await cdp.evaluate(ws, self._IMAGE_MODE_JS, timeout=5):
            return
        opener = """(() => {
            const e = document.querySelector('.mode-select-open');
            if (!e) return null;
            const r = e.getBoundingClientRect();
            return r.width && r.height ? {x:r.left+r.width/2,y:r.top+r.height/2} : null;
        })()"""
        item = """(() => {
            const e = [...document.querySelectorAll('.mode-select-dropdown-item')]
                .find(e => (e.innerText || '').trim() === 'Create Image' && e.getBoundingClientRect().width);
            if (!e) return null;
            const r = e.getBoundingClientRect();
            return {x:r.left+r.width/2,y:r.top+r.height/2};
        })()"""
        for _ in range(2):
            await cdp.hover_click(ws, opener)
            if await self.wait_until(ws, item, timeout=3):
                await asyncio.sleep(0.3)  # dropdown animation moves the target
                await cdp.hover_click(ws, item)
                if await self.wait_until(ws, self._IMAGE_MODE_JS, timeout=4):
                    return
            await cdp.key(ws, "Escape", "Escape", 27)
        raise RuntimeError("qwen: image mode could not be selected")

    async def read_images(self, ws, timeout: float = 15.0) -> list[str]:
        # The 128px image-generating icon is NOT a generated image. Scope to
        # the final answer; exclude examples, uploads, skeletons and previews.
        js = """(() => {
            const last = [...document.querySelectorAll('.qwen-chat-message-assistant')].at(-1);
            if (!last) return [];
            return [...last.querySelectorAll('img.qwen-image')]
                .filter(i => i.complete && i.naturalWidth >= 256 && i.naturalHeight >= 256
                    && !i.closest('[class*="skeleton"]'))
                .map(i => {
                    const u = new URL(i.currentSrc || i.src);
                    // Keep the resource token, remove only the thumbnail transform.
                    if (u.hostname === 'cdn.qwenlm.ai' && u.pathname.startsWith('/output/'))
                        u.searchParams.delete('x-oss-process');
                    return u.href;
                }).slice(-4);
        })()"""
        result = await cdp.evaluate(ws, js, timeout=timeout)
        return result if isinstance(result, list) else []

    async def generation_done(self, ws) -> bool:
        return bool(await cdp.evaluate(ws, """(() => {
            const last = [...document.querySelectorAll('.qwen-chat-message-assistant')].at(-1);
            return !!last && !last.querySelector('.qwen-media-skeleton')
                && !document.querySelector('button[aria-label="Stop"], .stop-button');
        })()""", timeout=5))

    async def finished_without_image(self, ws) -> bool:
        if not await self.generation_done(ws):
            return False
        return bool(await cdp.evaluate(ws, """(() => {
            const last = [...document.querySelectorAll('.qwen-chat-message-assistant')].at(-1);
            return !!last && !last.querySelector('img.qwen-image') && !!(last.innerText || '').trim();
        })()""", timeout=5))

    async def send_prompt(self, ws, prompt_text: str) -> bool:
        # The text is REPLACED, not appended. `insert_text` inserts at the
        # cursor, so it would stick to old text left in the field (measured:
        # 121 + 39 = 160 characters, two questions merged into one).
        fill = f"""(() => {{
            const el = document.querySelector({QWEN_INPUT!r});
            if (!el) return false;
            el.focus();
            const setter = Object.getOwnPropertyDescriptor(
                Object.getPrototypeOf(el), 'value').set;
            setter.call(el, {prompt_text!r});
            el.dispatchEvent(new Event('input', {{ bubbles: true }}));
            el.dispatchEvent(new Event('change', {{ bubbles: true }}));
            return (el.value || '').length;
        }})()"""
        if not await cdp.evaluate(ws, fill):
            return False
        await asyncio.sleep(0.4)

        # There are several ways to send and which one works depends on the
        # site's current version. So AFTER each method we check whether the text
        # field emptied: if so — sent; if not — move to the next method. The code
        # used to return True after "clicking" while nothing was actually sent
        # and the request silently vanished.
        length_js = f"((document.querySelector({QWEN_INPUT!r}) || {{}}).value || '').length"

        async def was_sent() -> bool:
            # SPEED: previously it waited a fixed 0.8 s after every method; since the first
            # two methods do not work on this page each request lost ~3 s. Now it checks
            # every 0.15 s (at most 1 s).
            for _ in range(7):
                await asyncio.sleep(0.15)
                try:
                    if not await cdp.evaluate(ws, length_js, timeout=8):
                        return True
                except Exception:
                    pass
            return False

        SEND_BUTTON = ('.chat-prompt-send-button, .message-input-right-button-send, '
                 'button.send-button, button[aria-label="Send" i]')

        async def dom_click():
            await cdp.evaluate(ws, f"""(() => {{
                const b = document.querySelector({SEND_BUTTON!r});
                if (!b || b.offsetParent === null) return false;
                b.click();
                return true;
            }})()""")

        async def mouse_click():
            # Vue often ignores a DOM click — use a real mouse click
            await cdp.hover_click(ws, f"""(() => {{
                const b = document.querySelector({SEND_BUTTON!r});
                if (!b || b.offsetParent === null) return null;
                const r = b.getBoundingClientRect();
                return {{ x: Math.round(r.left + r.width / 2),
                          y: Math.round(r.top + r.height / 2) }};
            }})()""")

        async def enter_key():
            await cdp.evaluate(ws, f"""(() => {{
                const ta = document.querySelector({QWEN_INPUT!r});
                if (!ta) return false;
                ta.focus();
                const n = (ta.value || '').length;
                if (ta.setSelectionRange) ta.setSelectionRange(n, n);
                return true;
            }})()""")
            await cdp.key(ws, "Enter")

        async def key_events():
            await cdp.evaluate(ws, f"""(() => {{
                const ta = document.querySelector({QWEN_INPUT!r});
                if (!ta) return false;
                for (const evt of ['keydown', 'keypress', 'keyup']) {{
                    ta.dispatchEvent(new KeyboardEvent(evt, {{
                        key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true
                    }}));
                }}
                return true;
            }})()""")

        methods = {"dom_click": dom_click, "mouse_click": mouse_click,
                   "enter_key": enter_key, "key_events": key_events}
        # SPEED: the method that worked last is tried first (at one point only Enter
        # worked and the two earlier methods cost ~3 s per request).
        order = sorted(methods, key=lambda n: n != self._send_pref)
        for name in order:
            await methods[name]()
            if await was_sent():
                self._send_pref = name
                return True

        log.warning("qwen: could not send the request (all 4 methods failed)")
        return False

    async def read_response(self, ws, timeout: int = 45) -> Optional[str]:
        script = ("""(() => {
            const nodes = document.querySelectorAll('.qwen-chat-message-assistant');
            const last = nodes[nodes.length - 1];
            if (!last) return { answer: '', generating: false };

            // NOTE: the `awaiting-response` class lingers after the answer arrives —
            // generation state is detected via the stop button instead.
            const generating = !!document.querySelector(
                'button[aria-label*="stop" i], [class*="stop-btn" i], [class*="stop-generat" i]'
            );

            const flow = last.querySelector('.chat-response-message-render-flow') || last;

            // Source 1: the ORIGINAL markdown of the answer (from React state).
            // Qwen renders code blocks in a VIRTUAL Monaco editor: the DOM only
            // holds the few lines currently on screen (measured: lines "36..39"
            // of a 40-line snippet), so the full code cannot be read from the DOM.
            // The markdown component's `content` prop holds the whole answer with ``` fences.
            // A/B comparison ("Which response do you prefer? Response 1 / Response 2"):
            // Qwen sometimes produces two answers. To avoid merging them only the
            // FIRST is taken; if they cannot be separated it returns empty (the request is retried).
            const compare = /I prefer this response|Which response do you prefer|Какой ответ вам больше нравится/i
                .test(document.body.innerText || '');
            const fromState = () => {
                const parts = [];
                for (const ans of flow.querySelectorAll('.response-message-content.phase-answer')) {
                    let el = ans;
                    while (el.firstElementChild) el = el.firstElementChild;
                    const k = Object.keys(el).find(x => x.startsWith('__reactFiber$'));
                    let f = k ? el[k] : null;
                    for (let i = 0; f && i < 40; i++, f = f.return) {
                        const c = f.memoizedProps && f.memoizedProps.content;
                        if (typeof c === 'string' && c.trim()) { parts.push(c.trim()); break; }
                        if (f.stateNode === ans) break;
                    }
                }
                const NL = String.fromCharCode(10);
                return compare ? (parts[0] || '') : parts.join(NL + NL);
            };
            let text = '';
            try { text = fromState(); } catch (e) { text = ''; }

            // Source 2 (fallback): the live DOM — reasoning blocks are hidden, code
            // blocks are turned into ``` fences.
            if (!text && !compare) text = __READ__(flow);

            // Leftover marker texts
            const markers = ['Thinking completed', 'Thinking...', 'Thought Process',
                             'Завершено размышление', 'Размышление...'];
            for (const m of markers) {
                if (text.startsWith(m)) text = text.slice(m.length).trim();
            }
            return { answer: text, generating };
        })()""".replace("__READ__", READ_TEXT_JS))

        # Qwen hides the stop button late — even while the generating flag is still set,
        # the answer counts as ready after 4 stable polls.
        return await self.poll_answer(ws, script, timeout=timeout,
                                      stable_needed=2, stable_if_generating=4)

    # --- Chat history cleanup --------------------------------------------------
    #
    # POLICY: "all" — ALL chats on the Qwen account may be deleted.
    # Confirmed by the owner: every chat on this account is disposable (even
    # personal ones). This permission applies ONLY to Qwen — the ChatGPT
    # (registry + 7 days) and DeepSeek (pin) protections stay in place.
    CLEANUP_POLICY = "all"

    # MEASURED (live DOM, nothing deleted — Cancel):
    #   * row: `a.chat-item-drag-link` (NO href — SPA), parent `.chat-item-drag`;
    #     the open chat's row has the `chat-item-drag-active` class;
    #   * the menu button `.chat-item-drag button[aria-label="Chat Menu"]` is
    #     always in the DOM but has zero width until HOVERED (same as DeepSeek);
    #   * menu item: `[role=menuitem].qwen-chat-v2-dropdown-menu-item-danger`
    #     ("Delete"); the menu also has Pin/Archive/Select/Share, etc.;
    #   * confirmation: ant-modal "Delete this chat?" — `.qwen-chat-v2-btn-danger`
    #     ("Delete") and `.qwen-chat-v2-btn-plain` ("Cancel").
    # The old flow failed SILENTLY: it looked the button up by innerText (it has
    # none, only aria-label) and never scrolled the last row of the list into view.
    #
    # MEASURED IN PRODUCTION: HOVER IS UNRELIABLE. The Chrome window is small
    # (viewport 952x393), the list re-renders live and after a CDP `mouseMoved`
    # the button sometimes gets a width and sometimes does not. But the button
    # is in the DOM and `element.click()` opens the menu EVERY TIME (even at zero
    # width). So all three clicks are DOM clicks. The target and the result are
    # checked by the `id` in the chat's React props — the row index shifts when
    # the list reloads.

    # The row's chat id (React fiber props; there is no href in the DOM)
    _ID_OF_JS = """const idOf = (a) => { const k = Object.keys(a).find(k => k.startsWith('__reactFiber'));
        let f = k && a[k];
        for (let d = 0; f && d < 10; d++, f = f.return) {
            const p = f.memoizedProps;
            if (p && typeof p.id === 'string' && 'title' in p) return p.id;
        }
        return null; };"""

    _ROWS_JS = """(() => Array.from(document.querySelectorAll('.chat-item-drag-link')).map(a => ({
        title: (a.innerText || '').trim().slice(0, 80),
        active: /chat-item-drag-active/.test(String(a.className)) })))()"""

    # Scrolls the list to the end (to load more chats) and returns the count
    _SCROLL_JS = """(() => {
        const a = document.querySelector('.chat-item-drag-link');
        let el = a, cont = null;
        while (el) { if (el.scrollHeight > el.clientHeight + 20 && el.clientHeight > 100) { cont = el; break; }
                     el = el.parentElement; }
        if (cont) cont.scrollTop = cont.scrollHeight;
        return document.querySelectorAll('.chat-item-drag-link').length;
    })()"""

    @classmethod
    def _row_id_js(cls, i: int) -> str:
        return """(() => { %s const a = document.querySelectorAll('.chat-item-drag-link')[%d];
            return a ? idOf(a) : null; })()""" % (cls._ID_OF_JS, i)

    @classmethod
    def _has_id_js(cls, chat_id: str) -> str:
        return """(() => { %s const want = %s;
            return Array.from(document.querySelectorAll('.chat-item-drag-link')).some(a => idOf(a) === want); })()""" % (
            cls._ID_OF_JS, json.dumps(chat_id))

    @classmethod
    def _open_menu_js(cls, chat_id: str) -> str:
        """Opens the menu. Returns a Promise — call with `await_promise=True`.

        MEASURED (production): on 16 of 60 rows the "Chat Menu" button is not in
        the DOM AT ALL — React only mounts it after `mouseover`. For such a row a
        synthetic hover event is dispatched and the button is awaited.
        """
        return """(async () => { %s const want = %s;
            const find = () => Array.from(document.querySelectorAll('.chat-item-drag-link')).find(e => idOf(e) === want);
            let a = find(); if (!a) return false;
            a.scrollIntoView({ block: 'center' });
            const btnOf = (a) => (a.closest('.chat-item-drag') || a.parentElement).querySelector('button[aria-label="Chat Menu"]');
            let b = btnOf(a);
            for (let k = 0; !b && k < 3; k++) {
                for (const el of [a.closest('.chat-item-drag') || a.parentElement, a]) {
                    el.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
                    el.dispatchEvent(new MouseEvent('mouseenter', { bubbles: false }));
                }
                await new Promise(r => setTimeout(r, 400));
                a = find(); if (!a) return false;
                b = btnOf(a);
            }
            if (!b) return false; b.click(); return true; })()""" % (cls._ID_OF_JS, json.dumps(chat_id))

    _MENU_DELETE_JS = """(() => {
        const it = Array.from(document.querySelectorAll('[role=menuitem].qwen-chat-v2-dropdown-menu-item-danger'))
            .find(e => e.offsetParent !== null && /^delete$/i.test((e.innerText || '').trim()));
        if (!it) return false; it.click(); return true; })()"""

    _CONFIRM_JS = """(() => {
        const b = Array.from(document.querySelectorAll('.ant-modal .qwen-chat-v2-btn-danger'))
            .find(e => e.offsetParent !== null && /^delete$/i.test((e.innerText || '').trim()));
        if (!b) return false; b.click(); return true; })()"""

    _CANCEL_JS = """(() => {
        const b = Array.from(document.querySelectorAll('.ant-modal .qwen-chat-v2-btn-plain'))
            .find(e => e.offsetParent !== null);
        if (b) { b.click(); return true; } return false; })()"""

    async def _count(self, ws) -> int:
        n = await cdp.evaluate(ws, "document.querySelectorAll('.chat-item-drag-link').length", timeout=10)
        return int(n or 0)

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

    async def plan_old_chats(self, ws, keep: int = 0) -> dict:
        """DRY RUN: counts what would be deleted. The top of the list holds the newest chats."""
        rows, complete = await self._load_rows(ws)
        keep = max(0, keep)
        return {"delete": rows[keep:], "protect": rows[:keep], "skip": [],
                "ledger_size": None, "rows": len(rows), "complete": complete}

    async def _close_overlays(self, ws) -> None:
        try:
            if not await cdp.evaluate(ws, self._CANCEL_JS, timeout=5):
                await cdp.key(ws, "Escape", "Escape", 27)
        except Exception:
            pass
        await asyncio.sleep(0.5)

    async def _delete_index(self, ws, i: int) -> bool:
        """Deletes the chat in row `i`. Success means that `id` disappeared from the list."""
        chat_id = await cdp.evaluate(ws, self._row_id_js(i))
        if not isinstance(chat_id, str) or not chat_id:
            log.warning("qwen: delete — could not read the id of row %d", i)
            return False
        if not await cdp.evaluate(ws, self._open_menu_js(chat_id), await_promise=True):
            log.warning("qwen: delete — menu button not found id=%s", chat_id[:8])
            return False
        await asyncio.sleep(0.8)
        if not await cdp.evaluate(ws, self._MENU_DELETE_JS):
            log.warning("qwen: delete — no 'Delete' in the menu id=%s", chat_id[:8])
            await self._close_overlays(ws)
            return False
        await asyncio.sleep(0.8)
        if not await cdp.evaluate(ws, self._CONFIRM_JS):
            log.warning("qwen: delete — confirmation dialog did not appear id=%s", chat_id[:8])
            await self._close_overlays(ws)
            return False
        # The provider's server may be slow — wait up to 10 seconds
        for _ in range(20):
            await asyncio.sleep(0.5)
            if not await cdp.evaluate(ws, self._has_id_js(chat_id)):
                return True
        log.warning("qwen: delete — still in the list after 10 s id=%s", chat_id[:8])
        await self._close_overlays(ws)
        return False

    async def delete_old_chats(self, ws, keep: int = 0, max_per_run: int = 40) -> int:
        """Deletes chats starting from the END of the list (the oldest); the top `keep` stay.

        Policy "all" (see above). At most `max_per_run` per call — so the worker
        lock is not held for long. Stops after three consecutive failures
        (the UI has changed).
        """
        keep = max(0, keep)
        deleted, failures = 0, 0
        await self._load_rows(ws)
        while deleted < max_per_run:
            n = await self._count(ws)
            if n <= keep:
                # None left on screen — are there more not yet loaded below?
                n = int(await cdp.evaluate(ws, self._SCROLL_JS, timeout=10) or 0)
                await asyncio.sleep(1.0)
                if await self._count(ws) <= keep:
                    break
                continue
            try:
                ok = await self._delete_index(ws, n - 1)
            except Exception as e:
                log.warning("qwen: error while deleting a chat: %s", e)
                await self._close_overlays(ws)
                ok = False
            if ok:
                deleted, failures = deleted + 1, 0
            else:
                failures += 1
                if failures >= 3:
                    log.warning("qwen: chat was not deleted (the UI may have changed) — stopping")
                    break
            await asyncio.sleep(0.3)
        if deleted:
            log.info("qwen: deleted %d chat(s)", deleted)
        return deleted
