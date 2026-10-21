from typing import Optional

from .. import cdp
from .base_adapter import BaseAIAdapter, Capabilities

# deepai.org/chat — no login required.
# Measured DOM:
#   input      : textarea#persistentChatbox.chatbox
#   send       : button#chatSubmitButton
#   answer     : .markdownContainer (clean text)
#   NOTE: the answer's outer wrapper `.outputBox` also contains ADS
#   ("SPONSORED ..."), so the inner `.markdownContainer` is read instead —
#   otherwise the ad text would be appended to the answer.


class DeepAIAdapter(BaseAIAdapter):
    FILE_INPUT = 'input[type="file"]'

    # Free and needs no sign-up, but image/file and agent mode have not been
    # measured — unverified capabilities stay off.
    caps = Capabilities()

    def __init__(self):
        super().__init__("deepai")

    # A clean chat (measured): a conversation adds `#<uuid>` to the URL as soon as the
    # message is sent (before any answer), answers render as `.outputBox`/`.markdownContainer`.
    # A new chat is plain `/chat` with none of them; it is ready ~0.2 s after the click.
    _FRESH_JS = """(() => {
        const box = document.querySelector('#persistentChatbox');
        return location.pathname.replace(/\\/$/, '') === '/chat' && !location.hash
            && !document.querySelector('.markdownContainer, .outputBox')
            && !!box && box.value === '';
    })()"""

    async def is_fresh_chat(self, ws) -> bool:
        """Clean chat check for a pre-warmed page (no previous conversation can leak in)."""
        try:
            return bool(await cdp.evaluate(ws, self._FRESH_JS, timeout=5))
        except Exception:
            return False

    async def reset_chat(self, ws, timeout: float = 10.0) -> None:
        """Opens a new chat — every request goes into its own conversation."""
        js = """(() => {
            const b = document.querySelector('.new-chat-button, .sidebar-new-chat-btn');
            if (b) { b.click(); return true; }
            return false;
        })()"""
        try:
            if await cdp.evaluate(ws, js, timeout=8):
                # Poll instead of a fixed 1 s: the new chat is usually ready in ~0.2 s
                if await self.wait_until(ws, self._FRESH_JS, timeout=3.0):
                    return
        except Exception:
            pass
        # No button found or the page did not become clean — go to the home page
        await self.goto(ws, "https://deepai.org/chat")
        await self.wait_for_input(ws, timeout=timeout, selector="#persistentChatbox")

    async def send_prompt(self, ws, prompt_text: str) -> bool:
        # The text is REPLACED: insert_text would insert at the cursor and
        # stick to leftovers from the previous request.
        fill = f"""(() => {{
            const ta = document.querySelector('#persistentChatbox');
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
        # The page needs a moment to register the input: wait until the send button is
        # visible (polled, usually immediate) instead of a fixed 0.3 s
        await self.wait_until(ws, "(() => { const b = document.querySelector('#chatSubmitButton');"
                                  " return !!b && b.offsetParent !== null && !b.disabled; })()", timeout=1.0)

        # Sent = the text field was cleared (polled every 0.1 s instead of a fixed 0.8 s)
        empty_js = "((document.querySelector('#persistentChatbox') || {}).value || '').length === 0"

        await cdp.evaluate(ws, """(() => {
            const b = document.querySelector('#chatSubmitButton');
            if (!b || b.offsetParent === null) return false;
            b.click();
            return true;
        })()""")
        if await self.wait_until(ws, empty_js, timeout=2.5):
            return True

        await cdp.key(ws, "Enter")
        return await self.wait_until(ws, empty_js, timeout=2.5)

    async def read_response(self, ws, timeout: int = 40) -> Optional[str]:
        script = """(() => {
            // Last answer: `.markdownContainer` is clean text without ads or buttons
            const nodes = document.querySelectorAll('.markdownContainer');
            const last = nodes[nodes.length - 1];
            const generating = !!document.querySelector(
                '.loading-spinner, .typing-indicator, [class*="generating" i]');
            return { generating, answer: last ? (last.innerText || '').trim() : '' };
        })()"""
        return await self.poll_answer(ws, script, timeout=timeout, initial_delay=1.0)
