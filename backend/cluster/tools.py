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
import shlex
import posixpath
from urllib.parse import urlsplit, unquote
from functools import lru_cache
from typing import Any, Mapping, Optional, Sequence

import yaml
from jsonschema.validators import validator_for
from referencing import Registry
from referencing.exceptions import NoSuchResource

from . import logs

log = logs.get("tools")

# The model can answer the YAML protocol with JSON or native OpenAI-shaped
# function objects. Both are data, and both must keep the same allowlist.
_FENCE = re.compile(r"```(?:ya?ml|json)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
# A bare "tool_calls:" block start (for the unfenced case)
_BARE = re.compile(r"(?:^|\n)((?:actions|tool_calls|calls):\s*\n(?:[ \t]+.*\n?)+)",
                   re.MULTILINE)

_MAX_TOOL_CALLS = 8

# A client may legitimately refine a file a few times in one user turn. Once
# the same mutating target has succeeded this many times, another write/edit is
# almost always an agent loop (the model keeps replaying its last plan while
# the client keeps returning the result). Exact argument repeats are stopped on
# the second request below; this limit catches changing code written to the same
# file without blocking the first few repairs.
MAX_SUCCESSFUL_TARGET_ACTIONS = 3
_MUTATING_TOOLS = frozenset({
    "write", "write_file", "edit", "edit_file", "apply_patch", "patch",
    "shell", "execute", "exec", "exec_command", "bash", "run_command",
})
_QUESTION_TOOLS = frozenset({"question", "ask_question", "ask_user", "request_user_input"})


# Bump when the tool protocol changes; this invalidates cache keys.
PROTOCOL_VERSION = "tools-5-schema-atomic"


def _no_remote_schema(uri):
    raise NoSuchResource(ref=uri)


@lru_cache(maxsize=128)
def _validator(encoded_schema: str):
    schema = json.loads(encoded_schema)
    cls = validator_for(schema)
    cls.check_schema(schema)
    # Client schemas are data: resolve local references only, never URLs.
    return cls(schema, registry=Registry(retrieve=_no_remote_schema))


def valid_arguments(arguments: Mapping, schema: Mapping) -> bool:
    try:
        # Round-trip enforces JSON values (YAML dates/NaN/recursive aliases are
        # not valid function arguments). Never coerce strings into numbers.
        encoded = json.dumps(arguments, ensure_ascii=False, allow_nan=False)
        value = json.loads(encoded)
        return _validator(json.dumps(schema, sort_keys=True)).is_valid(value)
    except Exception as exc:
        log.debug("tool schema validation failed (%s)", type(exc).__name__)
        return False


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
        # An outline alone loses nested required fields (e.g. question/options).
        # Send the complete contract once, without duplicating it as an outline.
        schema = fn.get("parameters") or {}
        specs.append("    JSON Schema: " + json.dumps(schema, ensure_ascii=False, separators=(",", ":")))

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
        "- Vosita kerak bo'lmagan savolda yoki vosita natijasi kelib ish tugagan bo'lsa, yakuniy javobni aniq belgilab yoz:",
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
               "If no tool is needed, or the work is complete after tool results, return YAML final_answer: | followed by the final text. "
               "Never announce a future action as a final answer; return its tool call instead."))


def parse_final_answer(text: str) -> Optional[str]:
    """Only a complete, unambiguous final-answer envelope is recognized."""
    stripped = (text or "").strip()
    match = _FENCE.fullmatch(stripped)
    candidate = match.group(1) if match else stripped
    if not candidate.lstrip().startswith(("final_answer:", "{")):
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
    if stripped.startswith(("actions:", "tool_calls:", "calls:", "{")):
        out.append(stripped)
    return out


def parse_tool_calls(text: str, allowed: Sequence[str]) -> tuple[list[dict], str]:
    """Extract tool calls from a model answer.

    Returns (tool_calls, leftover_text). If no tool call is found: ([], original text).

    Names that are not allowed are dropped: if the model invents a tool, the
    agent must not call a function that does not exist.
    """
    if isinstance(allowed, Mapping):
        schemas = allowed
    else:
        schemas = {str(a): {} for a in allowed}
    allowed_set = set(schemas)
    if not allowed_set:
        return [], (text or "")
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
        # A batch is atomic: returning only its valid first operation can
        # write half a file change while silently dropping a broken sibling.
        if len(raw) > _MAX_TOOL_CALLS:
            return [], (text or "")
        for item in raw:
            if not isinstance(item, Mapping):
                return [], (text or "")
            item = item.get("function") or item
            if not isinstance(item, Mapping):
                return [], (text or "")
            name = str(item.get("name") or item.get("tool") or "").strip()
            if not name or name not in allowed_set:
                log.warning("unknown tool dropped: %r", name[:40])
                return [], (text or "")
            args = item.get("arguments")
            if args is None:
                args = item.get("args") or {}
            if isinstance(args, str):
                # The model may have given a JSON string
                try:
                    args = json.loads(args)
                except ValueError:
                    log.warning("invalid JSON arguments dropped for tool %r", name[:40])
                    return [], (text or "")
            if not isinstance(args, Mapping) or not valid_arguments(args, schemas.get(name) or {}):
                return [], (text or "")
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
            if chunk.strip() == (text or "").strip():
                leftover = ""
            return calls, leftover
    return [], (text or "")


