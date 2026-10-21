import asyncio
import json
from typing import Optional

from .. import cdp
from .base_adapter import BaseAIAdapter


class ChatbotAppAdapter(BaseAIAdapter):
    def __init__(self):
        super().__init__("chatbotapp")

    async def send_prompt(self, ws, prompt_text: str) -> bool:
        type_script = f"""(() => {{
            const ta = document.querySelector('textarea');
            if (!ta) return false;
            ta.focus();
            const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
            nativeSetter.call(ta, {json.dumps(prompt_text)});
            ta.dispatchEvent(new Event('input', {{ bubbles: true }}));
            ta.dispatchEvent(new Event('change', {{ bubbles: true }}));
            return true;
        }})()"""
        if not await cdp.evaluate(ws, type_script):
            return False
        await asyncio.sleep(0.3)

        click_script = """(() => {
            const btn = document.querySelector('button[aria-label="Отправить сообщение"]')
                     || document.querySelector('button[aria-label*="Send" i]');
            if (btn && !btn.disabled) { btn.click(); return true; }
            return false;
        })()"""
        if await cdp.evaluate(ws, click_script):
            return True

        await cdp.key(ws, "Enter")
        return True

    async def read_response(self, ws, timeout: int = 40) -> Optional[str]:
        script = """(() => {
            const generating = !!document.querySelector('button[aria-label*="Остановить" i], svg.animate-spin');
            const bubbles = Array.from(document.querySelectorAll(
                'div[class*="leading-relaxed"], [class*="message"], .markdown'
            )).map(b => (b.innerText || '').trim()).filter(Boolean);
            return { generating, answer: bubbles[bubbles.length - 1] || '' };
        })()"""
        return await self.poll_answer(ws, script, timeout=timeout, initial_delay=1.0)
