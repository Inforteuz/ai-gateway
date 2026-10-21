"""Logged-out (anonymous) chatgpt.com — a free worker for text.

WHY A SEPARATE ADAPTER: the anonymous UI is COMPLETELY different from the
logged-in one. Logged in, the input is `div.ProseMirror`; here it is a plain
`textarea`; messages have no `data-message-author-role` attribute and class
names are obfuscated (`xjbqb8w xbs2zpe ...`), so they cannot be relied on.
The answer is therefore extracted by TEXT MARKERS.

MEASURED LIMITATION: image generation is CLOSED in anonymous mode — the page
replies outright with "creation is only available when you're logged in".
There is no file upload either. So this adapter is TEXT only.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from .. import cdp, logs
from .base_adapter import BaseAIAdapter, Capabilities

log = logs.get("chatgptanon")

ANON_INPUT = "textarea"
SEND_BTN = 'button[aria-label="Send message" i]'


class ChatGPTAnonAdapter(BaseAIAdapter):
    # Only text works in anonymous mode — everything else measured as closed.
    caps = Capabilities()

    def __init__(self):
        super().__init__("chatgptanon")

    # --- Bringing the page to a clean state -------------------------------

    async def _dismiss_cookie_banner(self, ws) -> None:
        """Closes the cookie banner with the most privacy-preserving option.

        After cookies are cleared the banner comes back EVERY TIME, covers the
        page and gets in the way of sending — so it is an integral part of the
        reset. "Reject non-essential" is chosen: essential cookies only.
        """
        js = """(() => {
            const b = Array.from(document.querySelectorAll('button')).find(e =>
                /reject non-essential|reject all/i.test((e.innerText || '').trim()));
            if (b) { b.click(); return true; }
            return false;
        })()"""
        try:
            if await cdp.evaluate(ws, js, timeout=8):
                await asyncio.sleep(0.6)
        except Exception:
            pass

    async def reset_limits(self, ws) -> None:
        """Clears cookies and stored data, then reloads the page.

        Used when the anonymous session hits its message limit or a login
        prompt blocks the page: after clearing, the site sees us as a new
        guest. (Measured: cookies drop from 8 to 3 and the login prompt
        disappears.)
        """
        try:
            await cdp.call(ws, "Network.enable", timeout=8)
            await cdp.call(ws, "Network.clearBrowserCookies", timeout=8)
            await cdp.call(ws, "Storage.clearDataForOrigin",
                           {"origin": "https://chatgpt.com", "storageTypes": "all"},
                           timeout=8)
        except Exception as e:
            log.debug("partial clear: %s", e)
        await self.goto(ws, "https://chatgpt.com/")
        await self.wait_for_input(ws, timeout=20, selector=ANON_INPUT)
        await self._dismiss_cookie_banner(ws)

    async def reset_chat(self, ws, timeout: float = 15.0) -> None:
        """A clean page before every request.

        The anonymous session keeps no chat history, so the most reliable way
        is to reload the page. If a login prompt shows up or the input field is
        not found, fall back to a full clear.
        """
        await self.goto(ws, "https://chatgpt.com/")
        ready = await self.wait_for_input(ws, timeout=timeout, selector=ANON_INPUT)
        await self._dismiss_cookie_banner(ws)

        blocked = False
        try:
            blocked = bool(await cdp.evaluate(ws, """(() => {
                const t = document.body.innerText || '';
                return /only available when you.re logged in|too many requests|rate limit/i.test(t);
            })()""", timeout=8))
        except Exception:
            pass

        if not ready or blocked:
            log.info("chatgptanon: page is blocked, clearing the session")
            await self.reset_limits(ws)

    # --- Sending the request ----------------------------------------------

    async def send_prompt(self, ws, prompt_text: str) -> bool:
        fill = f"""(() => {{
            const vis = e => {{ const r = e.getBoundingClientRect();
                return r.width > 50 && r.height > 8 && e.offsetParent !== null; }};
            const ta = Array.from(document.querySelectorAll('textarea')).filter(vis).pop();
            if (!ta) return false;
            ta.focus();
            const setter = Object.getOwnPropertyDescriptor(
                Object.getPrototypeOf(ta), 'value').set;
            setter.call(ta, {prompt_text!r});
            ta.dispatchEvent(new Event('input', {{ bubbles: true }}));
            return true;
        }})()"""
        if not await cdp.evaluate(ws, fill):
            return False
        # The "Send message" button appears only AFTER text is entered
        await asyncio.sleep(0.7)

        async def was_sent() -> bool:
            await asyncio.sleep(0.8)
            try:
                return not await cdp.evaluate(ws, """(() => {
                    const ta = Array.from(document.querySelectorAll('textarea'))
                        .filter(e => e.offsetParent !== null).pop();
                    return ta ? (ta.value || '').length : 0;
                })()""", timeout=8)
            except Exception:
                return False

        await cdp.evaluate(ws, f"""(() => {{
            const b = document.querySelector({SEND_BTN!r});
            if (!b || b.disabled) return false;
            b.click();
            return true;
        }})()""")
        if await was_sent():
            return True

        await cdp.hover_click(ws, f"""(() => {{
            const b = document.querySelector({SEND_BTN!r});
            if (!b) return null;
            const r = b.getBoundingClientRect();
            return {{ x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2) }};
        }})()""")
        return await was_sent()

    # --- Reading the answer -----------------------------------------------

    async def read_response(self, ws, timeout: int = 45) -> Optional[str]:
        # The anonymous UI has no stable selector (obfuscated class names, no
        # `data-message-author-role`), so the answer is split out by text
        # markers. The site renders the conversation like this:
        #     You said:
        #     <question>
        #     ChatGPT
        #     <answer>
        #
        # Two traps (both observed in measurements):
        #   1) the word "ChatGPT" also appears inside OUR system prompt
        #      (in the list of forbidden names). A naive search would find it
        #      and return our own prompt instead of the answer. So only a
        #      "ChatGPT" on a LINE OF ITS OWN is accepted as the marker.
        #   2) the question and the answer sit in SIBLING elements, so the
        #      element containing "You said:" only covers the question —
        #      the text is read at page level.
        script = """(() => {
            const NL = String.fromCharCode(10);
            const full = document.body.innerText || '';
            const start = full.lastIndexOf('You said:');
            if (start < 0) return { answer: '', generating: true };

            const lines = full.slice(start).split(NL);
            let marker = -1;
            for (let i = lines.length - 1; i >= 0; i--) {
                if (lines[i].trim() === 'ChatGPT') { marker = i; break; }
            }
            if (marker < 0) return { answer: '', generating: true };

            const TAIL = ['ChatGPT is AI and can make mistakes',
                          'ChatGPT can make mistakes',
                          'Log in', 'Sign up for free', 'Continue with Google'];
            const answerLines = [];
            for (const q of lines.slice(marker + 1)) {
                if (TAIL.some(t => q.trim().startsWith(t))) break;
                answerLines.push(q);
            }
            const generating = !!document.querySelector(
                'button[aria-label*="Stop" i], [data-testid*="stop"]');
            return { answer: answerLines.join(NL).trim(), generating };
        })()"""
        return await self.poll_answer(ws, script, timeout=timeout, initial_delay=1.0)
