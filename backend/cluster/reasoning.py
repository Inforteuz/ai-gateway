"""YAML-structured reasoning to improve answer quality.

IDEA: when a model writes the answer directly, it skips steps on hard
questions. Asking for a SHORT plan first and then the answer improves
accuracy (a cheap form of "think before answering"; web-chat providers have
no `reasoning_effort` parameter, so we do it in the prompt).

WHY YAML rather than free text:
  - Plan and answer can be separated EXACTLY: only the `answer` part goes to
    the user, internal "step 1, step 2" thoughts never leak.
  - A YAML block scalar (`|`) carries multi-line answers, code included,
    without escaping; models make many mistakes doing this in JSON.
  - No harm if it does not parse: the text is returned as is (that is how
    `split_answer` below works).

IMPORTANT: this mode is not applied to every request. Writing a plan for a
trivial question ("hi", "2+2") only slows the answer down, so it is enabled
only when there are signs of complexity.

WHEN IT IS NEVER APPLIED:

  1) The user demands an exact output format ("JSON only",
     `response_format`, "just give the code" ...). A mandatory
     plan/check/answer template breaks that demand: models were seen
     returning plan/check/answer in a ```yaml wrapper to a "JSON only" request.

  2) Code is requested or provided. The template required wrapping code in
     ``` inside `answer`, i.e. ```python inside ```yaml. The provider page's
     markdown renderer reads the inner fence as closing the outer one, so the
     code becomes a plain paragraph and leading whitespace is lost (Python
     returned without indentation). Live check: `tests/live_audit.py`.

If the template is broken (no `answer`, or the template text itself comes
back), the answer is INVALID: it is not shown to the user, not cached, and
the request is resent once without the template (see `server.py`).
"""
from __future__ import annotations

import re
from typing import Optional

import yaml

from . import logs

log = logs.get("reasoning")

