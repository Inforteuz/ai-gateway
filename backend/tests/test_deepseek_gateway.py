"""Gateway -> actual cluster routing -> direct worker, with only DeepSeek HTTP mocked."""
import base64
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app import catalog
from app.routes_admin import _check_model_rules
from app.setup_cli import CLUSTER_CHANNELS
from cluster import server as cluster_server, settings as cfg
from cluster.adapters import get_adapter
from cluster.balancer import ClusterBalancer
from cluster.cache import ResponseCache
from cluster.pool import ClusterPool, DeepSeekDirectWorker
from cluster.compose import OVERFLOW_NOTE, OVERFLOW_FILE_NAME

from .conftest import add_key, add_model, add_user, bearer


def test_cached_reasoning_survives_replay_and_respects_entry_size(monkeypatch):
    cache = ResponseCache()
    cache.put("r1", "Answer", model="deepseek-reasoner", reasoning_content="Reasoning")
    entry = cache.get("r1")
    assert entry.answer == "Answer" and entry.reasoning_content == "Reasoning"
    monkeypatch.setattr(cfg, "CACHE_MAX_ENTRY_BYTES", 12)
    cache.put("too-big", "Answer", reasoning_content="Reasoning")
    assert cache.get("too-big") is None


def test_existing_model_keeps_identity_and_prices(env):
    add_model(env, "existing-deepseek", provider="cluster", upstream="deepseek")
    before = dict(env.db.one("SELECT * FROM models WHERE id='existing-deepseek'"))
    env.db._init(False, None)
    after = dict(env.db.one("SELECT * FROM models WHERE id='existing-deepseek'"))
    assert before == after
    assert env.db.scalar("SELECT COUNT(*) FROM models WHERE id IN ('deepseek-chat','deepseek-reasoner')") == 0
    key, _ = add_key(env, add_user(env))
    response = env.client.get("/v1/models", headers=bearer(key))
    assert response.status_code == 200
    model = next(m for m in response.json()["data"] if m["id"] == "existing-deepseek")
    assert all(model["capabilities"][c] for c in ("vision", "file_input", "files", "tool_calling"))


def test_reasoner_alias_uses_direct_worker_health(env):
    add_model(env, "sable-3.7", upstream="deepseek-reasoner", provider="cluster")
    with env.db.tx() as c:
        c.execute("INSERT INTO model_health(upstream,state,since,last_check) VALUES ('deepseek','up',1,1)")
    response = env.client.get('/portal/status/models')
    assert response.status_code == 200
    items = response.json()["items"]
    assert next(m for m in items if m["model"] == "sable-3.7")["state"] == "up"


@pytest.mark.parametrize("alias", ["deepseek-chat", "deepseek-reasoner", "deepseek-v3", "deepseek-r1"])
def test_aliases_and_capabilities_without_cluster_package(alias, monkeypatch):
    assert get_adapter(alias.upper()) is get_adapter("deepseek")
    assert alias in CLUSTER_CHANNELS
    _check_model_rules(None, "cluster", False, None, "test", alias)
    # API Docker image deliberately excludes cluster/.
    import sys
    monkeypatch.setitem(sys.modules, "cluster.adapters", None)
    assert catalog._cluster_caps(alias) == {"vision": True, "file_input": True, "files": True,
                                           "image_generation": False, "tool_calling": True}


