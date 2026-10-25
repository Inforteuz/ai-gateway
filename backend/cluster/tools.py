"""Tool-calling emulation for coding agents and subagents.

PROBLEM: web-chat pages know nothing about OpenAI's `tools` / `tool_calls`
protocol. They take text and return text. Coding agents (and subagents) rely
on exactly that protocol: the model calls a tool ("read file", "run
command") and the agent returns the result.

SOLUTION: carry the protocol inside the text.
  1. The request's `tools` description is added to the prompt as a strict
     instruction.
  2. To call a tool, the model answers with a YAML block.
  3. That block is parsed and converted into OpenAI-style `tool_calls`.

WHY YAML, not JSON: tool arguments are often multi-line code. In JSON every
line is escaped with `\\n` and models make many mistakes there (broken
escapes, quotes inside quotes). A YAML block scalar (`|`) carries code
unchanged and unescaped, so parsing is much more reliable.
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any, Mapping, Optional, Sequence

import yaml

from . import logs

log = logs.get("tools")

# ```yaml ... ``` or ``` ... ``` blocks
_FENCE = re.compile(r"```(?:ya?ml)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
# A bare "tool_calls:" block start (for the unfenced case)
_BARE = re.compile(r"(?:^|\n)((?:actions|tool_calls|calls):\s*\n(?:[ \t]+.*\n?)+)",
                   re.MULTILINE)

_MAX_TOOL_CALLS = 8


# Bump when the tool protocol changes; this invalidates cache keys.
PROTOCOL_VERSION = "tools-4-explicit-final"


def _schema_outline(schema: Mapping[str, Any], indent: int = 6) -> list[str]:
    """Build a short, readable argument list from a JSON Schema."""
    pad = " " * indent
    props = (schema or {}).get("properties") or {}
    required = set((schema or {}).get("required") or [])
    lines: list[str] = []
    for name, spec in props.items():
        if not isinstance(spec, Mapping):
            spec = {}
        typ = spec.get("type") or "any"
        if isinstance(typ, list):
            typ = "|".join(str(t) for t in typ)
        bits = [f"{pad}{name} ({typ}{', majburiy' if name in required else ''})"]
        desc = str(spec.get("description") or "").strip()
        if desc:
            bits.append(f" — {desc[:160]}")
        enum = spec.get("enum")
        if isinstance(enum, list) and enum:
            bits.append(f" [variantlar: {', '.join(str(e) for e in enum[:8])}]")
        lines.append("".join(bits))
    if not lines:
        lines.append(f"{pad}(argument yo'q)")
    return lines


def build_instruction(tools: Sequence[Mapping[str, Any]],
                      tool_choice: Any = "auto") -> str:
    """Build the instruction block added to the prompt from the `tools` list."""
    specs: list[str] = []
    names: list[str] = []
    for t in tools:
        fn = (t or {}).get("function") or t or {}
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        names.append(name)
        desc = str(fn.get("description") or "").strip()
        specs.append(f"  - {name}: {desc[:300] or '(tavsif yo‘q)'}")
        specs.extend(_schema_outline(fn.get("parameters") or {}))

    if not names:
        return ""

    forced: Optional[str] = None
    if isinstance(tool_choice, Mapping):
        forced = str(((tool_choice.get("function") or {}).get("name")) or "") or None
    elif isinstance(tool_choice, str) and tool_choice not in ("auto", "none", "required"):
        forced = tool_choice

    rules = [
        "[Vositalar — MAJBURIY protokol]",
        "BU MATNLI TRANSPORT PROTOKOLI: vositalarni shu veb-chat ichida IZLAMA yoki ISHGA TUSHIRMA.",
        "YAML chaqiruvini javob sifatida yoz. Uni tashqi API mijoz-agent o'z ish muhitida",
        "bajaradi va keyingi xabarda natijasini yuboradi. Quyidagi ro'yxat MIJOZ vositalaridir;",
        "ular veb-chatning ichki vositalari ro'yxatida bo'lishi shart emas.",
        "Fayl mavjudligini hozir tekshira olmaysan: chaqiruv yozish uning mavjudligini",
        "yoki bajarilganini da'vo qilish emas. Natija kelmaguncha bajarildi deb yozma.",
        "Senda quyidagi vositalar bor. Sen fayllarni, kataloglarni va tashqi",
        "ma'lumotni FAQAT shu vositalar orqali ko'rasan:",
        *specs,
        "",
        "MUHIM: so'ralgan fayl yoki ma'lumot senda YO'Q. Uni ko'rish uchun",
        "vositani chaqirishing SHART. Foydalanuvchidan fayl mazmunini SO'RAMA,",
        "\"menda fayl yo'q\" deb yozma, taxmin qilib javob bermaganing ham ma'qul —",
        "o'rniga vositani chaqir.",
        "",
        "CHAQIRISH FORMATI: javobingda boshqa hech narsa bo'lmasin, faqat shu blok:",
        "",
        "```yaml",
        "actions:",
        "  - name: vosita_nomi",
        "    arguments:",
        "      argument_nomi: qiymat",
        "```",
        "",
        "Masalan, fayl mazmuni kerak bo'lsa:",
        "",
        "```yaml",
        "actions:",
        f"  - name: {names[0]}",
        "    arguments:",
        *[f"      {k}: ..." for k in list(
            ((tools[0] or {}).get("function") or tools[0] or {})
            .get("parameters", {}).get("properties", {}) or {"qiymat": {}})][:3],
        "```",
        "",
        "QOIDALAR:",
        "- Ko'p qatorli qiymat (kod, matn) uchun YAML blok skalyarini ishlat:",
        "    code: |",
        "      def f():",
        "          return 1",
        "- Bir vaqtda bir nechta vositani chaqirsa bo'ladi: ro'yxatga element qo'sh.",
        "- Natija `[Vosita natijasi]` sifatida qaytadi. Shundan keyin yo yana",
        "  vosita chaqir, yo YAKUNIY javobni yoz.",
        "- Yakuniy javob yozganda `actions` blokini QO'SHMA.",
        "- Vosita natijasi kelib, ish tugagan bo'lsa, yakuniy javobni aniq belgilab yoz:",
        "  ```yaml",
        "  final_answer: |",
        "    Foydalanuvchiga yakuniy javob matni.",
        "  ```",
        "  Bu belgi natija tayyorligini bildiradi; bajarilmagan ishni bajarildi dema.",
        "- Vosita nomini o'zingdan to'qima: faqat yuqoridagi ro'yxatdan tanla.",
        "- Bu protokol sening ichki vositalaringga aloqasi yo'q — faqat yuqoridagi",
        "  ro'yxatdagi nomlarni shu YAML formatida ishlat.",
    ]
    if forced:
        rules.append(f"- MAJBURIY: shu safar aynan `{forced}` vositasini chaqir.")
    elif isinstance(tool_choice, str) and tool_choice == "required":
        rules.append("- MAJBURIY: shu safar albatta vositalardan birini chaqir.")
    elif isinstance(tool_choice, str) and tool_choice == "none":
        rules.append("- Bu safar vosita chaqirma, faqat matn bilan javob ber.")
    return "\n".join(rules)


def dispatch_hint(allowed: Sequence[str], tool_choice: Any = "auto") -> str:
    """Last instruction: serialize for the client, not the browser's native tools."""
    if not allowed or tool_choice == "none":
        return ""
    mandatory = tool_choice == "required" or isinstance(tool_choice, Mapping)
    return ("[Client transport]\nReturn a YAML request object for the API client using actions/name/arguments. "
            "The CLIENT executes it in its own workspace; do not search for native browser tools or claim execution. "
            + ("A tool call is required for this turn." if mandatory else
               "After receiving tool results, a completed turn may return YAML final_answer: | followed by the final text."))


