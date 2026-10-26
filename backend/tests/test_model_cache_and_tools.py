"""Cross-model cache isolation and bounded tool repair, including SSE."""
import json
import pytest
from fastapi.testclient import TestClient

from app import attribution, catalog
from cluster import server as srv, metrics, cache
from cluster.adapters import get_adapter
from .test_audit_regressions import cluster_client, _FakeBalancer, MSG

TOOLS = [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}]
CALL = '```yaml\nactions:\n  - name: read_file\n    arguments:\n      path: /workspace/probe.txt\n```'


def test_public_model_and_policy_isolate_cache_entries(cluster_client):
    first, _ = cluster_client(["first"], {"model": "gemini", "sf_public_model": "small", "messages": MSG})
    assert not first.json()["cached"]
    second, bal = cluster_client(["second"], {"model": "gemini", "sf_public_model": "large", "messages": MSG})
    assert not second.json()["cached"] and len(bal.prompts) == 1
    replay, bal = cluster_client([], {"model": "gemini", "sf_public_model": "small", "messages": MSG})
    assert replay.json()["cached"] and replay.json()["choices"][0]["message"]["content"] == "first" and not bal.prompts
    changed, _ = cluster_client(["changed"], {"model": "gemini", "sf_public_model": "small", "sf_model_policy": {"allow_thinking": 1}, "messages": MSG})
    assert not changed.json()["cached"]


@pytest.mark.parametrize("stream", [False, True])
def test_tool_retry_recovered_and_observable(cluster_client, monkeypatch, stream):
    measured = metrics.Metrics()
    monkeypatch.setattr(metrics, "_instance", measured)
    response, bal = cluster_client(["Let me read that.", CALL], {"messages": MSG, "tools": TOOLS, "tool_choice": "required", "stream": stream})
    assert response.status_code == 200 and len(bal.prompts) == 2
    if stream:
        events = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:") and "[DONE]" not in line]
        result = next(e for e in events if (e.get("gateway") or {}).get("tool_protocol"))
        assert any((c.get("delta") or {}).get("tool_calls") for e in events for c in e.get("choices", []))
    else:
        result = response.json()
        assert result["choices"][0]["finish_reason"] == "tool_calls"
    meta = {}
    attribution.observe(meta, result)
    assert meta["tool_protocol"]["retried"] and meta["tool_protocol"]["recovered"]
    stats = measured.snapshot()["per_model"][0]["tool_protocol"]
    assert (stats["requests"], stats["retries"], stats["recovered"], stats["missing_calls"]) == (1, 1, 1, 0)


def test_required_tool_missing_twice_is_failure_and_not_cached(cluster_client):
    response, bal = cluster_client(["I will read it.", "Cannot read it."], {"messages": MSG, "tools": TOOLS, "tool_choice": "required"})
    assert response.status_code == 502 and response.json()["error"]["code"] == "tool_call_required"
    assert len(bal.prompts) == 2
    retry, _ = cluster_client([CALL], {"messages": MSG, "tools": TOOLS, "tool_choice": "required"})
    assert retry.status_code == 200 and not retry.json()["cached"]


def test_forced_tool_cannot_be_replaced_with_another_allowed_tool(cluster_client):
    tools = TOOLS + [{"type": "function", "function": {"name": "write_file"}}]
    response, _ = cluster_client([CALL, CALL], {"messages": MSG, "tools": tools, "tool_choice": {"type": "function", "function": {"name": "write_file"}}})
    assert response.status_code == 502


def test_auto_final_answer_preserved_when_repair_fails(cluster_client):
    response, bal = cluster_client(["The task is finished.", "Still finished."], {"messages": MSG, "tools": TOOLS})
    assert response.status_code == 200 and len(bal.prompts) == 2
    assert response.json()["choices"][0]["message"]["content"] == "The task is finished."
    assert response.json()["gateway"]["tool_protocol"]["missing_calls"]


