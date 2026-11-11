"""Anthropic Messages (/v1/messages) and OpenAI Responses (/v1/responses) on the chat pipeline.

Claude Code / Claude Desktop speak Messages, Codex speaks Responses (2026-11-12). The
payloads below follow what those clients send: big system prompts, tool definitions,
tool results in the next turn, streaming always on for Codex.
"""
import json

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from app import protocols
from .conftest import add_key, add_model, add_user


def _sse_chunks(*deltas, finish="stop", usage=(120, 30)):
    out = ['data: {"id":"chatcmpl-abc","choices":[{"index":0,"delta":{"role":"assistant"}}]}\n\n']
    for d in deltas:
        out.append("data: " + json.dumps({"id": "chatcmpl-abc", "choices": [{"index": 0, "delta": d}]}) + "\n\n")
    out.append("data: " + json.dumps({"id": "chatcmpl-abc", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}) + "\n\n")
    out.append("data: " + json.dumps({"id": "chatcmpl-abc", "choices": [],
                                      "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]}}) + "\n\n")
    out.append("data: [DONE]\n\n")
    return out


def _upstream(env, *, reply=None, stream=None):
    seen = []
    fake = FastAPI()

    @fake.post("/v1/chat/completions")
    async def chat(request: Request):
        body = await request.json()
        seen.append(body)
        if body.get("stream"):
            async def gen():
                for e in stream:
                    yield e
            return StreamingResponse(gen(), media_type="text/event-stream")
        return reply

    env.app.state.ext_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=fake), base_url="http://gemini-pool")
    for k, v in {"pool_base_url": "http://gemini-pool:8088/v1", "pool_api_key": "pool-token", "pool_spill_model": ""}.items():
        env.db.set_setting(k, v)
    add_model(env, "agent-model", upstream="up-model", provider="pool")
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    return seen, key


def _events(text):
    out = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.split("\n") if ": " in line and not line.startswith(":"))
        if "event" in lines:
            out.append((lines["event"], json.loads(lines["data"])))
    return out


TOOL_REPLY = {"id": "chatcmpl-1", "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
    "role": "assistant", "content": "Let me look.", "tool_calls": [
        {"id": "call_1", "type": "function", "function": {"name": "Read", "arguments": '{"file_path": "/a.py"}'}}]}}],
    "usage": {"prompt_tokens": 50, "completion_tokens": 9}}

CLAUDE_BODY = {
    "model": "agent-model", "max_tokens": 32000,
    "system": [{"type": "text", "text": "You are Claude Code.", "cache_control": {"type": "ephemeral"}}],
    "tools": [{"name": "Read", "description": "Read a file", "input_schema": {
        "type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}}],
    "thinking": {"type": "enabled", "budget_tokens": 31999},
    "metadata": {"user_id": "u"},
    "messages": [
        {"role": "user", "content": [{"type": "text", "text": "open a.py"}]},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "hm", "signature": "sig"},
            {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"file_path": "/a.py"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": [{"type": "text", "text": "print(1)"}]},
            {"type": "text", "text": "and now?"}]},
    ],
}


# --- Anthropic Messages ------------------------------------------------------------------------------------------
def test_claude_payload_translates_to_chat():
    chat = protocols.anthropic_to_chat(CLAUDE_BODY)
    assert chat["messages"][0] == {"role": "system", "content": "You are Claude Code."}
    assistant = chat["messages"][2]
    assert assistant["tool_calls"][0]["id"] == "toolu_1" and json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"file_path": "/a.py"}
    # The tool result answers the call before the user's new text
    assert chat["messages"][3] == {"role": "tool", "tool_call_id": "toolu_1", "content": "print(1)"}
    assert chat["messages"][4] == {"role": "user", "content": "and now?"}
    assert chat["tools"][0]["function"]["parameters"]["required"] == ["file_path"]
    assert chat["reasoning_effort"] == "high" and chat["max_tokens"] == 32000


def test_messages_with_x_api_key_returns_tool_use(env):
    seen, key = _upstream(env, reply=TOOL_REPLY)
    r = env.client.post("/v1/messages", headers={"x-api-key": key, "anthropic-version": "2023-06-01"}, json=CLAUDE_BODY)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["type"] == "message" and body["model"] == "agent-model" and body["stop_reason"] == "tool_use"
    assert body["content"][0] == {"type": "text", "text": "Let me look."}
    assert body["content"][1]["type"] == "tool_use" and body["content"][1]["input"] == {"file_path": "/a.py"}
    assert body["usage"] == {"input_tokens": 50, "output_tokens": 9}
    assert seen[0]["model"] == "up-model" and seen[0]["tools"][0]["function"]["name"] == "Read"
    assert env.db.one("SELECT model, status_code FROM usage_logs")["status_code"] == 200


