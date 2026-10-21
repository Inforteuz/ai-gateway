import json
from typing import Optional

from .. import cdp
from .base_adapter import BaseAIAdapter


class GenSparkAdapter(BaseAIAdapter):
    def __init__(self):
        super().__init__("genspark")

    async def send_prompt(self, ws, prompt_text: str) -> bool:
        send_script = f"""(() => {{
            const ta = document.querySelector('textarea.j-search-input') || document.querySelector('textarea');
            if (!ta) return false;
            ta.focus();
            const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
            nativeSetter.call(ta, {json.dumps(prompt_text)});
            ta.dispatchEvent(new Event('input', {{ bubbles: true }}));
            ta.dispatchEvent(new Event('change', {{ bubbles: true }}));

            const btn = document.querySelector('button[aria-label="Send message"], .input-icon');
            if (btn) {{ btn.click(); return true; }}
            return false;
        }})()"""
        if await cdp.evaluate(ws, send_script):
            return True

        await cdp.key(ws, "Enter")
        return True

    async def read_response(self, ws, timeout: int = 40) -> Optional[str]:
        script = """(() => {
            const generating = !!document.querySelector(
                '.stop-generating, [class*="stop"], .loading, svg.animate-spin'
            );
            const contents = Array.from(document.querySelectorAll(
                '.conversation-content .content, div[class*="answer"], div[class*="markdown"]'
            )).map(c => (c.innerText || '').trim()).filter(Boolean);
            return { generating, answer: contents[contents.length - 1] || '' };
        })()"""
        return await self.poll_answer(ws, script, timeout=timeout, initial_delay=1.0)
