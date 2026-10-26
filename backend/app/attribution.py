"""Bounded request attribution. Never retain prompts, tool arguments or arbitrary URLs."""
from __future__ import annotations
import json
import re

_LABEL = re.compile(r"[^\w .:/@+-]", re.UNICODE)
_TOOL = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.:-]{0,63}$")


def label(value, limit=80):
    return _LABEL.sub("", str(value or ""))[:limit].strip()


def names(values):
    return sorted({v for v in values if isinstance(v, str) and _TOOL.fullmatch(v)})[:32]


def request_meta(headers, body):
    ua = (headers.get("user-agent") or "").lower()
    client = next((n for n in ("opencode", "claude", "codex", "cursor", "continue", "openai-python", "openai-node", "curl") if n in ua), "other")
    return {"client": client, "project": label(headers.get("x-sf-project")),
            "purpose": label(headers.get("x-sf-purpose")),
            "requested_tools": names((t.get("function") or {}).get("name") for t in body.get("tools", []) if isinstance(t, dict)),
            "called_tools": [], "queue_ms": 0}


def observe(meta, data):
    cached = data.get("cached", (data.get("gateway") or {}).get("cache_hit"))
    if isinstance(cached, bool):
        meta["cache_hit"] = cached
    protocol = (data.get("gateway") or {}).get("tool_protocol")
    if isinstance(protocol, dict):
        meta["tool_protocol"] = {k: protocol[k] for k in ("initial_calls", "retried", "recovered", "missing_calls", "final_answer")
                                 if isinstance(protocol.get(k), bool)}
        guard = protocol.get("loop_guard")
        if isinstance(guard, str) and _TOOL.fullmatch(guard):
            meta["tool_protocol"]["loop_guard"] = guard
        guard_count = protocol.get("loop_guard_count")
        if isinstance(guard_count, int) and 0 < guard_count <= 128:
            meta["tool_protocol"]["loop_guard_count"] = guard_count
        elapsed = protocol.get("retry_ms")
        if isinstance(elapsed, int) and 0 <= elapsed <= 3600000:
            meta["tool_protocol"]["retry_ms"] = elapsed
    # Function names can arrive in fragments. Arguments are intentionally ignored.
    full = set(meta.get("_full_tool_names", []))
    fragments = meta.setdefault("_tool_fragments", {})
    for ci, ch in enumerate((data.get("choices") or [])[:16]):
        msg = ch.get("message") or ch.get("delta") or {}
        for ti, tool in enumerate((msg.get("tool_calls") or [])[:32]):
            if not isinstance(tool, dict):
                continue
            part = (tool.get("function") or {}).get("name")
            if not isinstance(part, str):
                continue
            if ch.get("message") is not None:
                full.add(part)
            else:
                key = f"{ch.get('index', ci)}:{tool.get('index', ti)}"
                if key not in fragments and len(fragments) >= 32:
                    continue
                previous = fragments.get(key, "")
                fragments[key] = (previous if previous == part else previous + part)[:64]
    meta["_full_tool_names"] = names(full)
    meta["called_tools"] = names([*full, *fragments.values()])
    details = ((data.get("usage") or {}).get("completion_tokens_details") or {})
    n = details.get("reasoning_tokens")
    if isinstance(n, int) and 0 <= n <= 100_000_000:
        meta["reasoning_tokens"] = n


def persisted(meta):
    return {k: v for k, v in meta.items() if not k.startswith("_")}


def unpack(raw):
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}