_FENCE = re.compile(r"```(?:ya?ml)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)

INSTRUCTION = (
    "[Javob tartibi — majburiy]\n"
    "Bu savol bosqichli yechim talab qiladi. Javobni AYNAN shu YAML ko'rinishida yoz:\n"
    "\n"
    "```yaml\n"
    "plan:\n"
    "  - birinchi qadam (qisqa)\n"
    "  - ikkinchi qadam\n"
    "check: nimani tekshirib ko'rdim va natija to'g'rimi\n"
    "answer: |\n"
    "  Foydalanuvchiga ketadigan YAKUNIY javob.\n"
    "  Ko'p qatorli bo'lsa ham shu blok ichida qoladi.\n"
    "```\n"
    "\n"
    "Qoidalar:\n"
    "- `plan` va `check` — sening ichki ishing, qisqa bo'lsin (jami 5 qatordan oshmasin).\n"
    "- `answer` — to'liq, mustaqil javob: foydalanuvchi faqat shuni ko'radi,\n"
    "  shuning uchun unda \"yuqorida aytganimdek\" kabi havolalar bo'lmasin.\n"
    "- `answer` ichida ``` ishlatma (ichki o'ram tashqi blokni buzadi).\n"
    "- Hisob-kitobni `plan` da qadamba-qadam qilib, `answer` da faqat natijani ber.\n"
)

# Complexity hints: structured reasoning is enabled when these appear
_HARD_HINTS = (
    "nega", "qanday", "isbotla", "hisobla", "yech", "tahlil", "solishtir",
    "qancha", "optimal", "algoritm", "xato", "debug", "refactor", "murakkab",
    "why", "how many", "prove", "calculate", "solve", "analyze", "compare",
    "optimize", "algorithm", "complexity", "step by step",
    "почему", "как", "вычисли", "реши", "докажи", "проанализируй",
)

_CODE_HINTS = ("```", "def ", "class ", "function ", "SELECT ", "import ", "#include")

# Words indicating a code request. With code the template is NOT applied
# (see the module docstring); this list is deliberately broad.
_CODE_TASK = re.compile(
    r"\b(python|javascript|typescript|golang|rust|java|kotlin|swift|php|ruby|sql|bash|"
    r"funksiya|funktsiya|kod|skript|dastur|function|script|snippet|code|regex|"
    r"функци\w*|код|скрипт)\b", re.IGNORECASE)

# The user demands a strict output format. Only phrases about FORMAT match;
# the word "plan" or a plain "answer me" does not.
_FORMAT_DEMAND = re.compile(
    r"\bjson\b|\byaml\b|\bcsv\b|\bxml\b|"
    r"\bfaqat\b[^.\n]{0,40}\b(yoz|qaytar|ber|chiqar|javob)|"
    r"\bonly\b[^.\n]{0,30}\b(output|return|respond|answer|reply|print)|"
    r"\b(output|return|respond)\b[^.\n]{0,20}\bonly\b|"
    r"\bтолько\b|\bбез\s+пояснени",
    re.IGNORECASE)


def task_text(prompt_text: str) -> str:
    """Extract the user's current task from the prompt."""
    marker = "[Current task]:"
    if marker in (prompt_text or ""):
        return prompt_text.split(marker, 1)[1]
    return prompt_text or ""


def demands_format(prompt_text: str) -> bool:
    """Does the user demand a strict output format?"""
    return bool(_FORMAT_DEMAND.search(task_text(prompt_text)))


def involves_code(prompt_text: str) -> bool:
    task = task_text(prompt_text)
    return any(h in task for h in _CODE_HINTS) or bool(_CODE_TASK.search(task))


def should_use(prompt_text: str, min_chars: int = 180) -> bool:
    """Would structured reasoning help this request?

    Criterion: the question is long enough OR has complexity hints, but NEVER
    when the user demands a strict format or code is involved. Not applied to
    short simple questions either: it slows them down for no benefit.
    """
    if not prompt_text:
        return False
    task = task_text(prompt_text)
    if demands_format(prompt_text) or involves_code(prompt_text):
        return False
    low = task.lower()
    if any(h in low for h in _HARD_HINTS):
        return True
    return len(task.strip()) >= min_chars


# Sample texts taken from the template itself. If the model returns the
# template unfilled (observed in practice), these end up in `answer`; that is
# not a real answer.
_PLACEHOLDERS = (
    "foydalanuvchiga ketadigan yakuniy javob",
    "birinchi qadam (qisqa)",
    "nimani tekshirib ko'rdim va natija to'g'rimi",
)


def _is_placeholder(answer: str) -> bool:
    low = (answer or "").lower()
    return any(p in low for p in _PLACEHOLDERS)


_TOP_KEY = re.compile(r"^(plan|check|answer)\s*:", re.MULTILINE)


def split_answer(text: str) -> tuple[str, Optional[dict]]:
    """Extract the user-facing part from a YAML answer.

    Returns (answer, meta). `meta` holds plan/check (for logging).

    Returns ("", None) when the answer is INVALID; the caller does not show
    it, does not cache it, and resends the request without the template.
    Invalid means:
      - the template STRUCTURE is present (top-level `plan:`/`check:`/`answer:`
        keys) but there is no real `answer`, or it cannot be parsed;
      - `answer` is the template's own sample text.
    Detection is structural, not lexical: a plain answer that happens to
    contain the word "plan" is not invalid and is returned as is.
    """
    if not text:
        return text, None

    for chunk in [m.group(1) for m in _FENCE.finditer(text)] + [text]:
        try:
            data = yaml.safe_load(chunk)
        except yaml.YAMLError:
            continue
        if not isinstance(data, dict):
            continue
        answer = data.get("answer")
        if not isinstance(answer, str) or not answer.strip():
            # `plan` without `answer`: read mid-generation, or the model wrote
            # the plan and forgot the answer. This is the model's internal work.
            if "plan" in data or "check" in data:
                log.warning("YAML template: no answer, response invalid")
                return "", None
            continue
        if _is_placeholder(answer):
            log.warning("YAML template: sample text returned, response invalid")
            return "", None
        meta = {k: data[k] for k in ("plan", "check") if k in data}
        return answer.strip(), meta or None

    # No chunk parsed as YAML. If the text has the template's top-level keys,
    # the model broke the format; the plan and internal check must not leak
    # to the user.
    keys = {m.group(1) for m in _TOP_KEY.finditer(text)}
    if "answer" in keys and keys & {"plan", "check"}:
        log.warning("YAML template broken (unparseable), response invalid")
        return "", None
    return text, None


def is_template_residue(text: str) -> bool:
    """Does the answer contain reasoning-template residue (checked before caching)?

    An answer that went through `split_answer` is usually clean; this is a
    second guard for the cache: template text or its top-level structure
    must never be stored.
    """
    if not text:
        return False
    if _is_placeholder(text):
        return True
    keys = {m.group(1) for m in _TOP_KEY.finditer(text)}
    return "answer" in keys and bool(keys & {"plan", "check"})
