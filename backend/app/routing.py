"""Routing policy: preserve privacy, capabilities and the requested price ceiling."""
from __future__ import annotations

from . import catalog


# Short tool turns are usually file listing/opening, status checks or one small
# mutation. They do not benefit from a high reasoning pass, but they do pay its
# browser/UI latency. Keep the classifier conservative: long, planning,
# debugging and design prompts stay on the requested model.
_TOOL_COMPLEX_HINTS = (
    "plan", "planning", "analyze", "analyse", "debug", "refactor", "architecture",
    "design", "compare", "explain why", "tahlil", "reja", "arxitektura", "taqqosla",
    "план", "анализ", "отлад", "архитект", "сравни",
)
_TOOL_SPEED_ORDER = {"qwen": 0, "gemini": 1, "chatgpt": 2, "deepseek": 3}


def compatible(source, target, endpoint="chat/completions"):
    if not target or not target.get("enabled") or source["id"] == target["id"]:
        return False
    if catalog.processing_class(source) == "private" and catalog.processing_class(target) != "private":
        return False
    if endpoint != "chat/completions" and target["provider"] != source["provider"]:
        return False
    return True


def price_ceiling(source, target):
    return tuple(min(source[k], target[k]) for k in ("price_in_per_1m", "price_out_per_1m"))


def text_profile(body):
    """Use the actual composer and adapter limits, without fetching/uploading files.

    Attachment routing remains on the requested provider: inspecting files here
    would download them twice, and Qwen's wider text field cannot accept files.
    The API image includes cluster code for this pure metadata/composition path;
    it never starts a browser or imports the cluster server.
    """
    from cluster import compose, tools, reasoning, output_format, settings as cfg
    flat = []
    for message in body.get("messages", []):
        if not isinstance(message, dict):
            return None
        content = message.get("content") or ""
        if isinstance(content, list):
            if any(not isinstance(p, dict) or p.get("type") != "text" for p in content):
                return None
            content = "\n".join(str(p.get("text") or "") for p in content)
        if not isinstance(content, str):
            return None
        if content or message.get("tool_calls") or message.get("role") == "tool":
            flat.append({**message, "content": content})
    offered = body.get("tools") or []
    prompt = compose.compile_prompt(flat, tools_instruction=tools.build_instruction(offered, body.get("tool_choice", "auto")) if offered else "")
    tool_names = [str((t.get("function") or t).get("name")) for t in offered if (t.get("function") or t).get("name")]
    hint = tools.dispatch_hint(tool_names, body.get("tool_choice", "auto"))
    if hint:
        prompt += "\n\n" + hint
    spec = output_format.parse(body.get("response_format"))
    if spec.is_json:
        prompt += "\n\n" + output_format.instruction(spec)
    elif (not offered and not (body.get("stream") and cfg.LIVE_STREAM) and cfg.REASONING_YAML
          and (body.get("reasoning_effort") or "").lower() not in ("none", "minimal", "low")
          and reasoning.should_use(prompt, cfg.REASONING_MIN_CHARS)):
        prompt += "\n\n" + reasoning.INSTRUCTION
    current = next((m["content"] for m in reversed(flat) if m.get("role") == "user"), "")
    return len(prompt), bool(offered), compose.wants_image(current)


def _simple_tool_request(body, profile) -> bool:
    if not profile or not profile[1] or profile[2]:
        return False
    if body.get("tool_choice") == "none":
        return False
    offered = body.get("tools") or []
    if len(offered) > 16:
        return False
    current = ""
    for message in reversed(body.get("messages") or []):
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content") or ""
            if isinstance(content, str):
                current = content
            break
    low = current.lower()
    return (0 < len(current.strip()) <= 1200
            and "```" not in current
            and not any(hint in low for hint in _TOOL_COMPLEX_HINTS))


def tool_target(source, candidates, body):
    """Pick a measured fast, no-thinking cluster model for a simple tool turn.

    The public model and price remain those requested by the client. This is a
    capacity route like ``size_target``: it only changes the internal upstream
    when the candidate accepts the composed prompt and supports tools.
    """
    if source.get("provider") != "cluster":
        return None
    try:
        profile = text_profile(body)
    except (ValueError, TypeError, AttributeError):
        return None
    if not _simple_tool_request(body, profile):
        return None
    chars = profile[0]
    from cluster.adapters import get_adapter
    eligible = []
    for target in candidates:
        if not target.get("enabled") or target.get("provider") != "cluster":
            continue
        if target["id"] != source["id"] and not compatible(source, target):
            continue
        try:
            adapter = get_adapter(target.get("upstream_name"))
            capacity = getattr(adapter, "MAX_INPUT_CHARS", 0)
            has_tools = bool(adapter and adapter.caps.tool_calling)
        except (AttributeError, TypeError, ValueError):
            continue
        if not has_tools or (capacity and capacity < chars):
            continue
        upstream = (target.get("upstream_name") or "").lower()
        eligible.append((int(bool(target.get("allow_thinking", 1))),
                         _TOOL_SPEED_ORDER.get(upstream, 99),
                         target.get("price_in_per_1m", 0) + target.get("price_out_per_1m", 0),
                         target["id"], target))
    if not eligible:
        return None
    chosen = min(eligible, key=lambda item: item[:4])[4]
    if chosen["id"] == source["id"]:
        return None
    return chosen, {"route_reason": "tool_speed", "prompt_chars": chars,
                    "tool_speed": True, "source_model": source["id"]}


def size_target(source, candidates, body):
    if source["provider"] != "cluster":
        return None
    from cluster.adapters import get_adapter
    adapter = get_adapter(source["upstream_name"])
    limit = getattr(adapter, "MAX_INPUT_CHARS", 0)
    if not limit:
        return None
    try:
        profile = text_profile(body)
    except (ValueError, TypeError, AttributeError):
        # Malformed messages/tools/formats still get the cluster's normal
        # validation error; optional routing must not turn them into a 500.
        return None
    if profile is None or profile[0] <= limit:
        return None
    chars, needs_tools, wants_images = profile
    if wants_images:
        # Input-field size is not an image model selection policy.
        return None
    needs_images = wants_images and adapter.caps.image_gen
    eligible = []
    for target in candidates:
        if target["provider"] != "cluster" or not compatible(source, target):
            continue
        dest = get_adapter(target["upstream_name"])
        capacity = getattr(dest, "MAX_INPUT_CHARS", 0)
        if (capacity >= chars and (not needs_tools or dest.caps.tool_calling)
                and (not needs_images or dest.caps.image_gen)):
            eligible.append((capacity, target["price_in_per_1m"] + target["price_out_per_1m"], target["id"], target))
    if not eligible:
        return None
    chosen = min(eligible, key=lambda item: item[:3])[3]
    return chosen, {"route_reason": "input_size", "prompt_chars": chars, "source_input_limit": limit}