def test_messages_stream_has_the_anthropic_event_order(env):
    stream = _sse_chunks({"content": "Hi "}, {"content": "there"},
                         {"tool_calls": [{"index": 0, "id": "call_9", "type": "function", "function": {"name": "Read", "arguments": ""}}]},
                         {"tool_calls": [{"index": 0, "function": {"arguments": '{"file_path":'}}]},
                         {"tool_calls": [{"index": 0, "function": {"arguments": '"/b.py"}'}}]}, finish="tool_calls")
    _, key = _upstream(env, stream=stream)
    r = env.client.post("/v1/messages", headers={"Authorization": f"Bearer {key}"}, json={**CLAUDE_BODY, "stream": True})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    ev = _events(r.text)
    names = [e[0] for e in ev]
    assert names[0] == "message_start" and names[-2:] == ["message_delta", "message_stop"]
    starts = [e[1]["content_block"] for e in ev if e[0] == "content_block_start"]
    assert starts[0]["type"] == "text" and starts[1] == {"type": "tool_use", "id": "call_9", "name": "Read", "input": {}}
    text = "".join(e[1]["delta"]["text"] for e in ev if e[0] == "content_block_delta" and e[1]["delta"]["type"] == "text_delta")
    args = "".join(e[1]["delta"]["partial_json"] for e in ev if e[0] == "content_block_delta" and e[1]["delta"]["type"] == "input_json_delta")
    assert text == "Hi there" and json.loads(args) == {"file_path": "/b.py"}
    assert names.count("content_block_stop") == 2
    final = ev[-2][1]
    assert final["delta"]["stop_reason"] == "tool_use" and final["usage"]["output_tokens"] == 30


def test_messages_errors_have_the_anthropic_shape(env):
    _, key = _upstream(env, reply=TOOL_REPLY)
    r = env.client.post("/v1/messages", headers={"x-api-key": key}, json={**CLAUDE_BODY, "model": "no-such-model"})
    assert r.status_code == 404 and r.json()["type"] == "error" and r.json()["error"]["type"] == "not_found_error"
    r = env.client.post("/v1/messages", headers={"x-api-key": "sk-wrong-wrong-wrong"}, json=CLAUDE_BODY)
    assert r.status_code == 401 and r.json()["error"]["type"] == "authentication_error"
    r = env.client.post("/v1/messages", headers={"x-api-key": key}, json={"model": "agent-model"})
    assert r.status_code == 400 and r.json()["error"]["type"] == "invalid_request_error"


def test_count_tokens(env):
    _, key = _upstream(env, reply=TOOL_REPLY)
    r = env.client.post("/v1/messages/count_tokens", headers={"x-api-key": key}, json=CLAUDE_BODY)
    assert r.status_code == 200 and r.json()["input_tokens"] > 10


# --- OpenAI Responses (Codex) ------------------------------------------------------------------------------------
CODEX_BODY = {
    "model": "agent-model", "instructions": "You are Codex.", "stream": True, "store": False,
    "reasoning": {"effort": "medium", "summary": "auto"}, "include": ["reasoning.encrypted_content"],
    "tool_choice": "auto", "parallel_tool_calls": False,
    "tools": [
        {"type": "function", "name": "shell", "description": "Run a command", "strict": False,
         "parameters": {"type": "object", "properties": {"command": {"type": "array", "items": {"type": "string"}}}}},
        {"type": "custom", "name": "apply_patch", "description": "Apply a patch", "format": {"type": "grammar"}},
        {"type": "web_search"},
    ],
    "input": [
        {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "sandbox: workspace-write"}]},
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "list files"}]},
        {"type": "reasoning", "summary": [], "encrypted_content": "xxx"},
        {"type": "function_call", "name": "shell", "arguments": '{"command":["ls"]}', "call_id": "call_a"},
        {"type": "function_call_output", "call_id": "call_a", "output": "a.py\nb.py"},
        {"type": "custom_tool_call", "name": "apply_patch", "input": "*** Begin Patch", "call_id": "call_b"},
        {"type": "custom_tool_call_output", "call_id": "call_b", "output": "Done"},
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "thanks, now fix b.py"}]},
    ],
}


