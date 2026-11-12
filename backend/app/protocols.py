"""Anthropic Messages and OpenAI Responses on top of the gateway's chat completions.

Claude apps (Claude Code, Claude Desktop in third-party mode) speak the Anthropic
Messages API; current Codex speaks the OpenAI Responses API. The gateway's whole
pipeline — keys, billing, routing, fallbacks, streaming guards — is built around
/v1/chat/completions, so these two protocols are translated at the edge instead of
being implemented again:

    client body  --to_chat-->  chat completions body  --> proxy() -->
    chat JSON / SSE  --from_chat / *Stream-->  client protocol

Everything here is pure (no I/O), so it is tested on its own. Reasoning text is
not passed back to the clients: Anthropic thinking blocks need a signature the
upstream cannot give, and the cluster's "[gateway] waiting" note is not the
model's. Long silences become `ping` events instead.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any, AsyncIterator


class BadRequest(ValueError):
    """The client's body cannot be translated; becomes a 400 in its protocol."""


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def _text_of(blocks: Any) -> str:
    if isinstance(blocks, str):
        return blocks
    out = []
    for b in blocks or []:
        if isinstance(b, dict) and b.get("type") in ("text", "input_text", "output_text"):
            out.append(str(b.get("text") or ""))
    return "\n".join(out)


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


def _effort_from_budget(budget: Any) -> str:
    try:
        n = int(budget)
    except (TypeError, ValueError):
        return "medium"
    return "low" if n <= 2048 else "medium" if n <= 8192 else "high"


# =============================================================================================
# Anthropic Messages
# =============================================================================================
_STOP_TO_ANTHROPIC = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use",
                      "function_call": "tool_use", "content_filter": "refusal"}


