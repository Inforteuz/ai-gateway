import asyncio
from typing import Optional

from .. import cdp
from .base_adapter import BaseAIAdapter, Capabilities

# grok.com (measured):
#   input    : div.ProseMirror (tiptap), placeholder "Ask Grok anything"
#   send     : button[data-testid="chat-submit"] (aria-label="Submit")
#   attach   : button[data-testid="attach-button"] + input[type=file]
#   mode     : aria-label="Model: Fast" — fast mode is already on
#
# NOTE: the free account has a capacity limit. When busy, Grok replies with
# "Grok is under heavy usage right now" instead of an answer — this is
# detected via `PROVIDER_ERROR_KEYWORDS` and triggers a short cooldown.

GROK_INPUT = "div.ProseMirror, .tiptap"
GROK_SUBMIT = 'button[data-testid="chat-submit"], button[aria-label="Submit" i]'


class GrokAdapter(BaseAIAdapter):
    FILE_INPUT = 'input[type="file"]'

    # There is a file attach button, but image understanding and image
    # generation have not been measured — unverified capabilities stay off.
    #
    # `delete_chats` must NEVER be enabled here. This account holds the
    # owner's PERSONAL conversations, and automatic cleanup works on a
    # "delete everything except the newest N" basis — enabling it would
    # wipe the personal chats.
    caps = Capabilities()

    def __init__(self):
        super().__init__("grok")

    async def reset_chat(self, ws, timeout: float = 12.0) -> None:
        """New chat — every request goes into its own conversation."""
        await self.goto(ws, "https://grok.com/")
        await self.wait_for_input(ws, timeout=timeout, selector=GROK_INPUT)

    async def send_prompt(self, ws, prompt_text: str) -> bool:
        # Text goes into ProseMirror via `execCommand`: it produces real
        # input events and the editor updates its own state.
        # `innerHTML` or CDP insertText do not work here.
        fill = f"""(() => {{
            const vis = e => {{ const r = e.getBoundingClientRect();
                return r.width > 50 && r.height > 8 && e.offsetParent !== null; }};
            const el = Array.from(document.querySelectorAll({GROK_INPUT!r}))
                .filter(vis).pop();
            if (!el) return false;
            el.focus();
            const range = document.createRange();
            range.selectNodeContents(el);
            const sel = window.getSelection();
            sel.removeAllRanges(); sel.addRange(range);
            return document.execCommand('insertText', false, {prompt_text!r});
        }})()"""
        if not await cdp.evaluate(ws, fill):
            return False
        # The submit button becomes enabled after text is entered
        await asyncio.sleep(0.6)

        async def was_sent() -> bool:
            await asyncio.sleep(0.9)
            try:
                return not await cdp.evaluate(ws, f"""(() => {{
                    const vis = e => e.offsetParent !== null;
                    const el = Array.from(document.querySelectorAll({GROK_INPUT!r}))
                        .filter(vis).pop();
                    return el ? (el.innerText || '').trim().length : 0;
                }})()""", timeout=8)
            except Exception:
                return False

        await cdp.evaluate(ws, f"""(() => {{
            const b = document.querySelector({GROK_SUBMIT!r});
            if (!b || b.disabled) return false;
            b.click(); return true;
        }})()""")
        if await was_sent():
            return True

        await cdp.hover_click(ws, f"""(() => {{
            const b = document.querySelector({GROK_SUBMIT!r});
            if (!b) return null;
            const r = b.getBoundingClientRect();
            return {{ x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2) }};
        }})()""")
        return await was_sent()

    async def read_response(self, ws, timeout: int = 45) -> Optional[str]:
        # grok.com (measured): each answer is
        #   div[data-testid="assistant-message"]  -> message bubble (with a "Grok" label)
        #     .response-content-markdown          -> just the answer text
        # The bubble's innerText includes the "Grok" label, so we read the
        # inner markdown block; without it the label line is stripped.
        script = """(() => {
            const NL = String.fromCharCode(10);
            const vis = e => e.offsetParent !== null;
            const bubbles = Array.from(
                document.querySelectorAll('[data-testid="assistant-message"]')
            ).filter(vis);
            const last = bubbles.pop();
            let answer = '';
            if (last) {
                const md = last.querySelector('.response-content-markdown')
                        || last.querySelector('.streamdown-chat-md');
                if (md) {
                    answer = (md.innerText || '').trim();
                } else {
                    const lines = (last.innerText || '').split(NL);
                    if (lines.length && lines[0].trim() === 'Grok') lines.shift();
                    answer = lines.join(NL).trim();
                }
            }
            // On the free tier Grok shows a blocking banner instead of an answer.
            // Return it as the answer text: `PROVIDER_ERROR_KEYWORDS` recognises
            // it and applies a long cooldown. Otherwise we would wait out the 45s timeout.
            if (!answer) {
                const sc = document.querySelector('[data-testid="chat-transcript-scroller"]');
                const tr = sc ? (sc.innerText || '') : '';
                // Exact banners only: the transcript also contains the user's question, so a
                // generic "rate limit" phrase could match the question and fake a block.
                const BLOCKERS = ['Grok is under heavy usage', 'High Demand',
                               'You have reached your limit'];
                for (const t of BLOCKERS) {
                    if (tr.indexOf(t) >= 0) return { answer: t, generating: false };
                }
            }
            const generating = !!document.querySelector(
                'button[aria-label*="Stop" i], [data-testid*="stop"]');
            return { answer: answer, generating: generating };
        })()"""
        return await self.poll_answer(ws, script, timeout=timeout, initial_delay=1.5)
