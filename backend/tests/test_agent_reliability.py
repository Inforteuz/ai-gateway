import asyncio
import json

import pytest

from cluster import tools, server, pool, runmeta
from .test_audit_regressions import cluster_client


SCHEMA = {"type": "object", "required": ["path", "content"], "additionalProperties": False,
          "properties": {"path": {"type": "string"}, "content": {"type": "string"}}}
TOOLS = [{"type": "function", "function": {"name": "write", "parameters": SCHEMA}}]


@pytest.mark.parametrize("args", [{"path": "a.py"}, {"path": 7, "content": "x"},
                                     {"path": "a", "content": "x", "extra": True}])
def test_schema_invalid_arguments_are_not_forwarded(args):
    raw = json.dumps({"actions": [{"name": "write", "arguments": args}]})
    assert tools.parse_tool_calls(raw, {"write": SCHEMA}) == ([], raw)


def test_nested_schema_and_local_reference():
    schema = {"type": "object", "properties": {"options": {"type": "array", "items": {"$ref": "#/$defs/option"}}},
              "required": ["options"], "$defs": {"option": {"type": "object", "required": ["label"],
                  "properties": {"label": {"type": "string"}}}}}
    assert tools.valid_arguments({"options": [{"label": "yes"}]}, schema)
    assert not tools.valid_arguments({"options": [{"wrong": "yes"}]}, schema)
    assert not tools.valid_arguments({"a": 1}, {"$ref": "http://127.0.0.1/schema"})


def test_partial_batch_is_atomic_and_never_drops_a_broken_sibling():
    raw = json.dumps({"actions": [{"name": "write", "arguments": {"path": "a", "content": "x"}},
                                   {"name": "write", "arguments": {"path": "b"}}]})
    assert not tools.parse_tool_calls(raw, {"write": SCHEMA})[0]
    valid = json.dumps({"actions": [{"name": "write", "arguments": {"path": "a", "content": "x"}}]})
    assert tools.parse_tool_calls(valid, {"write": SCHEMA})[0]


def test_yaml_non_json_values_and_recursive_aliases_are_rejected():
    for text in ['actions:\n - name: write\n   arguments: {path: x, content: 2026-10-26}',
                 'actions:\n - name: write\n   arguments: &a {path: x, content: *a}',
                 'actions:\n - name: write\n   arguments: {path: x, content: .nan}']:
        assert not tools.parse_tool_calls(text, {"write": SCHEMA})[0]


@pytest.mark.parametrize("stream", [False, True])
def test_invalid_tool_schema_gets_one_repair_before_reaching_client(cluster_client, stream):
    wrong = json.dumps({"actions": [{"name": "write", "arguments": {"path": "a.py"}}]})
    right = json.dumps({"actions": [{"name": "write", "arguments": {"path": "a.py", "content": "print(1)"}}]})
    response, bal = cluster_client([wrong, right], {"tools": TOOLS, "stream": stream,
        "messages": [{"role": "user", "content": "Create a.py"}]})
    assert response.status_code == 200 and len(bal.prompts) == 2
    assert "print(1)" in response.text
    if stream:
        events = [json.loads(l[5:]) for l in response.text.splitlines() if l.startswith("data:") and "[DONE]" not in l]
        calls = [t for e in events for ch in e.get("choices", []) for t in ch.get("delta", {}).get("tool_calls", [])]
        assert calls[0]["index"] == 0


def test_parallel_stream_calls_keep_separate_indexes():
    calls = [{"id": "one", "type": "function", "function": {"name": "read", "arguments": '{"path":"a"}'}},
             {"id": "two", "type": "function", "function": {"name": "read", "arguments": '{"path":"b"}'}}]
    chunks = server._sse_chunks({}, calls, "", {})
    emitted = json.loads(chunks[0][5:])["choices"][0]["delta"]["tool_calls"]
    assert [c["index"] for c in emitted] == [0, 1]
    assert [c["function"]["arguments"] for c in emitted] == ['{"path":"a"}', '{"path":"b"}']


