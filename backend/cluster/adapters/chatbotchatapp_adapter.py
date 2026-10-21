import asyncio
import json
from typing import Optional

from .. import cdp
from .base_adapter import BaseAIAdapter


class ChatbotChatAppAdapter(BaseAIAdapter):
    """Adapter for chatbotchatapp.com.

    Needs no sign-up and answers quickly, but has a daily free limit — once the
    limit is used up `pool.classify_response` detects it.
    """

    def __init__(self):
        super().__init__("chatbotchatapp")

    async def send_prompt(self, ws, prompt_text: str) -> bool:
        send_script = f"""(() => {{
            const ta = document.querySelector('textarea.textarea') || document.querySelector('textarea');
            if (!ta) return false;
            ta.focus();
            const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
            nativeSetter.call(ta, {json.dumps(prompt_text)});
            ta.dispatchEvent(new Event('input', {{ bubbles: true }}));
            ta.dispatchEvent(new Event('change', {{ bubbles: true }}));
            return true;
        }})()"""
        if not await cdp.evaluate(ws, send_script):
            return False
        await asyncio.sleep(0.2)

        click_send = """(() => {
            const btn = document.querySelector('#btn-send-message');
            if (btn) { btn.click(); return true; }
            return false;
        })()"""
        if await cdp.evaluate(ws, click_send):
            return True

        await cdp.key(ws, "Enter")
        return True

    async def read_response(self, ws, timeout: int = 35) -> Optional[str]:
        script = """(() => {
            const generating = !!document.querySelector('.btn-stop-response:not([style*="display: none"])');
            const messages = Array.from(document.querySelectorAll(
                '.message-body, .message-content, .markdown, div[class*="message"]'
            )).map(m => (m.innerText || '').trim()).filter(Boolean);
            return { generating, answer: messages[messages.length - 1] || '' };
        })()"""
        return await self.poll_answer(ws, script, timeout=timeout, initial_delay=1.0)