def argument_schemas(tools, names):
    return {str((t.get("function") or t).get("name")): (t.get("function") or t).get("parameters") or {}
            for t in tools or [] if str((t.get("function") or t).get("name")) in names}


def _open_target(call):
    """Normalize only a simple macOS open command, including browser variants.

    No shell evaluation, no arbitrary command equivalence, and no state shared
    between users. Compound commands are excluded from automatic suppression.
    """
    fn = call.get("function") or {}
    if fn.get("name") not in ("shell", "execute", "bash", "exec_command"):
        return None
    try:
        args = json.loads(fn.get("arguments") or "{}")
        cmd = args.get("command") or args.get("cmd") or ""
        if not isinstance(cmd, str) or any(c in cmd for c in (";", "&&", "||", "|", "\n", "`", "$")):
            return None
        words = shlex.split(cmd)
        if not words or words.pop(0) not in ("open", "/usr/bin/open"):
            return None
        targets = []
        while words:
            word = words.pop(0)
            if word in ("-a", "-b"):
                if not words:
                    return None
                words.pop(0)
            elif word.startswith("-"):
                return None
            else:
                if word.startswith("file://"):
                    word = unquote(urlsplit(word).path)
                word = re.sub(r"^/Users/[^/]+/", "~/", word)
                targets.append(posixpath.normpath(word) if not word.startswith(("http:", "https:")) else word)
        return tuple(sorted(targets)) if targets else None
    except (ValueError, TypeError, AttributeError):
        return None


def repeated_open(calls, messages):
    """Do not re-open a file in another browser after that task's tool returned.

    A new user turn resets the guard. Explicit failure output permits retry.
    This does not suppress reads/polls, edits, or unrelated shell commands.
    """
    data = [m.model_dump() if hasattr(m, "model_dump") else m for m in messages]
    start = max((i for i, m in enumerate(data) if m.get("role") == "user"), default=-1)
    pending = {}
    done = set()
    for message in data[start + 1:]:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                target = _open_target(call)
                if target and call.get("id"):
                    pending[call["id"]] = target
        elif message.get("role") == "tool":
            target = pending.get(message.get("tool_call_id"))
            result = str(message.get("content") or "")
            if target and not re.search(r"error|failed|not found|no such file|denied|non.?zero|exit code:\s*[1-9]", result, re.I):
                done.add(target)
    return any(_open_target(call) in done for call in calls if _open_target(call))


def _call_signature(call):
    """Stable, content-safe identity for a parsed tool call."""
    fn = call.get("function") or {}
    name = str(fn.get("name") or "").strip().lower()
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except (TypeError, ValueError):
        args = fn.get("arguments") or ""
    return name, json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _question_signature(call):
    """Return a stable identity for the visible prompt of an interactive call.

    Clients sometimes add a fresh id, header or option metadata when an agent
    accidentally replays the same question. Comparing the full arguments would
    miss that case, while comparing all text could conflate unrelated fields.
    The visible question/prompt strings are the useful identity; if a client
    uses an unusual schema, fall back to the normal exact signature.
    """
    name, encoded = _call_signature(call)
    if name not in _QUESTION_TOOLS:
        return None
    try:
        args = json.loads(encoded)
    except (TypeError, ValueError):
        return name, encoded
    prompts = []

    def collect(value, key=""):
        if isinstance(value, Mapping):
            for child_key, child in value.items():
                key_name = str(child_key).strip().lower()
                if key_name in {"question", "prompt"} and isinstance(child, str) and child.strip():
                    prompts.append(" ".join(child.split()))
                else:
                    collect(child, key_name)
        elif isinstance(value, list):
            for child in value:
                collect(child, key)

    collect(args)
    return (name, tuple(prompts)) if prompts else (name, encoded)