def test_cached_response_keeps_cache_attribution_in_sse(cluster_client):
    cluster_client(["reply"], {"messages": MSG})
    response, bal = cluster_client([], {"messages": MSG, "stream": True})
    assert not bal.prompts
    meta = {}
    for line in response.text.splitlines():
        if line.startswith("data:") and "[DONE]" not in line:
            attribution.observe(meta, json.loads(line[5:]))
    assert meta["cache_hit"] is True


def test_chatgpt_capability_uses_client_protocol():
    assert get_adapter("chatgpt").caps.tool_calling
    assert catalog._cluster_caps("chatgpt")["tool_calling"]
    from cluster.tools import build_instruction
    text = build_instruction(TOOLS, "required")
    assert "tashqi API mijoz-agent" in text and "bajarilganini" in text


def test_cached_rows_do_not_distort_monitoring_latency(env):
    from app import monitor, billing
    from .conftest import add_key, add_user
    uid = add_user(env)
    _, kid = add_key(env, uid)
    for rid, latency, hit in [("fresh", 12000, False), ("cached1", 17, True), ("cached2", 41, True)]:
        with env.db.tx() as c:
            c.execute("""INSERT INTO usage_logs(request_id,user_id,key_id,model,endpoint,prompt_tokens,completion_tokens,cost,
                status_code,stream,estimated,latency_ms,created_at,attribution) VALUES (?,?,?,'sable-3.5','chat/completions',1,1,0,200,0,0,?,1,?)""",
                (rid, uid, kid, latency, json.dumps({"cache_hit": hit})))
    quality = monitor.quality(env.db, 0)
    assert quality["requests"] == 3 and quality["latency_p50"] == 12000
    assert quality["cache_hits_in_latency_sample"] == 2 and quality["latency_samples"] == 1


@pytest.mark.parametrize("stream", [False, True])
def test_explicit_final_after_tool_result_skips_unnecessary_retry(cluster_client, stream):
    messages = [*MSG, {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "call_1", "name": "read_file", "content": "probe-code"}]
    response, bal = cluster_client(['```yaml\nfinal_answer: |\n  probe-code\n```'], {"messages": messages, "tools": TOOLS, "stream": stream})
    assert response.status_code == 200 and len(bal.prompts) == 1
    assert "probe-code" in response.text and "final_answer:" not in response.text
    if not stream:
        status = response.json()["gateway"]["tool_protocol"]
        assert status["final_answer"] and not status["retried"] and not status["missing_calls"]


def test_repeated_successful_tool_call_ends_agent_loop(cluster_client):
    previous = {"id": "call_old", "type": "function", "function": {
        "name": "read_file", "arguments": json.dumps({"path": "/workspace/probe.txt"})}}
    messages = [{"role": "user", "content": "Read the file"},
                {"role": "assistant", "content": None, "tool_calls": [previous]},
                {"role": "tool", "tool_call_id": "call_old", "name": "read_file", "content": "probe-code"}]
    response, bal = cluster_client([CALL], {"messages": messages, "tools": TOOLS})
    assert response.status_code == 200 and len(bal.prompts) == 1
    assert "tool_calls" not in response.json()["choices"][0]["message"]
    status = response.json()["gateway"]["tool_protocol"]
    assert status["loop_guard"] == "repeated_tool_action" and status["final_answer"]


def test_final_marker_respects_required_and_auto_semantics(cluster_client):
    marker = '```yaml\nfinal_answer: done\n```'
    required, bal = cluster_client([marker, marker], {"messages": MSG, "tools": TOOLS, "tool_choice": "required"})
    assert required.status_code == 502 and len(bal.prompts) == 2
    auto, bal = cluster_client([marker], {"messages": MSG, "tools": TOOLS})
    assert auto.status_code == 200 and len(bal.prompts) == 1
    assert auto.json()["choices"][0]["message"]["content"] == "done"
    future = '```yaml\nfinal_answer: I will read the file now.\n```'
    action, bal = cluster_client([future, CALL], {"messages": [{"role":"user","content":"Read the new file"}], "tools": TOOLS})
    assert action.status_code == 200 and len(bal.prompts) == 2
    assert action.json()["choices"][0]["finish_reason"] == "tool_calls"