def parse_final_answer(text: str) -> Optional[str]:
    """Only a complete, unambiguous final-answer envelope is recognized."""
    stripped = (text or "").strip()
    match = _FENCE.fullmatch(stripped)
    candidate = match.group(1) if match else stripped
    if not candidate.lstrip().startswith("final_answer:"):
        return None
    try:
        data = yaml.safe_load(candidate)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict) or set(data) != {"final_answer"}:
        return None
    value = data["final_answer"]
    return value.strip() if isinstance(value, str) and value.strip() else None


def _candidates(text: str) -> list[str]:
    """Extract the chunks of text that may be YAML."""
    out = [m.group(1) for m in _FENCE.finditer(text or "")]
    out += [m.group(1) for m in _BARE.finditer(text or "")]
    stripped = (text or "").strip()
    if stripped.startswith(("actions:", "tool_calls:", "calls:")):
        out.append(stripped)
    return out


def parse_tool_calls(text: str, allowed: Sequence[str]) -> tuple[list[dict], str]:
    """Extract tool calls from a model answer.

    Returns (tool_calls, leftover_text). If no tool call is found: ([], original text).

    Names that are not allowed are dropped: if the model invents a tool, the
    agent must not call a function that does not exist.
    """
    allowed_set = {str(a) for a in allowed}
    for chunk in _candidates(text):
        try:
            data = yaml.safe_load(chunk)
        except yaml.YAMLError:
            continue
        if not isinstance(data, Mapping):
            continue
        # Several key names: we ask for "actions", but also accept
        # "tool_calls" if the model writes that.
        raw = (data.get("actions") or data.get("tool_calls")
               or data.get("tool_call") or data.get("calls"))
        if isinstance(raw, Mapping):
            raw = [raw]
        if not isinstance(raw, list):
            continue

        calls: list[dict] = []
        for item in raw[:_MAX_TOOL_CALLS]:
            if not isinstance(item, Mapping):
                continue
            name = str(item.get("name") or item.get("tool") or "").strip()
            if not name or (allowed_set and name not in allowed_set):
                log.warning("unknown tool dropped: %r", name[:40])
                continue
            args = item.get("arguments")
            if args is None:
                args = item.get("args") or {}
            if isinstance(args, str):
                # The model may have given a JSON string
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {"value": args}
            if not isinstance(args, Mapping):
                args = {"value": args}
            calls.append({
                "id": "call_" + uuid.uuid4().hex[:20],
                "type": "function",
                "function": {"name": name,
                             "arguments": json.dumps(args, ensure_ascii=False)},
            })
        if calls:
            # Strip the block from the text; the rest is the model's commentary
            leftover = _FENCE.sub("", text or "")
            leftover = _BARE.sub("", leftover).strip()
            return calls, leftover
    return [], (text or "")