def _call_target(call):
    """Return a normalized file/command target for mutation-loop detection."""
    name, _ = _call_signature(call)
    if name not in _MUTATING_TOOLS:
        return None
    try:
        args = json.loads((call.get("function") or {}).get("arguments") or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(args, Mapping):
        return None
    # Different agent clients use different spellings for the same file field.
    for key in ("path", "file_path", "filePath", "filename", "target", "file", "location"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return posixpath.normpath(value.strip())
    # A shell command is a target only when it is exactly the same command;
    # changing commands must not be hidden behind a generic target label.
    if name in {"shell", "execute", "exec", "exec_command", "bash", "run_command"}:
        command = args.get("command") or args.get("cmd")
        return command.strip() if isinstance(command, str) and command.strip() else None
    return None


def _successful_tool_result(content: Any) -> bool:
    """Conservatively recognize a completed tool result without storing it."""
    text = str(content or "")
    return not re.search(
        r"error|failed|failure|not found|no such file|denied|permission denied|"
        r"non.?zero|exit code:\s*[1-9]",
        text,
        re.I,
    )


def repeated_successful_actions(calls, messages, max_target_actions: int = MAX_SUCCESSFUL_TARGET_ACTIONS):
    """Find calls already completed in this user turn.

    The OpenAI tool-call IDs are regenerated on every gateway response, so an
    agent can accidentally replay the same side effect with a new ID. We match
    the function and canonical arguments, and also cap repeated writes to one
    target when the model keeps changing the file contents. A new user message
    resets the guard, and failed results remain retryable.

    Returns ``(exact_repeats, target_loops)`` as lists of current calls. The
    result contains no arguments or tool output, making it safe to use in logs.
    """
    data = [m.model_dump() if hasattr(m, "model_dump") else m for m in messages]
    start = max((i for i, m in enumerate(data) if isinstance(m, Mapping) and m.get("role") == "user"), default=-1)
    pending = {}
    exact_counts = {}
    question_counts = {}
    target_counts = {}
    last_target = None
    target_streak = 0
    for message in data[start + 1:]:
        if not isinstance(message, Mapping):
            continue
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                if not isinstance(call, Mapping) or not call.get("id"):
                    continue
                signature = _call_signature(call)
                pending[str(call["id"])] = (signature, _call_target(call))
        elif message.get("role") == "tool":
            previous = pending.get(str(message.get("tool_call_id") or ""))
            if previous is None or not _successful_tool_result(message.get("content")):
                continue
            signature, target = previous
            exact_counts[signature] = exact_counts.get(signature, 0) + 1
            question_signature = _question_signature({
                "function": {"name": signature[0], "arguments": signature[1]}})
            if question_signature is not None:
                question_counts[question_signature] = question_counts.get(question_signature, 0) + 1
            if target:
                key = target
                if key == last_target:
                    target_streak += 1
                else:
                    last_target, target_streak = key, 1
                target_counts[key] = max(target_counts.get(key, 0), target_streak)
            else:
                # A read or another non-target action between writes is a
                # normal edit/test cycle, so it starts a fresh mutation streak.
                last_target, target_streak = None, 0

    exact, target = [], []
    for call in calls or []:
        signature = _call_signature(call)
        name, _ = signature
        call_target = _call_target(call)
        question_signature = _question_signature(call)
        if exact_counts.get(signature, 0) or (
                question_signature is not None and question_counts.get(question_signature, 0)):
            exact.append(call)
        elif call_target and name in _MUTATING_TOOLS and target_counts.get(call_target, 0) >= max_target_actions:
            target.append(call)
    return exact, target


def unique_open_calls(calls):
    """Emit one identical browser-open per batch; keep all other actions."""
    seen = set()
    result = []
    for call in calls:
        target = _open_target(call)
        if target and target in seen:
            continue
        if target:
            seen.add(target)
        result.append(call)
    return result


def unique_tool_calls(calls):
    """Keep one copy of each exact call in a single model response.

    A tool call id is transport metadata and is regenerated by the gateway.
    Models can therefore emit the same interactive question twice with two
    different ids; clients render both questions and may wait for two answers.
    The same duplication is wasteful for reads and unsafe for side effects, so
    exact function/argument duplicates are removed while calls with different
    arguments remain independent.
    """
    seen = set()
    result = []
    for call in calls or []:
        signature = _call_signature(call)
        if signature in seen:
            continue
        seen.add(signature)
        result.append(call)
    return result


# Traces of a call the model STARTED but did not finish in a parseable form: the
# protocol's own block opener, or the XML-style tags some models fall back to.
_CALL_TRACE = re.compile(
    r"(?m)^[ \t]*(?:actions|tool_calls|calls):[ \t]*$|"
    r'"(?:actions|tool_calls|calls)"\s*:|</?(?:function_call|tool_call|parameter|invoke)\b')


def looks_like_call(text: str) -> bool:
    """True when the answer is a broken tool call rather than prose.

    Measured 2026-10-25 (opencode on qwen): the model opened `actions:`, put a
    multi-line `oldString` in without a block scalar, never wrote `newString`
    and closed with `</parameter></function_call>`. No parser can turn that into
    a valid call, and handed to the agent as text it ends the turn with garbage
    on screen.
    """
    return bool(_CALL_TRACE.search(text or ""))


def announces_action(text: str) -> bool:
    """A final envelope must not disguise a promise to act as completed work."""
    return bool(re.search(
        r"\b(?:let me|i(?:'ll| will| am going to)|next i|now i(?:'ll| will))\b|"
        r"\b(?:tekshirib|ko'rib|o'rganib|o'qib|ochib|tuzatib)\s+(?:chiqaman|beraman|ko'raman)|"
        r"\b(?:hozir|endi)\b.{0,60}\b(?:tekshiraman|o'qiyman|ochaman|tuzataman|yarataman)|"
        r"\b(?:сейчас|сначала|далее)\b.{0,60}\b(?:проверю|открою|прочитаю|изменю|создам)",
        text or "", re.I))


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
    if not content.strip():
        content = "[Tool returned no text. Empty output alone is not a failure. Do not repeat the completed action just to get output.]"
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
