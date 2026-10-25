"""Routing policy: preserve privacy, capabilities and the requested price ceiling."""
from __future__ import annotations

from . import catalog


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
        if content or message.get("tool_calls"):
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
          and (body.get("reasoning_effort") or "").lower() != "none"
          and reasoning.should_use(prompt, cfg.REASONING_MIN_CHARS)):
        prompt += "\n\n" + reasoning.INSTRUCTION
    current = next((m["content"] for m in reversed(flat) if m.get("role") == "user"), "")
    return len(prompt), bool(offered), compose.wants_image(current)


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
