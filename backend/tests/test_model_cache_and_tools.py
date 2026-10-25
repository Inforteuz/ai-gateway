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