def test_reliable_image_provider_cannot_return_preview_while_generating(monkeypatch):
    class Adapter:
        IMAGE_DONE_RELIABLE = True
        polls = 0
        async def read_images(self, ws):
            self.polls += 1
            return ["preview"] if self.polls < 6 else ["final"]
        async def generation_done(self, ws):
            return self.polls >= 7
    async def sleep(seconds):
        pass
    monkeypatch.setattr(pool.asyncio, "sleep", sleep)
    tab = object.__new__(pool.WorkerTab)
    tab.adapter = Adapter()
    assert asyncio.run(tab._wait_for_images(None, 10)) == ["final"]
    assert tab.adapter.polls == 7


def test_identity_followup_is_compact_only_with_identity_context():
    from app.fastpath import eligible, payload
    body = {"messages": [{"role": "user", "content": "Kimsan?"},
        {"role": "assistant", "content": "I am an assistant."}, {"role": "user", "content": "Aslidachi"}]}
    assert eligible(body)
    compact = payload(body, "fast", 200)["messages"][-1]["content"]
    assert "Kimsan?" in compact and "Aslidachi" in compact and len(compact) < 120
    assert not eligible({"messages": [{"role": "user", "content": "Fix the program"},
        {"role": "assistant", "content": "It is fixed."}, {"role": "user", "content": "Aslidachi"}]})


def _opening(command, cid="opened"):
    return {"id": cid, "type": "function", "function": {"name": "shell", "arguments": json.dumps({"command": command})}}


def test_browser_open_loop_stops_across_browser_variants_and_resets_for_new_task():
    first = _opening('open -a Safari ~/Desktop/clock/index.html')
    messages = [{"role":"user","content":"Open the clock"},
        {"role":"assistant","tool_calls":[first]}, {"role":"tool","tool_call_id":"opened","content":""}]
    repeat = [_opening('open -a "Google Chrome" /Users/test/Desktop/clock/index.html')]
    assert tools.repeated_open(repeat, messages)
    assert not tools.repeated_open([_opening('open ~/Desktop/other/index.html')], messages)
    assert not tools.repeated_open(repeat, messages + [{"role":"user","content":"Open it again"}])
    messages[-1]["content"] = "Error: application not found"
    assert not tools.repeated_open(repeat, messages)


def test_successful_tool_action_loop_is_stopped_even_with_a_new_call_id():
    first = {"id": "old", "type": "function", "function": {
        "name": "write", "arguments": json.dumps({"path": "clock/index.html", "content": "v1"})}}
    current = {"id": "new", "type": "function", "function": {
        "name": "write", "arguments": json.dumps({"path": "clock/index.html", "content": "v1"})}}
    messages = [{"role": "user", "content": "Build the clock"},
                {"role": "assistant", "tool_calls": [first]},
                {"role": "tool", "tool_call_id": "old", "content": "wrote clock/index.html"}]
    exact, target = tools.repeated_successful_actions([current], messages)
    assert exact == [current] and not target


def test_answered_question_is_not_emitted_again_with_a_new_call_id():
    first = {"id": "old", "type": "function", "function": {
        "name": "question", "arguments": json.dumps({"question": "Qaysi dizayn uslubini yoqtirasiz?"})}}
    current = {"id": "new", "type": "function", "function": {
        "name": "question", "arguments": json.dumps({"question": "Qaysi dizayn uslubini yoqtirasiz?"})}}
    messages = [{"role": "user", "content": "Build a page"},
                {"role": "assistant", "tool_calls": [first]},
                {"role": "tool", "tool_call_id": "old", "content": "Oq va minimalist"}]
    exact, target = tools.repeated_successful_actions([current], messages)
    assert exact == [current] and not target


