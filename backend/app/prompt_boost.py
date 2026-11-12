"""Prompt enhancement for the "pro" media models (owner's call 2026-11-12).

A model rewrites a short request into a detailed English prompt before it goes to the
image or video generator. First choice is the Gemini key pool (provider "pool"): the
local Qwen 14B does not understand Uzbek and swapped the subject ("kulib turgan echki",
a smiling goat, became a carpet in a yurt; a horse in the mountains went to a beach).
The local model stays as the fallback, and then the user's own words travel along as
the authority. Best effort: any failure keeps the user's prompt, and the request is
never failed because of this step.
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


async def enhance(state, settings: dict, prompt: str, system: str) -> str:
    """The richer prompt, or `prompt` unchanged when no model gives a usable answer."""
    from . import gateway   # here, not at import time: gateway imports this module
    messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt[:4000]}]
    if settings.get("pool_base_url") and settings.get("pool_api_key"):
        client, base, headers, _sem = gateway._target(state, {"provider": "pool"}, settings)
        # "low", not "none": the pool's gemini-3.5-flash times out on "none" and refuses "minimal";
        # its thinking spends tokens, hence the larger cap (the answer itself is ~100 tokens)
        text = await _ask(client, f"{base}/chat/completions", headers, {
            "model": settings.get("prompt_boost_model") or "gemini-3.5-flash", "stream": False,
            "max_tokens": 2000, "temperature": 0.4, "reasoning_effort": "low", "messages": messages}, 25.0)
        if _usable(text):
            return text
        log.info("prompt enhancement: pool gave no usable answer, trying the local model")
    upstream = settings.get("video_prompt_upstream") or "qwen3:14b"
    client, base, headers, _sem = gateway._target(state, {"provider": "local"}, settings)
    text = await _ask(client, f"{base}/chat/completions", headers, {
        "model": upstream, "stream": False, "max_tokens": 400, "temperature": 0.4, "reasoning_effort": "none",
        "messages": messages}, TIMEOUT)
    if not _usable(text):
        log.info("prompt enhancement skipped (%d chars)", len(text))
        return prompt
    # The local model may misread a non-English request; the generator gets the user's words as the authority
    return f"{text}\n\nThe user's own request (follow it wherever the description above disagrees): {prompt[:1000]}"