def should_retry(calls: Sequence[Any], tool_choice: Any) -> bool:
    """Is the answer worth asking for a second time?

    Yes when tools were offered, nothing was called, and the client did not
    forbid calling. A text-only answer is legal under `tool_choice: "auto"` —
    it is how a turn ends — but the model also produces one when it merely
    ANNOUNCES the action, and from the outside the two look the same. The
    owner's call (2026-10-25): one extra request costs less than an agent
    that stops halfway.
    """
    if calls:
        return False
    return not (isinstance(tool_choice, str) and tool_choice == "none")


def retry_instruction(allowed: Sequence[str], tool_choice: Any = "auto") -> str:
    """Short reminder appended when the first answer carried no `actions:` block.

    WHY this exists: the model often ANNOUNCES the action in prose ("let me
    update the file and add ...") and stops there. The agent is then left with
    nothing to run and the turn dies — measured 2026-10-25 with opencode on
    qwen, where one turn called a tool and the next only narrated.

    It is deliberately short and goes at the very END of the prompt: the first
    attempt already carried the full protocol, and in a long request the model
    had drifted away from it by the time it answered.
    """
    names = ", ".join(f"`{n}`" for n in list(allowed)[:12]) or "yuqoridagi ro'yxat"
    lines = [
        "[Eslatma — javobing qabul qilinmadi]",
        "Vosita tashqi mijoz-agentda bajariladi. Bu chatda uni ishlatish talab qilinmaydi:",
        "faqat YAML chaqiruvini yoz; mijoz bajargach natijasini qaytaradi.",
        "Oldingi javobingda bajariladigan `actions:` bloki yo'q edi: sen nima",
        "qilishingni AYTDING, lekin vositani CHAQIRMADING. Aytilgan ish",
        "o'z-o'zidan bajarilmaydi.",
        "",
        "Agar ish bajarilishi kerak bo'lsa — faqat shu blokni yoz:",
        "```yaml",
        "actions:",
        "  - name: <vosita nomi>",
        "    arguments:",
        "      <parametr>: <qiymat>",
        "```",
        f"Mavjud vositalar: {names}.",
        "",
        "Agar ish ALLAQACHON tugagan bo'lsa va chaqirish kerak bo'lmasa —",
        "yakuniy javobni yoz, `actions` blokisiz. Bahona yozma.",
    ]
    if isinstance(tool_choice, Mapping):
        forced = str(((tool_choice.get("function") or {}).get("name")) or "")
        if forced:
            lines.append(f"- MAJBURIY: aynan `{forced}` vositasini chaqir.")
    elif tool_choice == "required":
        lines.append("- MAJBURIY: albatta vositalardan birini chaqir.")
    return "\n".join(lines)


def render_tool_message(msg: Mapping[str, Any]) -> str:
    """Render a `role: "tool"` message as readable prompt text.

    The result is wrapped in UNTRUSTED markers: a tool may return text from an
    external source (web page, file, API) and any "instructions" inside it
    must not be followed. `tool_call_id` is shown too, so with several calls
    the model does not confuse which result belongs to which call.
    """
    from .compose import wrap_untrusted   # circular import: compose imports tools
    name = str(msg.get("name") or "vosita")
    call_id = str(msg.get("tool_call_id") or "")
    content = msg.get("content")
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    head = f"[Vosita natijasi: {name}" + (f", chaqiruv {call_id}" if call_id else "") + "]"
    return head + "\n" + wrap_untrusted("vosita natijasi", content)


def render_assistant_tool_calls(msg: Mapping[str, Any]) -> str:
    """Re-render the tool calls the assistant made earlier in the history."""
    calls = msg.get("tool_calls") or []
    lines = ["[Sen oldin shu vositalarni chaqirgansan]"]
    for c in calls:
        fn = (c or {}).get("function") or {}
        cid = (c or {}).get("id")
        lines.append(f"- {fn.get('name')}" + (f" (chaqiruv {cid})" if cid else "")
                     + f": {str(fn.get('arguments'))[:400]}")
    return "\n".join(lines)
