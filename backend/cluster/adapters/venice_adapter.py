import asyncio
from typing import Optional

from .. import cdp, logs
from .base_adapter import READ_TEXT_JS, BaseAIAdapter, Capabilities

log = logs.get("venice")

# venice.ai (agent chat, MEASURED on a live guest session):
#   - no sign-in at all: the chat answers a guest, which is why it earns its place as
#     a stand-in for the accounts that can be rate-limited or locked out;
#   - input: `textarea.chakra-textarea` ("Chat message input"), Enter sends, there is
#     no send button to click;
#   - every exchange sits in `[data-chat-turn-anchor]`, and inside it the answer is
#     `.assistant-content`, which starts with a "Worked for 3s · 1 step" line;
#   - nothing marks generation as running, but the action buttons
#     (`.minds-chat-message-actions`) only appear once the answer is complete - that
#     is the signal this adapter waits for.

VENICE_INPUT = "textarea.chakra-textarea, textarea"

# "Worked for 3s · 1 step" / "Worked for < 1s · 2 steps" - the agent's own progress line
# The agent prefixes its answer with a progress line: "Thinking..." while it runs,
# "Worked for 3s · 1 step" once it is done. Neither is part of the answer.
_PREAMBLE = r"/^\s*(Thinking\.{0,3}|Worked for[^\n]*?(step|steps)[^\n]*)\n+/i"


class VeniceAdapter(BaseAIAdapter):
    # Conservative until each is tried against a live session: a request routed to a
    # model that cannot actually see an image comes back confidently wrong.
    caps = Capabilities(tool_calling=True, vision=False, files=False, image_gen=False,
                        temporary_chat=False, delete_chats=False)

    def __init__(self):
        super().__init__("venice")

    async def is_fresh_chat(self, ws) -> bool:
        n = await cdp.evaluate(ws, "document.querySelectorAll('[data-chat-turn-anchor]').length", timeout=8)
        return n == 0

    async def send_prompt(self, ws, prompt_text: str) -> bool:
        # The text is REPLACED, not inserted at the cursor: anything left in the box
        # from an unsent request would otherwise be merged into this one.
        fill = f"""(() => {{
            const el = document.querySelector({VENICE_INPUT!r});
            if (!el) return 0;
            el.focus();
            const setter = Object.getOwnPropertyDescriptor(
                Object.getPrototypeOf(el), 'value').set;
            setter.call(el, {prompt_text!r});
            el.dispatchEvent(new Event('input', {{ bubbles: true }}));
            return (el.value || '').length;
        }})()"""
        if not await cdp.evaluate(ws, fill, timeout=20):
            return False
        await asyncio.sleep(0.4)

        length_js = f"((document.querySelector({VENICE_INPUT!r}) || {{}}).value || '').length"
        for attempt in range(3):
            await cdp.key(ws, "Enter")
            # Sent when the box empties; checked often so a fast send is not waited out
            for _ in range(8):
                await asyncio.sleep(0.15)
                try:
                    if not await cdp.evaluate(ws, length_js, timeout=8):
                        return True
                except Exception:
                    pass
            log.warning("venice: send not confirmed (attempt %d)", attempt + 1)
        return False

    async def read_response(self, ws, timeout: int = 45) -> Optional[str]:
        # MEASURED: `.assistant-content` also exists inside the USER's message, and it
        # comes first in the document, so selecting it from the turn returned the
        # prompt back as the answer. The answer is whatever sits under `.assistant`.
        script = ("""(() => {
            const turns = document.querySelectorAll('[data-chat-turn-anchor]');
            const last = turns[turns.length - 1];
            if (!last) return { answer: '', generating: false };
            const assistant = last.querySelector('.assistant');
            if (!assistant) return { answer: '', generating: true };
            const block = assistant.querySelector('.assistant-content') || assistant;

            let text = __READ__(block);
            text = text.replace(__PRE__, '').trim();
            return { answer: text, generating: false };
        })()""".replace("__READ__", READ_TEXT_JS).replace("__PRE__", _PREAMBLE))
        # Nothing on the page says "still writing": the action buttons appear while the
        # answer is still arriving, so they cannot be trusted. The end is found the only
        # way left - the text stops changing - which is why this waits longer than the
        # adapters that have a stop button to watch.
        return await self.poll_answer(ws, script, timeout=timeout, stable_needed=6)