@pytest.fixture
def direct_gateway(env, monkeypatch):
    calls, uploads, deleted = [], [], []
    worker = DeepSeekDirectWorker("mock-token")

    def stream_chat(**kw):
        calls.append(kw)
        if kw["thinking"]:
            yield {"type": "thinking", "text": "Avval hisoblayman. "}
            yield {"type": "thinking", "text": "Natija tayyor."}
        yield {"type": "content", "text": "Salom! "}
        yield {"type": "content", "text": "Yaxshiman."}

    def upload(path):
        uploads.append(Path(path).read_bytes())
        return f"file-{len(uploads)}"

    worker._client = SimpleNamespace(create_chat_session=lambda: "session-test", stream_chat=stream_chat,
                                     delete_chat_session=deleted.append, upload_file=upload)
    pool = ClusterPool()
    pool.workers = [worker]

    async def scan(force=False):
        pass

    monkeypatch.setattr(pool, "scan", scan)
    monkeypatch.setattr(cfg, "CACHE_ENABLED", False)
    monkeypatch.setattr(cfg, "REASONING_YAML", False)
    monkeypatch.setattr(cfg, "LIVE_STREAM", True)
    monkeypatch.setattr(cfg, "DEEPSEEK_AUTO_CLEANUP", True)
    monkeypatch.setattr(cfg, "API_KEY", "cluster-test-key")
    monkeypatch.setattr(cluster_server.app.state, "pool", pool, raising=False)
    monkeypatch.setattr(cluster_server.app.state, "balancer", ClusterBalancer(pool), raising=False)
    env.db.set_setting("cluster_base_url", "http://cluster/v1")
    env.db.set_setting("cluster_api_key", "cluster-test-key")
    env.app.state.cluster_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=cluster_server.app))
    add_model(env, "deepseek-chat", upstream="deepseek-chat", provider="cluster", allow_thinking=0, price_in=0, price_out=0)
    add_model(env, "deepseek-reasoner", upstream="deepseek-reasoner", provider="cluster", allow_thinking=1, price_in=0, price_out=0)
    key, _ = add_key(env, add_user(env))
    return env, key, calls, uploads, deleted


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("model", ["deepseek-chat", "deepseek-reasoner"])
def test_gateway_completion_reasoning_and_attachments(direct_gateway, stream, model):
    env, key, calls, uploads, deleted = direct_gateway
    text = b"Document test"
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")
    parts = [{"type": "text", "text": "Salom, qandaysiz?"},
             {"type": "file", "file": {"filename": "note.txt", "file_data": "data:text/plain;base64," + base64.b64encode(text).decode()}},
             {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}}]
    response = env.client.post("/v1/chat/completions", headers=bearer(key),
                               json={"model": model, "stream": stream, "messages": [{"role": "user", "content": parts}]})
    assert response.status_code == 200, response.text
    if stream:
        assert response.text.rstrip().endswith("[DONE]")
        events = [json.loads(l[5:]) for l in response.text.splitlines() if l.startswith("data:") and "[DONE]" not in l]
        assert all(e.get("model", model) == model for e in events)
        deltas = [e["choices"][0]["delta"] for e in events if e.get("choices")]
        answer = "".join(d.get("content", "") for d in deltas)
        reasoning = "".join(d.get("reasoning_content", "") for d in deltas)
    else:
        data = response.json()
        assert data["model"] == model
        answer = data["choices"][0]["message"]["content"]
        reasoning = data["choices"][0]["message"].get("reasoning_content", "")
    assert answer == "Salom! Yaxshiman."
    assert reasoning == ("Avval hisoblayman. Natija tayyor." if model == "deepseek-reasoner" else "")
    assert calls[0]["thinking"] is (model == "deepseek-reasoner")
    assert calls[0]["ref_file_ids"] == ["file-1", "file-2"]
    assert uploads == [text, png] and deleted == ["session-test"]
    assert env.up.get("calls", 0) == 0


def test_gateway_keeps_cli_supplied_history_when_model_changes(direct_gateway):
    env, key, calls, _uploads, _deleted = direct_gateway
    response = env.client.post("/v1/chat/completions", headers=bearer(key), json={
        "model": "deepseek-reasoner",
        "messages": [
            {"role": "user", "content": "Mening ismim Behzod."},
            {"role": "assistant", "content": "Tushunarli, Behzod."},
            {"role": "user", "content": "Ismim nima?"},
        ],
    })
    assert response.status_code == 200, response.text
    assert "Behzod" in calls[0]["prompt"]
    assert "Ismim nima?" in calls[0]["prompt"]


def test_direct_worker_moves_long_prompt_to_context_file_and_cleans_it(monkeypatch):
    worker = DeepSeekDirectWorker("mock-token")
    worker.adapter.MAX_INPUT_CHARS = 24000
    sent, uploaded, deleted = {}, [], []
    monkeypatch.setattr(cfg, "OVERFLOW_TO_FILE", True)
    monkeypatch.setattr(cfg, "DEEPSEEK_AUTO_CLEANUP", True)

    def upload(path):
        uploaded.append((Path(path).name, Path(path).read_text()))
        return "context-file"

    def stream_chat(**kw):
        sent.update(kw)
        yield {"type": "content", "text": "ok"}

    worker._client = SimpleNamespace(create_chat_session=lambda: "session", upload_file=upload,
                                     stream_chat=stream_chat, delete_chat_session=deleted.append)
    prompt = "[System]:\n" + "s" * 25000 + "\n\n[Current task]:\nDo this"
    answer, _ = asyncio.run(worker.execute_prompt(prompt))

    assert answer == "ok"
    assert len(sent["prompt"]) < 1000 and sent["prompt"].startswith(OVERFLOW_NOTE)
    assert uploaded[0][0] == OVERFLOW_FILE_NAME and "s" * 1000 in uploaded[0][1]
    assert deleted == ["session"]


def test_direct_worker_puts_rate_limited_response_into_cooldown(monkeypatch):
    worker = DeepSeekDirectWorker("mock-token")
    monkeypatch.setattr(cfg, "DEEPSEEK_AUTO_CLEANUP", False)

    def stream_chat(**_kw):
        raise RuntimeError("Server xatosi: HTTP 429")
        yield  # pragma: no cover

    worker._client = SimpleNamespace(create_chat_session=lambda: "session", stream_chat=stream_chat)
    with pytest.raises(RuntimeError, match="429"):
        asyncio.run(worker.execute_prompt("salom"))
    assert worker.cooldown_reason == "rate_limit"
    assert worker.cooldown_until > 0
