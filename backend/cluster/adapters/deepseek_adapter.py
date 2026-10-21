import asyncio
import json
from typing import Optional

from .. import cdp
from .base_adapter import READ_TEXT_JS, BaseAIAdapter, Capabilities

# chat.deepseek.com (2026+ UI):
#   - Input: textarea[placeholder*="DeepSeek"]
#   - Send button: usually an icon button — dynamic class, sending via Enter is more reliable
#   - Response: .ds-markdown (user and assistant messages)


class DeepSeekAdapter(BaseAIAdapter):
    # Measured accept: .pdf,.png,.jpg,.jpeg,.svg and many text formats.
    # DeepSeek reads images mostly for OCR — it does not generate images.
    # Measured: `setFileInputFiles` works — a preview appears in the composer
    # within 1 second (class names are hashed, so we rely on the preview
    # SIGNAL rather than a selector — `upload_state`).
    FILE_INPUT = 'input[type="file"]'

    # Measured: follows the tool protocol reliably (a multi-step agent loop passed).
    # Live test: vision 72 s, document reading 11 s. Does not draw images.
    # `delete_chats=True` — the sidebar delete flow was measured and tested
    # (`delete_old_chats`). PINNED chats are never deleted — that is how the
    # account owner's personal conversations are protected.
    caps = Capabilities(tool_calling=True, vision=True, files=True, image_gen=False,
                        temporary_chat=False, delete_chats=True)
    # Cleanup policy: unpinned chats (except the newest `keep`).
    # The admin panel dry run shows exactly this rule — it is never loosened.
    CLEANUP_POLICY = "pin"

    def __init__(self):
        super().__init__("deepseek")

    async def send_prompt(self, ws, prompt_text: str) -> bool:
        focus = """(() => {
            const el = document.querySelector('textarea[placeholder*="DeepSeek" i]')
                     || document.querySelector('textarea');
            if (!el) return false;
            el.focus();
            return true;
        })()"""
        if not await cdp.evaluate(ws, focus):
            return False

        await cdp.insert_text(ws, prompt_text)
        await asyncio.sleep(0.4)

        # Measured DOM: the send button is a `div.ds-button--primary` with NO
        # `aria-label` and no "send" in its class (classes are hashed).
        # So it is picked by POSITION rather than by name: the enabled primary
        # icon button to the right of the textarea.
        click = """(() => {
            const vis = e => { const r = e.getBoundingClientRect();
                return r.width > 8 && r.height > 8 && e.offsetParent !== null; };
            const ta = document.querySelector('textarea');
            if (!ta) return false;
            const tr = ta.getBoundingClientRect();
            const cands = Array.from(document.querySelectorAll(
                '.ds-button--primary, div[role="button"], button'
            )).filter(b => {
                if (!vis(b)) return false;
                if (b.disabled || b.getAttribute('aria-disabled') === 'true') return false;
                // Skip buttons WITH TEXT: the composer also has "DeepThink" and
                // "Search" buttons on the right, and they were the ones clicked
                // while the send button was disabled (measured).
                if ((b.innerText || '').trim()) return false;
                const r = b.getBoundingClientRect();
                // On the textarea's row or below it, in the right half
                return r.top > tr.top - 20 && r.left > tr.left + tr.width * 0.4;
            });
            // The primary button if there is one, otherwise the rightmost
            const primary = cands.find(b => String(b.className).includes('primary'));
            const btn = primary || cands.sort(
                (a, b) => b.getBoundingClientRect().left - a.getBoundingClientRect().left)[0];
            if (!btn) return false;
            btn.click();
            return true;
        })()"""
        # Send with CONFIRMATION: while a file uploads the button is disabled
        # and a click does nothing — so after every attempt we check that the
        # text field emptied and retry if needed.
        for attempt in range(3):
            if await cdp.evaluate(ws, click):
                if await self.wait_sent(ws, timeout=6.0):
                    return True
            await self._focus_end(ws)
            await cdp.key(ws, "Enter")
            if await self.wait_sent(ws, timeout=6.0):
                return True
            await asyncio.sleep(2.0)
        return False

    async def _focus_end(self, ws) -> None:
        # Fallback: Enter. IMPORTANT — first RETURN focus to the textarea.
        # After a file is attached focus moves to `body` and an Enter without
        # focus goes nowhere (measured: both text and image were entered but
        # the request was never sent).
        await cdp.evaluate(ws, """(() => {
            const ta = document.querySelector('textarea');
            if (!ta) return false;
            ta.focus();
            const n = (ta.value || '').length;
            if (ta.setSelectionRange) ta.setSelectionRange(n, n);
            return document.activeElement === ta;
        })()""")
        await asyncio.sleep(0.2)

    async def is_fresh_chat(self, ws) -> bool:
        ok = await cdp.evaluate(ws, """(() => !/\\/s\\//.test(location.pathname)
            && document.querySelectorAll('.ds-markdown, .ds-message, [class*="message" i] .ds-markdown').length === 0
            && !!document.querySelector('textarea'))()""", timeout=8)
        return ok is True

    async def read_response(self, ws, timeout: int = 45) -> Optional[str]:
        # Take the last .ds-markdown (the answer). User messages live in a separate container.
        # Text is read with READ_TEXT_JS so the code banner (language + "Copy" +
        # "Download") does not leak into the answer (previously every answer with
        # code started with "yaml\nCopy\nDownload").
        script = """(() => {
            const READ = __READ__;
            const generating = !!document.querySelector('[class*="stop" i], svg[aria-label*="stop" i]');
            const nodes = document.querySelectorAll('.ds-markdown');
            const last = nodes[nodes.length - 1];
            let answer = '';
            if (last) {
                answer = READ(last);
                // Strip the "Thinking..." prefix
                for (const m of ['Thinking...', 'Thinking', 'Thought for']) {
                    if (answer.startsWith(m)) {
                        const i = answer.indexOf('\\n');
                        if (i > 0) answer = answer.slice(i + 1).trim();
                    }
                }
            }
            return { generating, answer };
        })()""".replace("__READ__", READ_TEXT_JS)
        return await self.poll_answer(ws, script, timeout=timeout)

    # --- Chat history cleanup -------------------------------------------------
    #
    # All measured on the LIVE page (no guesses):
    #   * The sidebar is COLLAPSED by default: `a[href*="/a/chat/s/"]` is in
    #     the DOM but `getBoundingClientRect()` returns zero. So the first icon
    #     button at the top left is clicked first.
    #   * The row title is the second child of `a`; the chat ID ends the `href`.
    #     Titles REPEAT (the same title 4 times), so unlike the ChatGPT adapter
    #     the target is chosen by ID.
    #   * Every row's "..." button is always in the DOM but has zero width
    #     until HOVERED. The mouse must FIRST move away and then onto the row —
    #     sending `mouseMoved` to the same point twice makes the browser skip
    #     the new `mouseenter` and the button never opens.
    #   * Menu: `.ds-dropdown-menu-option` (Rename / Pin / Share /
    #     Multi-select / Delete).
    #   * Confirmation: "Cancel" and "Delete chat" inside `.ds-modal`
    #     (`ds-button--error`). NOTE — Escape does NOT close this dialog; a
    #     modal left open traps focus and blocks later hovers. "Cancel" is
    #     clicked to dismiss it.
    #   * The list loads lazily: scrolling down grew it from 101 to 124.

    # Chat list: each item is {id, title, group, protected}.
    #
    # `protected` — PINNED chats (and any not under a "date group").
    # MEASURED structure:
    #   * Date group: the FIRST child inside the container is the header ("Today",
    #     "Yesterday", "2025-03"), the rest are `<a>` rows.
    #   * "Pinned" group: rows sit ONE LEVEL DEEPER in a separate container;
    #     that container has NO header.
    # Class names are hashed (`_3098d02`, `_62cad32`) and change from release
    # to release, so we do not rely on them.
    #
    # The rule DELIBERATELY requires "positive confirmation": a chat is only
    # deleted when its group header was FOUND and is not "pin". If the UI
    # changes, cleanup STOPS SILENTLY (the watchdog returns 0) — far better
    # than deleting the owner's personal conversation.
    _LIST_JS = """(() => {
        const links = Array.from(document.querySelectorAll('a[href*="/a/chat/s/"]'));
        const headerOf = (a) => {
            const box = a.parentElement;
            if (!box) return null;
            for (const c of box.children) {
                if (c.tagName === 'A') continue;
                if (c.querySelector('a[href*="/a/chat/s/"]')) continue;
                const t = (c.innerText || '').trim();
                if (t && t.length < 24) return t;
            }
            return null;
        };
        return links.map(a => {
            const g = headerOf(a);
            return {
                id: (a.getAttribute('href') || '').split('/').pop(),
                title: ((a.children[1] || {}).innerText || '').trim(),
                group: g,
                protected: !g || /pin/i.test(g),
            };
        });
    })()"""

    def _row_js(self, chat_id: str) -> str:
        """Scrolls the target row into view and returns its coordinates."""
        return """(() => {
            const a = Array.from(document.querySelectorAll('a[href*="/a/chat/s/"]'))
                .find(e => (e.getAttribute('href') || '').endsWith(%s));
            if (!a) return null;
            a.scrollIntoView({ block: 'center' });
            const r = a.getBoundingClientRect();
            const d = a.querySelector('[role="button"]');
            const dr = d ? d.getBoundingClientRect() : null;
            return {
                rowX: Math.round(r.left + 60), rowY: Math.round(r.top + r.height / 2),
                dotsX: dr && dr.width ? Math.round(dr.left + dr.width / 2) : null,
                dotsY: dr && dr.width ? Math.round(dr.top + dr.height / 2) : null,
            };
        })()""" % json.dumps(chat_id)

    async def _mouse_to(self, ws, x: float, y: float) -> None:
        await cdp.call(ws, "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})

    async def _close_modal(self, ws) -> None:
        """Closes a lingering confirmation dialog with "Cancel".

        Escape does not work (measured) — an unclosed modal traps focus and
        every later hover/click is wasted.
        """
        pos = await cdp.evaluate(ws, """(() => {
            const b = Array.from(document.querySelectorAll('.ds-modal [role="button"], .ds-modal .ds-button'))
                .filter(e => e.offsetParent !== null)
                .find(e => /^(cancel|bekor)/i.test((e.innerText || '').trim()));
            if (!b) return null;
            const r = b.getBoundingClientRect();
            return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) };
        })()""")
        if isinstance(pos, dict):
            await cdp.click_at(ws, pos["x"], pos["y"])
            await asyncio.sleep(0.8)

    # In "Multi-select" mode rows turn into `div[role=button]` instead of
    # `<a href>` — the chat list DISAPPEARS for our selector and deletion
    # silently stops working. The mode persists until closed on the page
    # (even across reloads), so it is force-closed before every cleanup. The
    # close button is the only `<button>` in the "N chat selected" header.
    _MULTI_CLOSE_JS = """(() => {
        const hits = Array.from(document.querySelectorAll('div'))
            .filter(e => e.offsetParent !== null
                         && /chats? selected/i.test((e.innerText || '').trim())
                         && e.querySelector(':scope > button'));
        if (!hits.length) return null;
        const b = hits[hits.length - 1].querySelector(':scope > button');
        const r = b.getBoundingClientRect();
        return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) };
    })()"""

    async def _exit_multi_select(self, ws) -> None:
        for _ in range(2):
            pos = await cdp.evaluate(ws, self._MULTI_CLOSE_JS)
            if not isinstance(pos, dict):
                return
            await cdp.click_at(ws, pos["x"], pos["y"])
            await asyncio.sleep(1.2)

    async def open_sidebar(self, ws) -> bool:
        """Opens the sidebar. Leaves it alone if already open.

        Openness is judged by the links' REAL size: when collapsed the `a`
        elements either vanish entirely or stay in the DOM with a zero rect
        (which one depends on whether the panel was opened in this session).
        """
        await self._exit_multi_select(ws)
        visible = """(() => Array.from(document.querySelectorAll('a[href*="/a/chat/s/"]'))
            .some(a => a.getBoundingClientRect().width > 10))()"""
        if await cdp.evaluate(ws, visible):
            return True
        # Row of icon buttons at the top left: the leftmost is the panel toggle.
        # No `aria-label` and no text (classes are hashed), so unlike the ChatGPT
        # adapter it is picked by POSITION rather than by name.
        pos = await cdp.evaluate(ws, """(() => {
            const cands = Array.from(document.querySelectorAll('[role="button"]'))
                .map(b => ({ b, r: b.getBoundingClientRect() }))
                .filter(o => o.r.width > 8 && o.r.top < 60 && o.r.left < 300
                             && !(o.b.innerText || '').trim())
                .sort((a, b) => a.r.left - b.r.left);
            if (!cands.length) return null;
            const r = cands[0].r;
            return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) };
        })()""")
        if not isinstance(pos, dict):
            return False
        await cdp.click_at(ws, pos["x"], pos["y"])
        # The list is not drawn instantly — with 120+ rows it can take several
        # seconds.
        for _ in range(16):
            await asyncio.sleep(0.5)
            if await cdp.evaluate(ws, visible):
                return True
        return False

    async def list_chats(self, ws, scroll: bool = True) -> list[dict]:
        """All chats in the sidebar. `scroll=True` loads the lazy list to the end."""
        if not await self.open_sidebar(ws):
            return []
        if scroll:
            prev = -1
            for _ in range(15):
                n = await cdp.evaluate(ws, """(() => {
                    const links = document.querySelectorAll('a[href*="/a/chat/s/"]');
                    if (!links.length) return 0;
                    links[links.length - 1].scrollIntoView({ block: 'end' });
                    return links.length;
                })()""")
                if n == prev:
                    break
                prev = n
                await asyncio.sleep(1.0)
        return [c for c in (await cdp.evaluate(ws, self._LIST_JS) or []) if c.get("id")]

    async def _delete_one(self, ws, chat_id: str) -> bool:
        """Deletes one chat. The result is VERIFIED in the DOM, never assumed."""
        # 1) Move the mouse away from the row — otherwise if the next
        #    `mouseMoved` lands on the same point the hover does not fire again.
        await self._mouse_to(ws, 600, 120)
        await asyncio.sleep(0.3)
        # 2) Scroll the row into view (re-read after the scroll settles)
        await cdp.evaluate(ws, self._row_js(chat_id))
        await asyncio.sleep(0.6)
        row = await cdp.evaluate(ws, self._row_js(chat_id))
        if not isinstance(row, dict):
            return False
        # 3) Hover over the row — that is when the "..." button expands
        await self._mouse_to(ws, row["rowX"], row["rowY"])
        await asyncio.sleep(0.5)
        row = await cdp.evaluate(ws, self._row_js(chat_id))
        if not isinstance(row, dict) or not row.get("dotsX"):
            return False
        await cdp.click_at(ws, row["dotsX"], row["dotsY"])
        await asyncio.sleep(1.0)
        # 4) "Delete" from the menu
        item = await cdp.evaluate(ws, """(() => {
            const e = Array.from(document.querySelectorAll('.ds-dropdown-menu-option'))
                .filter(o => o.offsetParent !== null)
                .find(o => /^delete$/i.test((o.innerText || '').trim()));
            if (!e) return null;
            const r = e.getBoundingClientRect();
            return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) };
        })()""")
        if not isinstance(item, dict):
            await cdp.key(ws, "Escape", "Escape", 27)   # Escape is enough for the menu
            return False
        await cdp.click_at(ws, item["x"], item["y"])
        await asyncio.sleep(1.2)
        # 5) The red "Delete chat" in the confirmation dialog
        ok = await cdp.evaluate(ws, """(() => {
            const b = Array.from(document.querySelectorAll('.ds-modal [role="button"], .ds-modal .ds-button'))
                .filter(e => e.offsetParent !== null)
                .find(e => /error/.test(String(e.className))
                           || /^delete/i.test((e.innerText || '').trim()));
            if (!b) return null;
            const r = b.getBoundingClientRect();
            return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) };
        })()""")
        if not isinstance(ok, dict):
            await self._close_modal(ws)
            return False
        await cdp.click_at(ws, ok["x"], ok["y"])
        await asyncio.sleep(1.8)
        # 6) Is it really gone?
        if await cdp.evaluate(ws, self._row_js(chat_id)) is None:
            return True
        await self._close_modal(ws)
        return False

    async def plan_old_chats(self, ws, keep: int = 0) -> dict:
        """DRY RUN: counts what `delete_old_chats` would delete (at most 40 per run)."""
        chats = await self.list_chats(ws)
        unprotected = [c for c in chats if not c.get("protected")]
        targets = unprotected[max(0, keep):]
        return {"delete": targets[:40], "protect": [c for c in chats if c.get("protected")] + unprotected[:max(0, keep)],
                "skip": [], "later": max(0, len(targets) - 40), "ledger_size": None,
                "rows": len(chats), "complete": True}

    async def delete_old_chats(self, ws, keep: int = 0) -> int:
        """Deletes old chats, keeping the newest `keep`.

        PINNED chats are NEVER deleted and do not count towards `keep`. This is
        deliberate: if the account owner pins a personal conversation, automatic
        cleanup leaves it alone. The owner's personal conversations on this
        account were found and protected exactly this way.

        At most 40 chats are deleted per call — so the watchdog does not hold
        the worker lock for long. The next run picks up the rest.
        """
        chats = await self.list_chats(ws)
        if not chats:
            return 0
        to_delete = [c for c in chats if not c.get("protected")][max(0, keep):]
        deleted = 0
        skipped: set = set()
        for c in to_delete[:40]:
            if c["id"] in skipped:
                continue
            try:
                if await self._delete_one(ws, c["id"]):
                    deleted += 1
                else:
                    skipped.add(c["id"])
            except Exception:
                await self._close_modal(ws)
                break
            await asyncio.sleep(0.4)
        return deleted
