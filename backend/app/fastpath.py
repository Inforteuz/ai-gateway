"""Fast path: answer trivial messages (greetings, status checks, "who are you", thanks)
at once with a fixed reply instead of a model.

Until 2026-11-20 these went to the local GPU model. It shares one GPU with agent traffic
(32k-token prompts), so "kimsan" streamed 16 tokens in 77 s; and a thinking local model
answered "Men Qwen, Alibaba Cloud..." despite its identity note. The replies are the
persona's own examples anyway, so they are now produced here: no queue, no GPU, no leak.
The gateway sends these requests to `make_app()` (provider "canned") through the normal
pipeline, so streaming, usage, billing and receipts work as for any model.

Why (measured): agent CLIs send the whole context (~50k characters) with every message.
Even when a web-chat model (e.g. Gemini) writes its answer in 4-8 s, TYPING the request into
the page took 16-44 s and a single window formed a queue — even "Hey" waited 30-60 s.
Such messages need no history, tools or long instructions.

Safety limits:
  * Only SHORT messages from an explicit list. "ok", "ha" (yes), "davom et" (continue) are
    NOT included — in an agent they mean "continue the task".
  * The last message is the user's and text only (no images/files); no JSON format or
    forced tool call requested; n=1.
  * Text is not stored — no cache or database, routing only.
"""
from __future__ import annotations

import json as _json
import re
import time as _time
import uuid as _uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from .branding import ASSISTANT_NAME as _A, BRAND_NAME as _B

MAX_CHARS = 40

_PATTERN = re.compile(
    r"^(?:"
    # greetings
    r"salom(?:\s*alaykum)?|assalomu?\s*alaykum|assalom|hey+|hi+|hello+|hola|yo|"
    r"(?:hey\s+)?nima\s+gap|salom\s+(?:qalesan|qalaysan|qalaysiz)|how\s+are\s+u|"
    r"привет(?:ик)?|здравствуй(?:те)?|добрый\s+(?:день|вечер)|"
    r"qalesan|qalaysan|qalaysiz|yaxshimisiz|yaxshimisan|ishlar\s+qalay|how\s+are\s+you|как\s+дела|"
    # status checks
    r"ishlayapsanmi|ishlayapsizmi|ishlayabsanmi|ishlayapsanmi\s+o'zi|bormisan|bormisiz|"
    r"are\s+you\s+(?:there|working|alive|ok)|you\s+there|test|testing|ping|"
    r"ты\s+(?:тут|здесь|работаешь)|работаешь|"
    # about the assistant
    r"sen\s+kimsan|siz\s+kimsiz|kimsan|kimsiz|isming\s+nima|ismingiz\s+nima|"
    r"who\s+are\s+you|what(?:'s|\s+is)\s+your\s+name|ты\s+кто|кто\s+ты|как\s+тебя\s+зовут|"
    # thanks
    r"rahmat|raxmat|katta\s+rahmat|rahmat\s+sizga|thanks?|thank\s+you|thx|спасибо|благодарю"
    r")$",
    re.IGNORECASE,
)

# English with examples: given an Uzbek "Sen <name> — ..." ("You are <name> — ...") instruction, the small
# model copied it verbatim and answered "Men Sen <name>, ... yordamchi man" (live test).
PERSONA = (
    f"You are {_A}, the AI assistant of the {_B} platform. Reply in the same language as the user "
    f"(Uzbek, Russian or English) with one or two short, natural sentences. Uzbek examples: to a "
    f"greeting reply \"Salom! Qanday yordam bera olaman?\"; to \"sen kimsan?\" reply \"Men {_A} — {_B} "
    f"platformasining sun'iy intellekt yordamchisiman. Qanday yordam kerak?\"; to \"ishlayapsanmi?\" reply "
    f"\"Ha, ishlayapman. Nima qilamiz?\"; to thanks reply \"Arzimaydi! Yana savol bo'lsa, yozing.\" English "
    f"example: to \"who are you?\" reply \"I'm {_A}, the AI assistant of the {_B} platform. How can I help?\" "
    f"Russian example: to \"кто ты?\" reply \"Я {_A} — ИИ-ассистент платформы {_B}. Чем помочь?\" Never "
    "mention other companies or model names."
)


def _normalize(text: str) -> str:
    t = (text or "").strip().lower()
    t = t.translate({ord(c): "'" for c in "‘’ʻʼ`"})
    t = re.sub(r"[\s]+", " ", t)
    # Strip leading/trailing punctuation and emoji
    t = re.sub(r"^[^\w']+|[^\w']+$", "", t, flags=re.UNICODE)
    return t


