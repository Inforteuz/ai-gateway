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










def test_cached_response_keeps_cache_attribution_in_sse(cluster_client):
    cluster_client(["reply"], {"messages": MSG})
    response, bal = cluster_client([], {"messages": MSG, "stream": True})
    assert not bal.prompts
    meta = {}
    for line in response.text.splitlines():
        if line.startswith("data:") and "[DONE]" not in line:
            attribution.observe(meta, json.loads(line[5:]))
    assert meta["cache_hit"] is True




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