def test_answered_question_with_changed_options_is_still_not_repeated():
    first = {"id": "old", "type": "function", "function": {
        "name": "question", "arguments": json.dumps({
            "questions": [{"question": "Qaysi dizayn uslubini yoqtirasiz?",
                            "options": ["Minimalist", "Rangli"]}]})}}
    current = {"id": "new", "type": "function", "function": {
        "name": "question", "arguments": json.dumps({
            "questions": [{"question": "Qaysi dizayn uslubini yoqtirasiz?",
                            "options": ["Oq va minimalist", "To'q va yorqin"],
                            "header": "Design"}]})}}
    messages = [{"role": "user", "content": "Build a page"},
                {"role": "assistant", "tool_calls": [first]},
                {"role": "tool", "tool_call_id": "old", "content": "Oq va minimalist"}]
    exact, target = tools.repeated_successful_actions([current], messages)
    assert exact == [current] and not target


def test_successful_mutation_target_limit_catches_changing_loop_and_allows_failures():
    messages = [{"role": "user", "content": "Build the clock"}]
    for i in range(tools.MAX_SUCCESSFUL_TARGET_ACTIONS):
        call = {"id": f"old-{i}", "type": "function", "function": {
            "name": "write", "arguments": json.dumps({"path": "clock/index.html", "content": f"v{i}"})}}
        messages.extend([{ "role": "assistant", "tool_calls": [call]},
                         {"role": "tool", "tool_call_id": f"old-{i}", "content": "ok"}])
    current = {"id": "new", "type": "function", "function": {
        "name": "write", "arguments": json.dumps({"path": "clock/index.html", "content": "new"})}}
    exact, target = tools.repeated_successful_actions([current], messages)
    assert not exact and target == [current]
    messages[-1]["content"] = "Error: permission denied"
    exact, target = tools.repeated_successful_actions([current], messages)
    assert not exact and not target  # a failed result remains retryable


def test_mutation_limit_applies_across_write_and_edit_tools():
    messages = [{"role": "user", "content": "Build the clock"}]
    for i, name in enumerate(("write", "edit", "write")):
        call = {"id": f"old-{i}", "type": "function", "function": {
            "name": name, "arguments": json.dumps({"path": "clock/index.html", "content": f"v{i}"})}}
        messages.extend([{ "role": "assistant", "tool_calls": [call]},
                         {"role": "tool", "tool_call_id": f"old-{i}", "content": "ok"}])
    current = {"id": "new", "type": "function", "function": {
        "name": "edit", "arguments": json.dumps({"path": "clock/index.html", "content": "new"})}}
    exact, target = tools.repeated_successful_actions([current], messages)
    assert not exact and target == [current]


def test_empty_tool_result_is_preserved_and_repeat_not_emitted(cluster_client):
    first = _opening('open ~/Desktop/clock/index.html')
    messages = [{"role":"user","content":"Open the clock"},
        {"role":"assistant","tool_calls":[first]}, {"role":"tool","tool_call_id":"opened","content":""}]
    raw = json.dumps({"actions":[{"name":"shell","arguments":{"command":"open -a Safari ~/Desktop/clock/index.html"}}]})
    response, bal = cluster_client([raw], {"messages":messages,"tools":[{"type":"function","function":{"name":"shell"}}]})
    assert response.status_code == 200 and len(bal.prompts) == 1
    assert "Tool returned no text" in bal.prompts[0]
    assert response.json()["choices"][0]["finish_reason"] == "stop"
    assert "tool_calls" not in response.json()["choices"][0]["message"]


def test_same_batch_keeps_one_open_and_preserves_other_work():
    first = _opening('open ~/Desktop/clock/index.html')
    other = _opening('ls ~/Desktop/clock')
    assert tools.unique_open_calls([first, _opening('open -a Safari ~/Desktop/clock/index.html'), other]) == [first, other]


def test_same_batch_deduplicates_question_calls_with_new_ids():
    def question(cid, text):
        return {"id": cid, "type": "function", "function": {
            "name": "question", "arguments": json.dumps({"question": text})}}

    first = question("one", "Qaysi dizayn uslubini yoqtirasiz?")
    duplicate = question("two", "Qaysi dizayn uslubini yoqtirasiz?")
    next_question = question("three", "Qaysi rangni tanlaysiz?")
    assert tools.unique_tool_calls([first, duplicate, next_question]) == [first, next_question]


