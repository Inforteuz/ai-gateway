"""Bound local inference, including JSON callers, without storing generated content."""
from __future__ import annotations
import asyncio
import json
import time
from . import loops


class GuardStopped(RuntimeError):
    pass


async def bounded_lines(response, seconds, budget=None):
    # Bound each pending read as well as total duration. Keepalives cannot
    # extend either the generation deadline or the reasoning-only budget.
    deadline = time.monotonic() + max(0.001, seconds)
    iterator = response.aiter_lines().__aiter__()
    while True:
        remaining = deadline - time.monotonic()
        reasoning_remaining = None
        if budget is not None and not budget.visible:
            reasoning_remaining = budget.reasoning_seconds - (time.monotonic() - budget.started)
            remaining = min(remaining, reasoning_remaining)
        try:
            async with asyncio.timeout(max(0.001, remaining)):
                line = await anext(iterator)
        except StopAsyncIteration:
            return
        except TimeoutError:
            if reasoning_remaining is not None and reasoning_remaining <= max(0, deadline - time.monotonic()) + .01:
                raise GuardStopped("reasoning_budget_exceeded")
            raise
        yield line


class Budget:
    def __init__(self, reasoning_seconds):
        self.started = time.monotonic()
        self.reasoning_seconds = reasoning_seconds
        self.visible = False
        self.tails = {"content": "", "reasoning": ""}
        self.unchecked = 0

    def observe(self, obj, repetition=True):
        for choice in obj.get("choices") or []:
            delta = choice.get("delta") or {}
            self.visible |= bool(delta.get("content") or delta.get("tool_calls"))
            for kind, text in (("content", delta.get("content")), ("reasoning", delta.get("reasoning") or delta.get("reasoning_content"))):
                if isinstance(text, str):
                    self.tails[kind] = (self.tails[kind] + text)[-loops.TAIL:]
                    self.unchecked += len(text)
        if repetition and self.unchecked >= 150:
            self.unchecked = 0
            if any(loops.is_looping(t) for t in self.tails.values()):
                raise GuardStopped("repetition_loop")
        if not self.visible and time.monotonic() - self.started >= self.reasoning_seconds:
            raise GuardStopped("reasoning_budget_exceeded")


async def collect(response, total_seconds, reasoning_seconds):
    """Internal SSE -> OpenAI JSON, preserving usage, tools and choice indexes."""
    budget = Budget(reasoning_seconds)
    output = {"object": "chat.completion", "choices": []}
    choices = {}
    try:
        async for line in bounded_lines(response, total_seconds, budget):
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if raw == "[DONE]":
                break
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                raise GuardStopped("bad_upstream_json")
            if not isinstance(obj, dict) or obj.get("error"):
                raise GuardStopped("upstream_error")
            budget.observe(obj)
            for k in ("id", "created", "model", "usage"):
                if k in obj:
                    output[k] = obj[k]
            for ch in obj.get("choices") or []:
                idx = ch.get("index", 0)
                c = choices.setdefault(idx, {"index": idx, "message": {"role": "assistant", "content": ""}, "finish_reason": None})
                msg = c["message"]
                d = ch.get("delta") or {}
                for k in ("content", "reasoning", "reasoning_content"):
                    if isinstance(d.get(k), str):
                        msg[k] = msg.get(k, "") + d[k]
                for tc in d.get("tool_calls") or []:
                    tools = msg.setdefault("tool_calls", [])
                    i = tc.get("index", 0)
                    if not isinstance(i, int) or not 0 <= i < 128:
                        raise GuardStopped("invalid_tool_call")
                    while len(tools) <= i:
                        tools.append({"type": "function", "function": {"name": "", "arguments": ""}})
                    target = tools[i]
                    if tc.get("id"):
                        target["id"] = tc["id"]
                    for k in ("name", "arguments"):
                        target["function"][k] += (tc.get("function") or {}).get(k) or ""
                if ch.get("finish_reason"):
                    c["finish_reason"] = ch["finish_reason"]
        # DONE is a transport marker, not proof of a completed answer/tool.
        # Require every choice to finish even when that marker was received.
        if not choices or not all(c["finish_reason"] for c in choices.values()):
            raise GuardStopped("stream_interrupted")
        output["choices"] = [choices[k] for k in sorted(choices)]
        return output
    except TimeoutError as exc:
        raise GuardStopped("generation_budget_exceeded") from exc
    finally:
        await response.aclose()