def _text_of(content: Any) -> str | None:
    """Text-only content; None if there is an image/file part (not for the fast path)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if not isinstance(p, dict) or p.get("type") not in ("text", "input_text"):
                return None
            parts.append(str(p.get("text") or ""))
        return " ".join(parts)
    return None


def last_user_text(body: dict) -> str | None:
    msgs = body.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return None
    last = msgs[-1]
    if not isinstance(last, dict) or last.get("role") != "user":
        return None
    return _text_of(last.get("content"))


def eligible(body: dict) -> bool:
    """Whether the request qualifies for the fast path."""
    if body.get("web_search") is not None or (body.get("n") or 1) != 1:
        return False
    rf = body.get("response_format")
    if isinstance(rf, dict) and rf.get("type") not in (None, "text"):
        return False
    tc = body.get("tool_choice")
    if tc == "required" or isinstance(tc, dict):
        return False
    text = last_user_text(body)
    if text is None or len(text.strip()) > MAX_CHARS:
        return False
    if _PATTERN.fullmatch(_normalize(text)):
        return True
    # A context-dependent identity follow-up is small talk only immediately
    # after an identity question, never a generic "continue/really?" shortcut.
    if _normalize(text) in ("aslidachi", "aslida-chi", "aslida chi", "really who are you"):
        previous = [m for m in body.get("messages", [])[:-1] if m.get("role") == "user"]
        prior = _normalize(_text_of(previous[-1].get("content")) or "") if previous else ""
        return bool(re.fullmatch(r"(?:sen\s+|siz\s+)?kims(?:an|iz)|who are you|ты кто|кто ты", prior))
    return False


_EN = re.compile(r"^(?:hey+|hi+|hello+|hola|yo|how are (?:u|you)|are you (?:there|working|alive|ok)|you there|test|"
                 r"testing|ping|who are you|what(?:'s| is) your name|thanks?|thank you|thx|really who are you)$")
_KINDS = (
    ("identity", re.compile(r"kim|isming|who are|your name|кто|зовут|aslida|really")),
    ("thanks", re.compile(r"rahmat|raxmat|thank|thx|спасибо|благодарю")),
    ("status", re.compile(r"ishlay|bormisan|bormisiz|there|working|alive|\bok\b|test|ping|тут|здесь|работаешь")),
)
REPLIES = {
    "greeting": {"uz": "Salom! Qanday yordam bera olaman?", "ru": "Привет! Чем могу помочь?",
                 "en": "Hi! How can I help?"},
    "identity": {"uz": f"Men {_A} — {_B} platformasining sun'iy intellekt yordamchisiman. Qanday yordam kerak?",
                 "ru": f"Я {_A} — ИИ-ассистент платформы {_B}. Чем помочь?",
                 "en": f"I'm {_A}, the AI assistant of the {_B} platform. How can I help?"},
    "status": {"uz": "Ha, ishlayapman. Nima qilamiz?", "ru": "Да, работаю. Чем займёмся?",
               "en": "Yes, I'm here. What shall we do?"},
    "thanks": {"uz": "Arzimaydi! Yana savol bo'lsa, yozing.", "ru": "Пожалуйста! Если будут вопросы — пишите.",
               "en": "You're welcome! Ask any time."},
}


def kind(text: str) -> str:
    """greeting | identity | status | thanks."""
    t = _normalize(text)
    return next((k for k, rx in _KINDS if rx.search(t)), "greeting")


def is_identity(body: dict) -> bool:
    """An eligible "who are you" message. Local and key-pool models get the fixed reply only for
    these: they have no web-chat queue to skip, but they did name their real model."""
    return eligible(body) and kind(last_user_text(body) or "") == "identity"


def reply(text: str) -> str:
    """The fixed answer to an eligible message, in its language."""
    t = _normalize(text)
    lang = "ru" if re.search(r"[а-яё]", t) else ("en" if _EN.fullmatch(t) else "uz")
    return REPLIES[kind(t)][lang]


def _tokens(text: str) -> int:
    return max(1, len(text) // 4)


def make_app():
    """OpenAI-shaped upstream that answers eligible messages with `reply()`."""
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        body = await request.json()
        msgs = body.get("messages") or []
        text = reply(_text_of((msgs[-1] or {}).get("content")) or "" if msgs else "")
        usage = {"prompt_tokens": sum(_tokens(_text_of(m.get("content")) or "") for m in msgs if isinstance(m, dict)),
                 "completion_tokens": _tokens(text)}
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        cid, created, model = "chatcmpl-" + _uuid.uuid4().hex[:24], int(_time.time()), body.get("model") or "fastpath"
        if not body.get("stream"):
            return {"id": cid, "object": "chat.completion", "created": created, "model": model, "usage": usage,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}

        def chunk(delta, finish=None, **extra):
            return "data: " + _json.dumps({"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                                           "choices": [{"index": 0, "delta": delta, "finish_reason": finish}], **extra},
                                          ensure_ascii=False) + "\n\n"

        async def events():
            yield chunk({"role": "assistant", "content": text})
            yield chunk({}, "stop")
            if (body.get("stream_options") or {}).get("include_usage"):
                yield "data: " + _json.dumps({"id": cid, "object": "chat.completion.chunk", "created": created,
                                              "model": model, "choices": [], "usage": usage}) + "\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(events(), media_type="text/event-stream")
    return app


def payload(body: dict, upstream: str, max_out: int) -> dict:
    """Compact request for the fast model: only the persona and the last message."""
    text = last_user_text(body) or ""
    if _normalize(text) in ("aslidachi", "aslida-chi", "aslida chi", "really who are you"):
        previous = [m for m in body.get("messages", [])[:-1] if m.get("role") == "user"]
        if previous:
            # This shortcut was admitted because of the preceding identity
            # question. Retain that small context so the follow-up makes sense.
            prior = _text_of(previous[-1].get("content")) or ""
            text = f"[Previous identity question]: {prior}\n[Current message]: {text}"
    out: dict = {
        "model": upstream,
        "messages": [{"role": "system", "content": PERSONA},
                     {"role": "user", "content": text}],
        "max_tokens": min(max_out, 200),
        "reasoning_effort": "none",
        "stream": bool(body.get("stream")),
    }
    if body.get("stream") and body.get("stream_options"):
        out["stream_options"] = body["stream_options"]
    return out