@pytest.mark.parametrize("text", ['final_answer: 42', 'final_answer: ""', 'final_answer: done\nactions: []', 'prefix\n```yaml\nfinal_answer: done\n```', 'final_answer: !!python/object:builtins.object {}'])
def test_ambiguous_final_marker_is_not_accepted(text):
    from cluster.tools import parse_final_answer
    assert parse_final_answer(text) is None


def test_tool_stream_uses_short_preflight(cluster_client, monkeypatch):
    from cluster import settings as cfg
    original = srv.asyncio.wait
    timeouts = []
    async def tracked(*args, **kwargs):
        timeouts.append(kwargs.get("timeout"))
        return await original(*args, **kwargs)
    monkeypatch.setattr(srv.asyncio, "wait", tracked)
    monkeypatch.setattr(cfg, "TOOL_PREFLIGHT_SECONDS", .02)
    response, _ = cluster_client([(.08, CALL)], {"messages": MSG, "tools": TOOLS, "stream": True})
    assert response.status_code == 200 and timeouts[0] == .02


@pytest.mark.parametrize("fenced", [False, True])
def test_native_json_tool_envelope_does_not_require_repair(cluster_client, fenced):
    raw = json.dumps({"tool_calls": [{"type": "function", "function": {"name": "read_file", "arguments": json.dumps({"path": "/workspace/probe.txt"})}}]})
    if fenced:
        raw = '```json\n' + raw + '\n```'
    response, bal = cluster_client([raw], {"messages": MSG, "tools": TOOLS, "tool_choice": "required"})
    assert response.status_code == 200 and len(bal.prompts) == 1
    assert response.json()["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "read_file"


def test_none_choice_never_emits_model_invented_tool_call(cluster_client):
    response, bal = cluster_client([CALL], {"messages": MSG, "tools": TOOLS, "tool_choice": "none"})
    assert response.status_code == 200 and len(bal.prompts) == 1
    assert "tool_calls" not in response.json()["choices"][0]["message"]


@pytest.mark.parametrize("arguments", ['{"path": }', ["not-an-object"], 42])
def test_invalid_arguments_are_repaired_before_reaching_client(cluster_client, arguments):
    invalid = json.dumps({"actions": [{"name": "read_file", "arguments": arguments}]})
    response, bal = cluster_client([invalid, CALL], {"messages": MSG, "tools": TOOLS, "tool_choice": "required"})
    assert response.status_code == 200 and len(bal.prompts) == 2
    args = response.json()["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
    assert json.loads(args) == {"path": "/workspace/probe.txt"}


@pytest.mark.parametrize("stream", [False, True])
def test_tool_arguments_count_as_completion_tokens(cluster_client, stream):
    content = "print('hello')\n" * 400
    raw = json.dumps({"actions": [{"name": "write_file", "arguments": {"content": content}}]})
    response, _ = cluster_client([raw], {"messages": MSG, "tools": [{"type": "function", "function": {"name": "write_file"}}], "stream": stream})
    if stream:
        events = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:") and "[DONE]" not in line]
        usage = next(e["usage"] for e in events if "usage" in e)
    else:
        usage = response.json()["usage"]
    assert usage["completion_tokens"] > 1000


# --- a re-ask the requested model cannot serve (2026-10-25) -----------------
# qwen answered an opencode turn with a half-written call; both qwen tabs then
# timed out on the re-ask and the broken text went to the agent as its answer.
from cluster.balancer import UpstreamFailed
from .test_tool_retry import BROKEN


class _ByModel(_FakeBalancer):
    """Answers per requested model, and records which model each ask went to."""

    def __init__(self, answers):
        super().__init__([])
        self.by_model, self.models = {m: list(a) for m, a in answers.items()}, []

    async def execute(self, prompt_text, requested_model=None, **kw):
        self.models.append(requested_model)
        self.answers = self.by_model[requested_model]
        return await super().execute(prompt_text, **kw)


def _ask(monkeypatch, answers, **extra):
    monkeypatch.setattr(srv.cfg, "API_KEY", "")
    srv.app.state.balancer = bal = _ByModel(answers)
    body = {"model": "qwen", "messages": MSG, "tools": TOOLS, **extra}
    return TestClient(srv.app).post("/v1/chat/completions", json=body), bal


def test_stand_in_answers_the_reask_the_model_could_not(cluster_client, monkeypatch):
    # Prose first, then every qwen tab fails the re-ask: the stand-in answers it.
    response, bal = _ask(monkeypatch, {"qwen": ["Let me read it.", UpstreamFailed("timeout")], "deepseek": [CALL]},
                         sf_stand_in="deepseek")
    assert response.status_code == 200 and bal.models == ["qwen", "qwen", "deepseek"]
    message = response.json()["choices"][0]["message"]
    assert json.loads(message["tool_calls"][0]["function"]["arguments"]) == {"path": "/workspace/probe.txt"}


def test_broken_call_goes_straight_to_the_stand_in(cluster_client, monkeypatch):
    # Same-model re-asks of a broken call broke it again every time (2026-10-25)
    response, bal = _ask(monkeypatch, {"qwen": [BROKEN], "deepseek": [CALL]}, sf_stand_in="deepseek")
    assert response.status_code == 200 and bal.models == ["qwen", "deepseek"]
    assert "read_file" in response.text


def test_broken_call_without_a_stand_in_is_re_asked_on_the_same_model(cluster_client, monkeypatch):
    response, bal = _ask(monkeypatch, {"qwen": [BROKEN, CALL]})
    assert response.status_code == 200 and bal.models == ["qwen", "qwen"]


def test_broken_call_never_reaches_the_agent_as_text(cluster_client, monkeypatch):
    # No stand-in configured and the re-ask fails: an error, not the garbage.
    response, bal = _ask(monkeypatch, {"qwen": [BROKEN, UpstreamFailed("timeout")]})
    assert response.status_code == 502 and response.json()["error"]["code"] == "invalid_tool_call"
    assert bal.models == ["qwen", "qwen"]


def test_prose_answer_does_not_wake_the_stand_in(cluster_client, monkeypatch):
    # A text answer may be the final word; asking a second model each time would
    # only add delay. The stand-in is for a failed re-ask or a broken call.
    response, bal = _ask(monkeypatch, {"qwen": ["The task is finished.", "Still finished."], "deepseek": [CALL]},
                         sf_stand_in="deepseek")
    assert response.status_code == 200 and bal.models == ["qwen", "qwen"]
    assert response.json()["choices"][0]["message"]["content"] == "The task is finished."


@pytest.mark.parametrize("stream", [False, True])
def test_stand_in_answers_when_the_requested_model_cannot(cluster_client, monkeypatch, stream):
    # 2026-10-25: a Qwen tab failed on a half-loaded page and the stream ended in
    # "The model did not return an answer" while the stand-in sat idle.
    response, bal = _ask(monkeypatch, {"qwen": [UpstreamFailed("timeout")], "deepseek": [CALL]},
                         sf_stand_in="deepseek", stream=stream)
    assert response.status_code == 200 and bal.models == ["qwen", "deepseek"]
    assert "read_file" in response.text and "upstream_failed" not in response.text


def test_unsupported_feature_is_not_retried_on_the_stand_in(cluster_client, monkeypatch):
    from cluster.balancer import Unsupported
    response, bal = _ask(monkeypatch, {"qwen": [Unsupported("no")], "deepseek": [CALL]}, sf_stand_in="deepseek")
    assert response.status_code == 400 and bal.models == ["qwen"]
