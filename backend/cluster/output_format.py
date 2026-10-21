"""`response_format`: enforce the output format the client asked for.

The field must be declared on `ChatCompletionRequest`; otherwise pydantic
drops it SILENTLY and a client asking for JSON mode gets plain text without
knowing it.

Web-chat providers have no `response_format` parameter, so it cannot be
passed through an API. The mode is therefore enforced in three layers:

  1) A strict instruction is added to the prompt (and the YAML reasoning
     template is disabled, since it conflicts with the format).
  2) The answer is VALIDATED: the whole text must parse with `json.loads`
     and be an object. For `json_schema`, top-level `required` keys are
     checked too.
  3) If invalid, it is re-asked once; if still invalid, the client gets an
     explicit ERROR rather than invalid text. Invalid answers are not cached.

Normalization is DELIBERATELY narrow: the wrapper is stripped only when the
WHOLE answer is a single ``` block whose content is a valid JSON object.
JSON is never "dug out" of prose with a regex, since that could pick the
wrong fragment from among comments and hide the error.

An unsupported value (unknown `type`) is not silently ignored; it returns 400.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

SUPPORTED = ("text", "json_object", "json_schema")


class FormatError(ValueError):
    """The request's `response_format` is invalid or unsupported."""


@dataclass(frozen=True)
class FormatSpec:
    kind: str = "text"
    schema: Optional[dict] = None
    name: str = ""
    required: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_json(self) -> bool:
        return self.kind in ("json_object", "json_schema")

    def cache_token(self) -> str:
        """Stable representation that goes into the cache key."""
        if not self.is_json:
            return "text"
        return self.kind + ":" + json.dumps(self.schema or {}, sort_keys=True, ensure_ascii=False)


TEXT = FormatSpec()


def parse(rf: Any) -> FormatSpec:
    """Validate the `response_format` sent by the client."""
    if rf is None:
        return TEXT
    if not isinstance(rf, dict):
        raise FormatError("`response_format` obyekt bo'lishi kerak, masalan {\"type\": \"json_object\"}")
    kind = rf.get("type")
    if kind not in SUPPORTED:
        raise FormatError(
            f"`response_format.type` = {kind!r} qo'llab-quvvatlanmaydi. "
            f"Mumkin bo'lganlari: {', '.join(SUPPORTED)}")
    if kind == "text":
        return TEXT
    if kind == "json_object":
        return FormatSpec(kind="json_object")
    js = rf.get("json_schema")
    if not isinstance(js, dict) or not isinstance(js.get("schema"), dict):
        raise FormatError("`json_schema` rejimida `response_format.json_schema.schema` obyekt bo'lishi kerak")
    schema = js["schema"]
    if schema.get("type") not in (None, "object"):
        raise FormatError("`json_schema` faqat obyekt (`type: object`) sxemani qo'llab-quvvatlaydi")
    req = tuple(str(k) for k in (schema.get("required") or []) if isinstance(k, str))
    return FormatSpec(kind="json_schema", schema=schema, name=str(js.get("name") or ""), required=req)


def instruction(spec: FormatSpec) -> str:
    if not spec.is_json:
        return ""
    lines = [
        "[Javob formati — majburiy]",
        "Javobni FAQAT bitta to'g'ri JSON obyekt sifatida ber.",
        "JSON'dan oldin ham, keyin ham hech qanday matn, izoh, sarlavha yoki ``` o'ram yozma.",
    ]
    if spec.kind == "json_schema":
        lines.append("Obyekt quyidagi JSON Schema'ga mos bo'lsin:")
        lines.append(json.dumps(spec.schema, ensure_ascii=False))
    return "\n".join(lines)


def retry_instruction(spec: FormatSpec, why: str) -> str:
    return (instruction(spec) + "\n"
            f"Oldingi urinish talabga mos kelmadi ({why}). Endi faqat JSON obyektni qaytar.")


_WHOLE_FENCE = re.compile(r"\A```[a-zA-Z0-9_-]*[ \t]*\n(.*)\n```\s*\Z", re.DOTALL)


def normalize(answer: str, spec: FormatSpec) -> tuple[bool, str, str]:
    """Check the answer against the requested format.

    Returns (ok, text, reason). When `ok=False`, `text` must NOT be sent to
    the client.
    """
    if not spec.is_json:
        return True, answer, ""
    text = (answer or "").strip()
    if not text:
        return False, text, "bo'sh javob"

    candidate = text
    try:
        data = json.loads(candidate)
    except ValueError:
        m = _WHOLE_FENCE.match(text)
        if not m:
            return False, text, "JSON emas (atrofida matn bor yoki sintaksis xato)"
        candidate = m.group(1).strip()
        try:
            data = json.loads(candidate)
        except ValueError:
            return False, text, "``` ichidagi matn JSON emas"

    if not isinstance(data, dict):
        return False, text, "JSON obyekt emas"
    missing = [k for k in spec.required if k not in data]
    if missing:
        return False, text, f"majburiy kalitlar yo'q: {', '.join(missing)}"
    # Re-serialize compactly: the client runs `json.loads`, so whitespace is
    # irrelevant, while values stay unchanged.
    return True, json.dumps(data, ensure_ascii=False), ""