def test_gateway_emits_one_duplicate_question_call(cluster_client):
    raw = json.dumps({"actions": [
        {"name": "question", "arguments": {"question": "Qaysi dizayn uslubini yoqtirasiz?"}},
        {"name": "question", "arguments": {"question": "Qaysi dizayn uslubini yoqtirasiz?"}},
    ]})
    response, _ = cluster_client([raw], {"messages": [{"role": "user", "content": "Build a page"}],
                                    "tools": [{"type": "function", "function": {"name": "question"}}]})
    assert response.status_code == 200
    calls = response.json()["choices"][0]["message"]["tool_calls"]
    assert len(calls) == 1
    assert response.json()["gateway"]["tool_protocol"]["deduped_calls"] == 1


def test_closing_stream_after_initial_role_cancels_worker_job():
    async def check():
        released = asyncio.Event()
        async def job():
            try:
                await asyncio.Event().wait()
            finally:
                released.set()
        task = asyncio.create_task(job())
        await asyncio.sleep(0)
        stream = server._stream_with_keepalive(task, "x", 1, "m")
        assert '"role": "assistant"' in await anext(stream)
        await stream.aclose()
        assert task.cancelled() and released.is_set()
    asyncio.run(check())


def test_qwen_image_mode_is_never_reused_for_text(monkeypatch):
    from cluster.adapters.qwen_adapter import QwenAdapter
    from cluster import cdp
    async def evaluate(ws, js, **kw):
        return {"hasInput": True, "newPath": True, "msgs": 0, "imageMode": True}
    monkeypatch.setattr(cdp, "evaluate", evaluate)
    assert not asyncio.run(QwenAdapter().is_fresh_chat(None))


def test_qwen_image_mode_does_not_select_a_text_model(monkeypatch):
    from cluster.adapters.qwen_adapter import QwenAdapter
    from cluster import cdp
    async def evaluate(ws, js, **kw):
        assert 'Describe the image' in js
        return True
    monkeypatch.setattr(cdp, "evaluate", evaluate)
    assert asyncio.run(QwenAdapter().select_variant(None, None)) == "Qwen-Image 3.0"


def test_failed_image_reset_never_falls_through_to_send():
    class Adapter:
        async def reset_chat_for_images(self, ws):
            raise RuntimeError("image mode not selected")
    class Connection:
        async def __aenter__(self): return None
        async def __aexit__(self, *args): pass
    tab = object.__new__(pool.WorkerTab)
    tab.adapter = Adapter()
    async def connect(): return Connection()
    tab.connect = connect
    with pytest.raises(RuntimeError, match="image mode not selected"):
        asyncio.run(tab._reset_phase(for_images=True))


def test_qwen_cannot_silently_use_text_model_for_image_request(monkeypatch):
    from cluster.adapters.qwen_adapter import QwenAdapter
    from cluster import cdp
    async def evaluate(ws, js, **kw): return False
    monkeypatch.setattr(cdp, "evaluate", evaluate)
    async def check():
        runmeta.start(image_requested=True)
        with pytest.raises(RuntimeError, match="image mode was not confirmed"):
            await QwenAdapter().select_variant(None, None)
    asyncio.run(check())


def test_parallel_image_mode_setup_cannot_steal_same_account_focus():
    async def check():
        active = 0
        peak = 0
        class Adapter:
            async def reset_chat_for_images(self, ws):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.01)
                active -= 1
        class Connection:
            async def __aenter__(self): return None
            async def __aexit__(self, *args): pass
        async def connect(): return Connection()
        tabs = []
        for _ in range(2):
            tab = object.__new__(pool.WorkerTab)
            tab.adapter = Adapter()
            tab.model_name, tab.browser_port = "qwen", 19777
            tab.connect = connect
            tabs.append(tab)
        await asyncio.gather(*(t._reset_phase(for_images=True) for t in tabs))
        assert peak == 1
    asyncio.run(check())
