"""Prompt enhancement for the "pro" media models (owner's call 2026-11-12).

A model rewrites a short request into a detailed English prompt before it goes to the
image or video generator. First choice is the Gemini key pool (provider "pool"): the
local Qwen 14B does not understand Uzbek and swapped the subject ("kulib turgan echki",
a smiling goat, became a carpet in a yurt; a horse in the mountains went to a beach).
The local model stays as the fallback for English requests only: when it was handed
the Uzbek one with the user's words appended, Gemini still drew the carpet. Any other
request falls back to the user's own words plus a style hint. Best effort: the request
is never failed because of this step.
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


def _clean(text: str) -> str:
    # Qwen may still wrap its thinking in <think>...</think>; keep only the answer
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip().strip('"')


async def _ask(client, url: str, headers: dict, body: dict, timeout: float) -> str:
    try:
        resp = await client.post(url, json=body, headers=headers, timeout=httpx.Timeout(timeout, connect=5.0))
        if resp.status_code != 200:
            return ""
        choice = resp.json()["choices"][0]
        if choice.get("finish_reason") == "length":   # cut mid-thought: not a prompt
            return ""
        return _clean(choice["message"].get("content") or "")
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
        return ""


def _usable(text: str) -> bool:
    return 20 <= len(text) <= 2000


_ENGLISH = {"the", "a", "an", "of", "in", "on", "with", "and", "at", "is", "for", "from", "by", "under", "over"}
# When no model could rewrite a non-English request, the generator still gets a nudge towards the pro look
PLAIN_SUFFIX = {
    "image": "Photorealistic photograph, natural light, sharp focus, fine texture detail.",
    "video": "Cinematic, smooth camera movement, natural light, rich detail.",
}


def looks_english(prompt: str) -> bool:
    """The local Qwen rewrites English well and misreads Uzbek, so it only gets English requests."""
    if re.search(r"[\u0400-\u04ff]", prompt):
        return False
    return any(w in _ENGLISH for w in re.findall(r"[a-z]+", prompt.lower()))


async def enhance(state, settings: dict, prompt: str, system: str) -> str:
    """The richer prompt; on failure the user's own prompt (with a style hint when it is not English)."""
    from . import gateway   # here, not at import time: gateway imports this module
    messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt[:4000]}]
    if settings.get("pool_base_url") and settings.get("pool_api_key"):
        client, base, headers, _sem = gateway._target(state, {"provider": "pool"}, settings)
        first = settings.get("prompt_boost_model") or "gemini-3.5-flash"
        # Usually 3-6 s; a key out of credit makes the pool try others, so each model gets a bounded turn.
        # "low", not "none": gemini-3.5-flash times out on "none" and refuses "minimal"; its thinking
        # spends tokens, hence the larger cap (the answer itself is ~100 tokens).
        for upstream in dict.fromkeys([first, "gemini-3-flash-preview"]):
            text = await _ask(client, f"{base}/chat/completions", headers, {
                "model": upstream, "stream": False, "max_tokens": 2000, "temperature": 0.4,
                "reasoning_effort": "low", "messages": messages}, 15.0)
            if _usable(text):
                return text
            log.warning("prompt enhancement: pool model %s gave no usable answer", upstream)
    kind = "video" if system == VIDEO_SYSTEM else "image"
    if not looks_english(prompt):
        return f"{prompt}\n\n{PLAIN_SUFFIX[kind]}"
    upstream = settings.get("video_prompt_upstream") or "qwen3:14b"
    client, base, headers, _sem = gateway._target(state, {"provider": "local"}, settings)
    text = await _ask(client, f"{base}/chat/completions", headers, {
        "model": upstream, "stream": False, "max_tokens": 400, "temperature": 0.4, "reasoning_effort": "none",
        "messages": messages}, TIMEOUT)
    if not _usable(text):
        log.warning("prompt enhancement skipped (%d chars)", len(text))
        return prompt
    return text
