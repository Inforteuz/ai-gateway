import asyncio
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

    async def reset_chat(self, ws, timeout: float = 10.0) -> None:
        """Opens a new chat — every request goes into its own conversation."""
        js = """(() => {
            const b = document.querySelector('.new-chat-button, .sidebar-new-chat-btn');
            if (b) { b.click(); return true; }
            return false;
        })()"""
        try:
            if await cdp.evaluate(ws, js, timeout=8):
                await asyncio.sleep(1.0)
                return
        except Exception:
            pass
        # No button found — go to the home page
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
        await asyncio.sleep(0.3)

        # Send, then VERIFY that the text field was cleared
        async def was_sent() -> bool:
            await asyncio.sleep(0.8)
            try:
                return not await cdp.evaluate(
                    ws, "((document.querySelector('#persistentChatbox') || {}).value || '').length",
                    timeout=8)
            except Exception:
                return False

        await cdp.evaluate(ws, """(() => {
            const b = document.querySelector('#chatSubmitButton');
            if (!b || b.offsetParent === null) return false;
            b.click();
            return true;
        })()""")
        if await was_sent():
            return True

        await cdp.key(ws, "Enter")
        return await was_sent()

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