def anthropic_to_chat(body: dict) -> dict:
    msgs = body.get("messages")
    if not isinstance(msgs, list) or not msgs:
        raise BadRequest("`messages` must be a non-empty list.")
    out: list[dict] = []
    # Claude Code 2.1+ also puts {"role": "system"} entries (environment notes, reminders) inside
    # `messages`. They join the top-level system prompt: several upstream chat templates accept a
    # system message only at the start, and the text is still an instruction, not a user turn.
    system_parts = [_text_of(body.get("system"))] if body.get("system") else []
    for m in msgs:
        if isinstance(m, dict) and m.get("role") == "system":
            system_parts.append(_text_of(m.get("content")))
    system_text = "\n\n".join(p for p in system_parts if p)
    if system_text:
        out.append({"role": "system", "content": system_text})
    for m in msgs:
        if isinstance(m, dict) and m.get("role") == "system":
            continue
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
            raise BadRequest("Each message needs role 'user', 'assistant' or 'system'.")
        role, content = m["role"], m.get("content")
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            raise BadRequest("Message content must be a string or a list of blocks.")
        if role == "assistant":
            text, calls = [], []
            for b in content:
                t = b.get("type") if isinstance(b, dict) else None
                if t == "text":
                    text.append(str(b.get("text") or ""))
                elif t == "tool_use":
                    calls.append({"id": str(b.get("id") or _id("toolu")), "type": "function",
                                  "function": {"name": str(b.get("name") or ""),
                                               "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)}})
                # thinking / redacted_thinking: not forwarded (see module docstring)
            msg: dict = {"role": "assistant", "content": "\n".join(text) if text else None}
            if calls:
                msg["tool_calls"] = calls
            if msg["content"] is not None or calls:
                out.append(msg)
            continue
        parts, tool_msgs = [], []
        for b in content:
            t = b.get("type") if isinstance(b, dict) else None
            if t == "text":
                parts.append({"type": "text", "text": str(b.get("text") or "")})
            elif t == "image":
                src = b.get("source") or {}
                if src.get("type") == "base64":
                    url = f"data:{src.get('media_type', 'image/png')};base64,{src.get('data', '')}"
                elif src.get("type") == "url":
                    url = str(src.get("url") or "")
                else:
                    continue
                parts.append({"type": "image_url", "image_url": {"url": url}})
            elif t == "tool_result":
                result = _text_of(b.get("content")) if not isinstance(b.get("content"), str) else b["content"]
                if b.get("is_error"):
                    result = f"Error: {result}"
                tool_msgs.append({"role": "tool", "tool_call_id": str(b.get("tool_use_id") or ""), "content": result or ""})
        # Tool results answer the previous assistant turn, so they come first
        out.extend(tool_msgs)
        if parts:
            only_text = all(p["type"] == "text" for p in parts)
            out.append({"role": "user", "content": "\n".join(p["text"] for p in parts) if only_text else parts})
    chat: dict = {"model": body.get("model"), "messages": out, "stream": bool(body.get("stream"))}
    if body.get("max_tokens") is not None:
        chat["max_tokens"] = body["max_tokens"]
    for k in ("temperature", "top_p"):
        if body.get(k) is not None:
            chat[k] = body[k]
    if body.get("stop_sequences"):
        chat["stop"] = body["stop_sequences"]
    tools = [{"type": "function", "function": {
        "name": t["name"], "description": t.get("description") or "",
        "parameters": t.get("input_schema") or {"type": "object", "properties": {}}}}
        for t in body.get("tools") or [] if isinstance(t, dict) and t.get("name") and t.get("type") in (None, "custom")]
    if tools:
        chat["tools"] = tools
        tc = body.get("tool_choice") or {}
        kind = tc.get("type") if isinstance(tc, dict) else None
        if kind == "any":
            chat["tool_choice"] = "required"
        elif kind == "tool" and tc.get("name"):
            chat["tool_choice"] = {"type": "function", "function": {"name": tc["name"]}}
        elif kind == "none":
            chat["tool_choice"] = "none"
    thinking = body.get("thinking")
    effort = (body.get("output_config") or {}).get("effort") if isinstance(body.get("output_config"), dict) else None
    if isinstance(thinking, dict):
        if thinking.get("type") == "enabled":
            chat["reasoning_effort"] = _effort_from_budget(thinking.get("budget_tokens"))
        elif thinking.get("type") == "disabled":
            chat["reasoning_effort"] = "none"
        elif thinking.get("type") == "adaptive":
            # Adaptive thinking (Claude Code 2.1+): the depth comes from output_config.effort
            level = {"xhigh": "high", "max": "high"}.get(str(effort), str(effort))
            if level in ("low", "medium", "high"):
                chat["reasoning_effort"] = level
    if chat["stream"]:
        chat["stream_options"] = {"include_usage": True}
    return chat


def _usage(chat: dict | None) -> tuple[int, int]:
    u = (chat or {}).get("usage") or {}
    return int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)


def chat_to_anthropic(chat: dict, model: str) -> dict:
    choice = (chat.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content: list[dict] = []
    if msg.get("content"):
        content.append({"type": "text", "text": msg["content"]})
    for call in msg.get("tool_calls") or []:
        fn = call.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = {"_raw": fn.get("arguments")}
        content.append({"type": "tool_use", "id": call.get("id") or _id("toolu"), "name": fn.get("name") or "", "input": args})
    pt, ct = _usage(chat)
    stop = _STOP_TO_ANTHROPIC.get(choice.get("finish_reason") or "stop", "end_turn")
    if any(b["type"] == "tool_use" for b in content) and stop != "max_tokens":
        stop = "tool_use"
    return {"id": "msg_" + str(chat.get("id") or uuid.uuid4().hex)[-24:], "type": "message", "role": "assistant",
            "model": model, "content": content, "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": pt, "output_tokens": ct}}


class AnthropicStream:
    """Chat completion SSE data objects in, Anthropic Messages SSE events out."""

    def __init__(self, model: str):
        self.model = model
        self.started = False
        self.open: tuple[str, Any] | None = None       # ("text", None) | ("tool", chat tool index)
        self.index = -1
        self.stop = "end_turn"
        self.usage = (0, 0)
        self.done = False

    def _start(self, msg_id: str = "") -> list[bytes]:
        if self.started:
            return []
        self.started = True
        return [_sse("message_start", {"type": "message_start", "message": {
            "id": "msg_" + (msg_id or uuid.uuid4().hex)[-24:], "type": "message", "role": "assistant", "model": self.model,
            "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}})]

    def _close(self) -> list[bytes]:
        if self.open is None:
            return []
        self.open = None
        return [_sse("content_block_stop", {"type": "content_block_stop", "index": self.index})]

    def ping(self) -> list[bytes]:
        return self._start() + [_sse("ping", {"type": "ping"})]

    def feed(self, obj: dict) -> list[bytes]:
        out = self._start(str(obj.get("id") or ""))
        if isinstance(obj.get("error"), dict):
            out += self._close()
            err = obj["error"]
            out.append(_sse("error", {"type": "error", "error": {"type": "api_error",
                                                                   "message": str(err.get("message") or err.get("code") or "error")}}))
            self.done = True
            return out
        if obj.get("usage"):
            self.usage = _usage(obj)
        for ch in obj.get("choices") or []:
            d = ch.get("delta") or {}
            if d.get("content"):
                if self.open != ("text", None):
                    out += self._close()
                    self.index += 1
                    self.open = ("text", None)
                    out.append(_sse("content_block_start", {"type": "content_block_start", "index": self.index,
                                                            "content_block": {"type": "text", "text": ""}}))
                out.append(_sse("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                                        "delta": {"type": "text_delta", "text": d["content"]}}))
            for tc in d.get("tool_calls") or []:
                key = ("tool", tc.get("index", 0))
                fn = tc.get("function") or {}
                if self.open != key:
                    out += self._close()
                    self.index += 1
                    self.open = key
                    out.append(_sse("content_block_start", {"type": "content_block_start", "index": self.index,
                                                            "content_block": {"type": "tool_use", "id": tc.get("id") or _id("toolu"),
                                                                              "name": fn.get("name") or "", "input": {}}}))
                if fn.get("arguments"):
                    out.append(_sse("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                                            "delta": {"type": "input_json_delta", "partial_json": fn["arguments"]}}))
                self.stop = "tool_use"
            if ch.get("finish_reason"):
                mapped = _STOP_TO_ANTHROPIC.get(ch["finish_reason"], "end_turn")
                # A tool call cut at max_tokens is incomplete JSON: Claude Code handles max_tokens, not a broken tool_use
                self.stop = mapped if mapped == "max_tokens" else ("tool_use" if self.stop == "tool_use" else mapped)
        return out

    def finish(self) -> list[bytes]:
        if self.done:
            return []
        self.done = True
        out = self._start() + self._close()
        out.append(_sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": self.stop, "stop_sequence": None},
                                          "usage": {"input_tokens": self.usage[0], "output_tokens": self.usage[1]}}))
        out.append(_sse("message_stop", {"type": "message_stop"}))
        return out


_CONTEXT_NUMBERS = re.compile(r"maximum context length is (\d+) tokens, but the request needs about (\d+) tokens")


def anthropic_error(status: int, message: str, code: str | None = None) -> dict:
    if code == "context_length_exceeded" and (m := _CONTEXT_NUMBERS.search(message)):
        # Claude Code recognises an over-long prompt only by Anthropic's own wording and then
        # compacts the conversation and retries; any other text just ends the session.
        message = f"prompt is too long: {m.group(2)} tokens > {m.group(1)} maximum"
    kind = {400: "invalid_request_error", 401: "authentication_error", 402: "billing_error", 403: "permission_error",
            404: "not_found_error", 413: "request_too_large", 429: "rate_limit_error", 529: "overloaded_error"}.get(
        status, "api_error" if status >= 500 else "invalid_request_error")
    return {"type": "error", "error": {"type": kind, "message": message}}


# =============================================================================================
# OpenAI Responses
# =============================================================================================
def responses_to_chat(body: dict) -> tuple[dict, set[str]]:
    """Returns the chat body and the names of `custom` (free-text input) tools."""
    if body.get("previous_response_id"):
        raise BadRequest("previous_response_id is not supported: send the whole conversation in `input` (store=false).")
    out: list[dict] = []
    if body.get("instructions"):
        out.append({"role": "system", "content": str(body["instructions"])})
    items = body.get("input")
    if isinstance(items, str):
        items = [{"role": "user", "content": items}]
    if not isinstance(items, list) or not items:
        raise BadRequest("`input` must be a string or a non-empty list.")
    custom: set[str] = {t["name"] for t in body.get("tools") or [] if isinstance(t, dict) and t.get("type") == "custom" and t.get("name")}

    def assistant_with_calls() -> dict:
        if out and out[-1]["role"] == "assistant" and "tool_calls" in out[-1]:
            return out[-1]
        if out and out[-1]["role"] == "assistant" and "tool_calls" not in out[-1]:
            out[-1]["tool_calls"] = []
            return out[-1]
        out.append({"role": "assistant", "content": None, "tool_calls": []})
        return out[-1]

    for it in items:
        if not isinstance(it, dict):
            continue
        t = it.get("type") or ("message" if it.get("role") else None)
        if t == "message":
            role = str(it.get("role") or "")
            role = "system" if role == "developer" else role
            if role not in ("system", "user", "assistant"):
                continue
            content = it.get("content")
            if isinstance(content, str) or role != "user":
                out.append({"role": role, "content": _text_of(content)})
                continue
            parts = []
            for p in content or []:
                pt = p.get("type") if isinstance(p, dict) else None
                if pt in ("input_text", "output_text", "text"):
                    parts.append({"type": "text", "text": str(p.get("text") or "")})
                elif pt == "input_image" and isinstance(p.get("image_url"), str):
                    parts.append({"type": "image_url", "image_url": {"url": p["image_url"]}})
            if parts:
                only_text = all(x["type"] == "text" for x in parts)
                out.append({"role": "user", "content": "\n".join(x["text"] for x in parts) if only_text else parts})
        elif t in ("function_call", "custom_tool_call"):
            args = it.get("arguments") if t == "function_call" else json.dumps({"input": it.get("input") or ""}, ensure_ascii=False)
            assistant_with_calls()["tool_calls"].append({"id": str(it.get("call_id") or it.get("id") or _id("call")), "type": "function",
                                                         "function": {"name": str(it.get("name") or ""), "arguments": args or "{}"}})
        elif t in ("function_call_output", "custom_tool_call_output"):
            output = it.get("output")
            if not isinstance(output, str):
                output = _text_of(output) if isinstance(output, list) else json.dumps(output, ensure_ascii=False)
            out.append({"role": "tool", "tool_call_id": str(it.get("call_id") or ""), "content": output or ""})
        # reasoning items and server tools (web_search_call, local_shell_call...) are not forwarded
    chat: dict = {"model": body.get("model"), "messages": out, "stream": bool(body.get("stream"))}
    if body.get("max_output_tokens") is not None:
        chat["max_tokens"] = body["max_output_tokens"]
    for k in ("temperature", "top_p", "parallel_tool_calls"):
        if body.get(k) is not None:
            chat[k] = body[k]
    effort = (body.get("reasoning") or {}).get("effort") if isinstance(body.get("reasoning"), dict) else None
    if effort:
        chat["reasoning_effort"] = {"minimal": "low", "xhigh": "high"}.get(effort, effort)
    tools = []
    for t in body.get("tools") or []:
        if not isinstance(t, dict) or not t.get("name"):
            continue
        if t.get("type") == "function":
            tools.append({"type": "function", "function": {"name": t["name"], "description": t.get("description") or "",
                                                           "parameters": t.get("parameters") or {"type": "object", "properties": {}}}})
        elif t.get("type") == "custom":
            # Free-text tools (Codex's apply_patch): one string argument carries the text
            tools.append({"type": "function", "function": {"name": t["name"], "description": t.get("description") or "",
                                                           "parameters": {"type": "object", "properties": {
                                                               "input": {"type": "string", "description": "The complete input text for the tool."}},
                                                               "required": ["input"]}}})
    if tools:
        chat["tools"] = tools
        tc = body.get("tool_choice")
        if tc in ("auto", "required", "none"):
            chat["tool_choice"] = tc
        elif isinstance(tc, dict) and tc.get("name"):
            chat["tool_choice"] = {"type": "function", "function": {"name": tc["name"]}}
    if chat["stream"]:
        chat["stream_options"] = {"include_usage": True}
    return chat, custom


def _call_item(call_id: str, name: str, args: str, custom: set[str], status: str = "completed") -> dict:
    if name in custom:
        try:
            text = json.loads(args or "{}").get("input", "")
        except (ValueError, AttributeError):
            text = args or ""
        return {"type": "custom_tool_call", "id": _id("ctc"), "call_id": call_id, "name": name, "input": text, "status": status}
    return {"type": "function_call", "id": _id("fc"), "call_id": call_id, "name": name, "arguments": args or "{}", "status": status}


def _response_shell(resp_id: str, model: str, status: str, output: list, usage: tuple[int, int] | None, created: int) -> dict:
    r = {"id": resp_id, "object": "response", "created_at": created, "status": status, "model": model,
         "output": output, "parallel_tool_calls": True, "tool_choice": "auto", "tools": [], "error": None,
         "incomplete_details": None}
    if usage is not None:
        r["usage"] = {"input_tokens": usage[0], "input_tokens_details": {"cached_tokens": 0},
                      "output_tokens": usage[1], "output_tokens_details": {"reasoning_tokens": 0},
                      "total_tokens": usage[0] + usage[1]}
    return r


def chat_to_responses(chat: dict, model: str, custom: set[str]) -> dict:
    msg = ((chat.get("choices") or [{}])[0]).get("message") or {}
    output: list[dict] = []
    if msg.get("content"):
        output.append({"type": "message", "id": _id("msg"), "status": "completed", "role": "assistant",
                       "content": [{"type": "output_text", "text": msg["content"], "annotations": []}]})
    for call in msg.get("tool_calls") or []:
        fn = call.get("function") or {}
        output.append(_call_item(call.get("id") or _id("call"), fn.get("name") or "", fn.get("arguments") or "{}", custom))
    cut = ((chat.get("choices") or [{}])[0]).get("finish_reason") == "length"
    shell = _response_shell("resp_" + str(chat.get("id") or uuid.uuid4().hex)[-24:], model,
                            "incomplete" if cut else "completed", output, _usage(chat), int(chat.get("created") or time.time()))
    if cut:
        shell["incomplete_details"] = {"reason": "max_output_tokens"}
    return shell


class ResponsesStream:
    """Chat completion SSE data objects in, OpenAI Responses SSE events out."""

    def __init__(self, model: str, custom: set[str]):
        self.model, self.custom = model, custom
        self.resp_id = _id("resp")
        self.created = int(time.time())
        self.seq = 0
        self.started = False
        self.output: list[dict] = []
        self.text_item: dict | None = None
        self.calls: dict[int, dict] = {}       # chat tool index -> {"item", "output_index", "args"}
        self.usage = (0, 0)
        self.cut = False                       # finish_reason "length": the answer hit max_output_tokens
        self.done = False

    def _ev(self, kind: str, **data) -> bytes:
        self.seq += 1
        return _sse(kind, {"type": kind, "sequence_number": self.seq, **data})

    def _start(self) -> list[bytes]:
        if self.started:
            return []
        self.started = True
        shell = _response_shell(self.resp_id, self.model, "in_progress", [], None, self.created)
        return [self._ev("response.created", response=shell), self._ev("response.in_progress", response=shell)]

    def ping(self) -> list[bytes]:
        # The Responses stream has no ping event; an SSE comment keeps proxies and the client from timing out
        return self._start() + [b": keepalive\n\n"]

    def _close_text(self) -> list[bytes]:
        if not self.text_item:
            return []
        item, self.text_item = self.text_item, None
        text = item["content"][0]["text"]
        idx = self.output.index(item)
        item["status"] = "completed"
        return [self._ev("response.output_text.done", item_id=item["id"], output_index=idx, content_index=0, text=text),
                self._ev("response.content_part.done", item_id=item["id"], output_index=idx, content_index=0,
                         part={"type": "output_text", "text": text, "annotations": []}),
                self._ev("response.output_item.done", output_index=idx, item=item)]

    def feed(self, obj: dict) -> list[bytes]:
        out = self._start()
        if isinstance(obj.get("error"), dict):
            err = obj["error"]
            shell = _response_shell(self.resp_id, self.model, "failed", self.output, self.usage, self.created)
            shell["error"] = {"code": str(err.get("code") or "server_error"), "message": str(err.get("message") or "error")}
            out.append(self._ev("response.failed", response=shell))
            self.done = True
            return out
        if obj.get("usage"):
            self.usage = _usage(obj)
        for ch in obj.get("choices") or []:
            d = ch.get("delta") or {}
            if d.get("content"):
                if not self.text_item:
                    self.text_item = {"type": "message", "id": _id("msg"), "status": "in_progress", "role": "assistant",
                                      "content": [{"type": "output_text", "text": "", "annotations": []}]}
                    self.output.append(self.text_item)
                    idx = len(self.output) - 1
                    out.append(self._ev("response.output_item.added", output_index=idx,
                                        item={**self.text_item, "content": []}))
                    out.append(self._ev("response.content_part.added", item_id=self.text_item["id"], output_index=idx,
                                        content_index=0, part={"type": "output_text", "text": "", "annotations": []}))
                self.text_item["content"][0]["text"] += d["content"]
                out.append(self._ev("response.output_text.delta", item_id=self.text_item["id"],
                                    output_index=self.output.index(self.text_item), content_index=0, delta=d["content"]))
            for tc in d.get("tool_calls") or []:
                i = tc.get("index", 0)
                fn = tc.get("function") or {}
                if i not in self.calls:
                    out += self._close_text()
                    item = _call_item(tc.get("id") or _id("call"), fn.get("name") or "", "", self.custom, status="in_progress")
                    self.output.append(item)
                    self.calls[i] = {"item": item, "output_index": len(self.output) - 1, "args": ""}
                    out.append(self._ev("response.output_item.added", output_index=len(self.output) - 1, item=item))
                if fn.get("arguments"):
                    self.calls[i]["args"] += fn["arguments"]
                    if self.calls[i]["item"]["type"] == "function_call":
                        out.append(self._ev("response.function_call_arguments.delta", item_id=self.calls[i]["item"]["id"],
                                            output_index=self.calls[i]["output_index"], delta=fn["arguments"]))
            if ch.get("finish_reason") == "length":
                self.cut = True
        return out

    def finish(self) -> list[bytes]:
        if self.done:
            return []
        self.done = True
        out = self._start() + self._close_text()
        for c in self.calls.values():
            done = _call_item(c["item"]["call_id"], c["item"]["name"], c["args"], self.custom)
            done["id"] = c["item"]["id"]
            self.output[c["output_index"]] = done
            if done["type"] == "function_call":
                out.append(self._ev("response.function_call_arguments.done", item_id=done["id"],
                                    output_index=c["output_index"], arguments=done["arguments"]))
            else:
                out.append(self._ev("response.custom_tool_call_input.done", item_id=done["id"],
                                    output_index=c["output_index"], input=done["input"]))
            out.append(self._ev("response.output_item.done", output_index=c["output_index"], item=done))
        if self.cut:
            # Not "completed": an answer cut at the token limit (often only reasoning, no output at all)
            # used to look finished, and Codex ended the turn with nothing done.
            shell = _response_shell(self.resp_id, self.model, "incomplete", self.output, self.usage, self.created)
            shell["incomplete_details"] = {"reason": "max_output_tokens"}
            out.append(self._ev("response.incomplete", response=shell))
            return out
        out.append(self._ev("response.completed", response=_response_shell(
            self.resp_id, self.model, "completed", self.output, self.usage, self.created)))
        return out


def responses_error(status: int, message: str, code: str) -> dict:
    return {"error": {"message": message, "type": "invalid_request_error" if status < 500 else "server_error",
                      "code": code, "param": None}}


# =============================================================================================
# chat completions SSE reader
# =============================================================================================
async def read_chat_sse(chunks) -> AsyncIterator[tuple[str, Any]]:
    """Yield ("data", dict) / ("done", None) / ("keepalive", None) from a chat SSE byte stream."""
    buf = b""
    async for chunk in chunks:
        buf += chunk if isinstance(chunk, bytes) else chunk.encode()
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            if line.startswith(b":"):
                yield "keepalive", None
                continue
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload == b"[DONE]":
                yield "done", None
                continue
            try:
                obj = json.loads(payload)
            except ValueError:
                continue
            if isinstance(obj, dict):
                yield "data", obj
