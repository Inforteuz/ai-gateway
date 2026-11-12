"""Prompt enhancement for the "pro" media models (owner's call 2026-11-12).

The local GPU model ("Model 1.0", Qwen) rewrites a short request into a detailed English
prompt before it goes to the image or video generator. Best effort: any failure keeps
the user's own prompt, and the request is never failed because of this step.
"""
from __future__ import annotations

import logging
import re

import httpx

log = logging.getLogger("prompt_boost")

TIMEOUT = 30.0

VIDEO_SYSTEM = (
    "You rewrite a user's video idea into ONE prompt for a text-to-video model. Write in English, under 120 "
    "words: the subject and its action, the setting, camera movement and framing, lighting, lens and colour, "
    "mood and style. Keep every concrete detail the user gave (names, objects, text, numbers, language of any "
    "spoken words) and do not add new subjects. Output only the prompt, no quotes or explanations.")

IMAGE_SYSTEM = (
    "You rewrite a user's picture request into ONE prompt for a text-to-image model. Write in English, under "
    "100 words. Unless the user asks for another style (cartoon, logo, illustration, anime...), make it a "
    "photorealistic photograph: name the subject and its materials, the setting, composition and camera angle, "
    "lens and depth of field, natural lighting and colour, fine texture detail. Keep every concrete detail the "
    "user gave (names, objects, exact text to show, numbers, colours) and do not add new subjects. Output only "
    "the prompt, no quotes or explanations.")


async def enhance(state, settings: dict, prompt: str, system: str) -> str:
    """The richer prompt, or `prompt` unchanged when the local model fails or answers oddly."""
    from . import gateway   # here, not at import time: gateway imports this module
    upstream = settings.get("video_prompt_upstream") or "qwen3:14b"
    client, base, headers, _sem = gateway._target(state, {"provider": "local"}, settings)
    body = {"model": upstream, "stream": False, "max_tokens": 400, "temperature": 0.4, "reasoning_effort": "none",
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt[:4000]}]}
    try:
        resp = await client.post(f"{base}/chat/completions", json=body, headers=headers,
                                 timeout=httpx.Timeout(TIMEOUT, connect=5.0))
        text = (resp.json()["choices"][0]["message"].get("content") or "").strip() if resp.status_code == 200 else ""
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
        text = ""
    # Qwen may still wrap its thinking in <think>...</think>; keep only the answer
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip().strip('"')
    if not (20 <= len(text) <= 2000):
        log.info("prompt enhancement skipped (%d chars)", len(text))
        return prompt
    return text
