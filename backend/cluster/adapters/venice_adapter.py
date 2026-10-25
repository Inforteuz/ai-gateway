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
#   - the agentic-thinking-indicator contains a spinner/shimmer while working;
#     its "Loading... / Thinking..." text is UI, never an answer.

VENICE_INPUT = "textarea.chakra-textarea, textarea"

# The landing page's decorative 1080p loop costs several CPU cores with
# software rendering. Match the known asset AND its hidden decoration wrapper,
# never arbitrary muted videos or media inside a conversation. A document-level
# play listener also catches the SPA remounting/restarting the background.
QUIET_BACKGROUND_JS = r"""(() => {
    const quiet = video => {
        if (!(video instanceof HTMLVideoElement) || !video.loop || !video.muted
                || video.controls || !video.closest('[aria-hidden="true"]')
                || video.closest('[data-chat-turn-anchor], .assistant, .assistant-content')) return;
        let url;
        try { url = new URL(video.currentSrc || video.src, location.href); }
        catch (_) { return; }
        if (url.origin !== 'https://media.venice.ai'
                || !/^\/assets\/light-waves-loop(?:-v\d+)?\.mp4$/.test(url.pathname)) return;
        if (!video.paused) video.pause();
    };
    if (!window.__gatewayQuietVeniceBackground) {
        const onMedia = event => quiet(event.target);
        document.addEventListener('play', onMedia, true);
        document.addEventListener('loadeddata', onMedia, true);
        window.__gatewayQuietVeniceBackground = true;
    }
    document.querySelectorAll('video').forEach(quiet);
})()"""

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

    async def _quiet_background_media(self, ws) -> None:
        try:
            await cdp.evaluate(ws, QUIET_BACKGROUND_JS, timeout=2)
        except Exception as exc:
            # Optional resource saving must never prevent sending a request.
            log.debug("venice: background media check skipped (%s)", type(exc).__name__)

    async def check_session(self, ws, timeout: float = 5.0,
                            recover: bool = True) -> tuple[bool, str]:
        result = await super().check_session(ws, timeout=timeout, recover=recover)
        if result[0]:
            await self._quiet_background_media(ws)
        return result

    async def is_fresh_chat(self, ws) -> bool:
        await self._quiet_background_media(ws)
        n = await cdp.evaluate(ws, "document.querySelectorAll('[data-chat-turn-anchor]').length", timeout=8)
        return n == 0

    async def send_prompt(self, ws, prompt_text: str) -> bool:
        await self._quiet_background_media(ws)
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

            const indicators = [...block.querySelectorAll('[data-testid="agentic-thinking-indicator"]')];
            const generating = indicators.some(e => e.querySelector('.chakra-spinner, .loading-shimmer'));
            // Read from the live DOM to preserve code formatting. Hide only the
            // measured progress widget and restore it in the same synchronous
            // step, as READ_TEXT_JS does for other provider UI controls.
            const saved = indicators.map(e => [e, e.style.getPropertyValue('display'),
                                               e.style.getPropertyPriority('display')]);
            let text;
            try {
                indicators.forEach(e => e.style.setProperty('display', 'none', 'important'));
                text = __READ__(block);
            } finally {
                for (const [e, value, priority] of saved) {
                    if (value) e.style.setProperty('display', value, priority);
                    else e.style.removeProperty('display');
                }
            }
            text = text.replace(__PRE__, '').trim();
            return { answer: text, generating: generating };
        })()""".replace("__READ__", READ_TEXT_JS).replace("__PRE__", _PREAMBLE))
        # Action buttons can appear before text finishes. After the explicit
        # thinking state ends, still allow the text to settle before returning.
        return await self.poll_answer(ws, script, timeout=timeout, stable_needed=6)