def test_codex_payload_translates_to_chat():
    chat, custom = protocols.responses_to_chat(CODEX_BODY)
    roles = [m["role"] for m in chat["messages"]]
    assert roles == ["system", "system", "user", "assistant", "tool", "assistant", "tool", "user"]
    assert chat["messages"][3]["tool_calls"][0]["function"]["arguments"] == '{"command":["ls"]}'
    assert json.loads(chat["messages"][5]["tool_calls"][0]["function"]["arguments"]) == {"input": "*** Begin Patch"}
    names = [t["function"]["name"] for t in chat["tools"]]
    assert names == ["shell", "apply_patch"] and custom == {"apply_patch"}
    assert chat["reasoning_effort"] == "medium" and chat["parallel_tool_calls"] is False


def test_previous_response_id_is_refused_clearly():
    try:
        protocols.responses_to_chat({**CODEX_BODY, "previous_response_id": "resp_1"})
    except protocols.BadRequest as exc:
        assert "previous_response_id" in str(exc)
    else:
        raise AssertionError("expected BadRequest")


def test_responses_stream_text_and_custom_tool(env):
    stream = _sse_chunks({"content": "Fixing."},
                         {"tool_calls": [{"index": 0, "id": "call_p", "type": "function", "function": {"name": "apply_patch", "arguments": '{"input":'}}]},
                         {"tool_calls": [{"index": 0, "function": {"arguments": ' "*** Begin Patch\\n*** End Patch"}'}}]},
                         {"tool_calls": [{"index": 1, "id": "call_s", "type": "function", "function": {"name": "shell", "arguments": '{"command":["pytest"]}'}}]},
                         finish="tool_calls")
    seen, key = _upstream(env, stream=stream)
    r = env.client.post("/v1/responses", headers={"Authorization": f"Bearer {key}"}, json=CODEX_BODY)
    assert r.status_code == 200, r.text
    ev = _events(r.text)
    names = [e[0] for e in ev]
    assert names[:2] == ["response.created", "response.in_progress"] and names[-1] == "response.completed"
    assert [e[1]["sequence_number"] for e in ev] == list(range(1, len(ev) + 1))
    assert "".join(e[1]["delta"] for e in ev if e[0] == "response.output_text.delta") == "Fixing."
    done = [e[1]["item"] for e in ev if e[0] == "response.output_item.done"]
    assert done[0]["type"] == "message" and done[0]["content"][0]["text"] == "Fixing."
    assert done[1] == {**done[1], "type": "custom_tool_call", "name": "apply_patch", "call_id": "call_p",
                       "input": "*** Begin Patch\n*** End Patch"}
    assert done[2]["type"] == "function_call" and done[2]["arguments"] == '{"command":["pytest"]}'
    completed = ev[-1][1]["response"]
    assert completed["status"] == "completed" and len(completed["output"]) == 3
    assert completed["usage"]["input_tokens"] == 120 and completed["usage"]["output_tokens"] == 30
    assert seen[0]["stream"] is True and seen[0]["messages"][0] == {"role": "system", "content": "You are Codex."}


def test_responses_non_stream(env):
    _, key = _upstream(env, reply=TOOL_REPLY)
    r = env.client.post("/v1/responses", headers={"Authorization": f"Bearer {key}"},
                        json={"model": "agent-model", "input": "hello", "tools": CODEX_BODY["tools"]})
    assert r.status_code == 200, r.text
    out = r.json()["output"]
    assert out[0]["type"] == "message" and out[1]["type"] == "function_call" and out[1]["call_id"] == "call_1"


def test_mid_stream_error_becomes_response_failed(env):
    stream = ['data: {"choices":[{"index":0,"delta":{"content":"par"}}]}\n\n',
              'data: {"error":{"message":"The connection to the model was lost.","type":"server_error","code":"stream_interrupted"}}\n\n',
              "data: [DONE]\n\n"]
    _, key = _upstream(env, stream=stream)
    r = env.client.post("/v1/responses", headers={"Authorization": f"Bearer {key}"}, json=CODEX_BODY)
    ev = _events(r.text)
    assert ev[-1][0] == "response.failed" and ev[-1][1]["response"]["error"]["code"] == "stream_interrupted"
    assert "response.completed" not in [e[0] for e in ev]
