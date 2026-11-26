"""Behavioral regressions from the 2026-10-27 audit; no live provider claims."""
import asyncio
import base64
import json
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app import catalog, local_guard, routing
from cluster import attachments as A, pool, runmeta, server, tools
from cluster.adapters import get_adapter
from cluster.adapters.gemini_adapter import GeminiAdapter
from .conftest import add_key, add_model, add_user, bearer, wallet
from .test_audit_regressions import _FakeBalancer, cluster_client
from .test_routing_policy import model, body
from .test_gateway_improvements import Stream


READ_TOOLS = [{"type": "function", "function": {"name": "read_file"}}]


@pytest.mark.parametrize("prompt", [
    "Davom et", "Tuzat", "Continue", "Продолжай", "Sayt yaratib ber",
    "Barcha xatolarni tuzatib chiq", "Исправь ошибки и добавь авторизацию",
    "Open the project and optimize all files", "Read the file and implement the fix",
])
def test_short_complex_and_continuation_tasks_are_not_silently_downgraded(prompt):
    assert routing.tool_target(model("full", allow_thinking=1),
        [model("fast", "qwen", allow_thinking=0)], body(prompt, tools=READ_TOOLS)) is None


@pytest.mark.parametrize("prompt", ["Read /tmp/probe.txt", "index.html faylini och", "Прочитай файл app.py"])
def test_unambiguous_simple_requests_still_have_fast_route(prompt):
    assert routing.tool_target(model("full", allow_thinking=1),
        [model("fast", "qwen", allow_thinking=0)], body(prompt, tools=READ_TOOLS))


@pytest.mark.parametrize("effort", ["medium", "high", "xhigh"])
def test_explicit_effort_is_preserved_by_optional_speed_route(effort):
    assert routing.tool_target(model("full", allow_thinking=1),
        [model("fast", "qwen", allow_thinking=0)],
        body("Read app.py", tools=READ_TOOLS, reasoning_effort=effort)) is None


def test_active_tool_history_keeps_requested_route():
    request = body("Read app.py", tools=READ_TOOLS)
    request["messages"] += [{"role": "assistant", "tool_calls": [{"id": "one"}]},
                            {"role": "tool", "tool_call_id": "one", "content": "x" * 50000}]
    assert routing.tool_target(model("full", allow_thinking=1),
        [model("fast", "qwen", allow_thinking=0)], request) is None


def test_optional_speed_route_excludes_known_down_and_thinking_targets():
    source = model("full", allow_thinking=1)
    request = body("Read app.py", tools=READ_TOOLS)
    for target in (model("down", "qwen", allow_thinking=0, route_health="down"),
                   model("thinking", "qwen", allow_thinking=1)):
        assert routing.tool_target(source, [source, target], request) is None


def test_gateway_speed_route_obeys_recorded_provider_health(env):
    from .test_routing_policy import setup
    seen, _, key, _ = setup(env)
    add_model(env, "full", provider="cluster", upstream="gemini", allow_thinking=1)
    add_model(env, "down-fast", provider="cluster", upstream="qwen", allow_thinking=0)
    with env.db.tx() as conn:
        conn.execute("INSERT INTO model_health(upstream,state,since,last_check) VALUES('qwen','down',1,1)")
    response = env.client.post("/v1/chat/completions", headers=bearer(key),
                              json={"model": "full", **body("Read app.py", tools=READ_TOOLS)})
    assert response.status_code == 200 and seen[0]["model"] == "gemini"
    assert "x-sf-route-reason" not in response.headers


def test_gemini_effort_is_set_on_each_account_and_after_navigation(monkeypatch):
    pages = []
    async def evaluate(ws, js, **kw):
        if "Выбор режима" in js:
            pages.append(ws)
        return False if js.startswith("!!document") else {"x": 1, "y": 2}
    async def click(*args):
        pass
    async def sleep(*args):
        pass
    monkeypatch.setattr("cluster.adapters.gemini_adapter.cdp.evaluate", evaluate)
    monkeypatch.setattr("cluster.adapters.gemini_adapter.cdp.click_at", click)
    monkeypatch.setattr("cluster.adapters.gemini_adapter.asyncio.sleep", sleep)
    async def check():
        shared = GeminiAdapter()
        for ws in ("account-a", "account-b", "account-a-new-page"):
            assert await shared.set_effort(ws, "low") == "low"
    asyncio.run(check())
    assert pages == ["account-a", "account-b", "account-a-new-page"]


@pytest.mark.parametrize("provider", ["qwen", "gemini", "chatgpt", "deepseek"])
def test_failed_clean_chat_verification_prevents_prompt_send(provider):
    sent = []
    @asynccontextmanager
    async def connection():
        yield object()
    async def connect(**kw):
        return connection()
    async def no(*args, **kw):
        return False
    async def noop(*args, **kw):
        pass
    async def send(*args, **kw):
        sent.append(True)
    adapter = SimpleNamespace(wait_for_input=noop, verify_clean_chat=no, send_prompt=send)
    worker = pool.WorkerTab(19900, "audit", provider, "ws://unused", adapter)
    worker.connect, worker._reuse_clean_chat, worker._reset_phase = connect, no, noop
    worker._lookup_ws_url = lambda: worker.ws_url
    async def check():
        runmeta.start()
        await worker._run_once("synthetic probe", 5)
    with pytest.raises(RuntimeError, match="isolation check failed"):
        asyncio.run(check())
    assert not sent


def test_reset_failure_is_not_swallowed_for_text_requests():
    @asynccontextmanager
    async def connection():
        yield object()
    async def connect(**kw):
        return connection()
    async def reset(*args):
        raise RuntimeError("reset failed")
    worker = pool.WorkerTab(19901, "audit", "qwen", "ws://unused", SimpleNamespace(reset_chat=reset))
    worker.connect = connect
    with pytest.raises(RuntimeError, match="reset failed"):
        asyncio.run(worker._reset_phase())


def call(name, args, cid):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def completed(messages, action, output="ok"):
    messages.extend([{"role": "assistant", "tool_calls": [action]},
                     {"role": "tool", "tool_call_id": action["id"], "content": output}])


def test_read_after_edit_is_verification_and_reaches_client(cluster_client):
    messages = [{"role": "user", "content": "Fix and verify app.py"}]
    completed(messages, call("read_file", {"path": "app.py"}, "old-read"), "v1")
    completed(messages, call("write", {"path": "app.py", "content": "v2"}, "edit"))
    repeat = call("read_file", {"path": "app.py"}, "new-read")
    assert tools.repeated_successful_actions([repeat], messages) == ([], [])
    response, _ = cluster_client([json.dumps({"actions": [{"name": "read_file", "arguments": {"path": "app.py"}}]})],
                                {"messages": messages, "tools": READ_TOOLS})
    assert response.status_code == 200
    assert response.json()["choices"][0]["finish_reason"] == "tool_calls"


def test_read_resets_a_previous_mutation_streak():
    messages = [{"role": "user", "content": "Fix app.py"}]
    for i in range(tools.MAX_SUCCESSFUL_TARGET_ACTIONS):
        completed(messages, call("write", {"path": "app.py", "content": str(i)}, str(i)))
    completed(messages, call("read_file", {"path": "app.py"}, "verify"), "needs another fix")
    next_edit = call("write", {"path": "app.py", "content": "corrected"}, "last")
    assert tools.repeated_successful_actions([next_edit], messages) == ([], [])


def test_qwen_edit_is_not_advertised_without_verified_output():
    assert get_adapter("qwen").caps.image_gen
    assert not get_adapter("qwen").caps.image_edit
    assert catalog._cluster_caps("qwen")["image_editing"] is False
    for provider in ("gemini", "chatgpt"):
        assert catalog._cluster_caps(provider)["image_editing"] is True


def test_duplicate_upload_names_do_not_overwrite_any_file():
    with A.AttachmentStore() as store:
        files = [store.add_bytes(data, "text/plain", name) for name, data in
                 [("report.txt", b"first"), ("report-3.txt", b"second"), ("report.txt", b"third"), ("report.txt", b"fourth")]]
        assert len({f.path for f in files}) == 4
        assert [Path(f.path).read_bytes() for f in files] == [b"first", b"second", b"third", b"fourth"]


def test_top_level_input_file_is_actually_attached():
    with A.AttachmentStore() as store:
        result = A.parse_message_content([{"type": "input_file", "filename": "probe.txt",
            "file_data": "data:text/plain;base64," + base64.b64encode(b"file canary").decode()}], store)
        assert len(result.attachments) == 1
        assert Path(result.attachments[0].path).read_bytes() == b"file canary"


@pytest.mark.parametrize("part,code", [
    ({"type": "file", "file": {"file_id": "file-probe"}}, "attachment_file_id_unsupported"),
    ({"type": "input_file"}, "attachment_missing_data"),
    ({"type": "image_url", "image_url": {}}, "attachment_missing_data"),
])
def test_missing_attachment_data_is_an_error_instead_of_silent_loss(cluster_client, part, code):
    response, bal = cluster_client([], {"messages": [{"role": "user", "content": [part]}]})
    assert response.status_code == 400 and response.json()["error"]["code"] == code
    assert not bal.prompts


def test_invalid_base64_is_rejected():
    with A.AttachmentStore() as store, pytest.raises(A.AttachmentError, match="base64"):
        store.add_data_uri("data:image/png;base64,!!!!")


def test_cancelled_download_finishes_before_store_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(A.cfg, "ATTACHMENT_DIR", str(tmp_path))
    started, release = threading.Event(), threading.Event()
    def download(store):
        store.add_bytes(b"first", "text/plain", "a.txt")
        started.set()
        assert release.wait(2)
        store.add_bytes(b"last", "text/plain", "b.txt")
    async def check():
        store = A.AttachmentStore()
        with store:
            job = asyncio.create_task(store.run_io(download, store))
            for _ in range(500):
                if started.is_set():
                    break
                await asyncio.sleep(.001)
            assert started.is_set()
            job.cancel()
            with pytest.raises(asyncio.CancelledError):
                await job
        assert list(tmp_path.iterdir())  # a worker is still using the store
        release.set()
        for _ in range(500):
            if not list(tmp_path.iterdir()):
                break
            await asyncio.sleep(.001)
        assert not list(tmp_path.iterdir())
    try:
        asyncio.run(check())
    finally:
        release.set()


def test_slow_file_download_does_not_block_another_chat_request(monkeypatch):
    started, release = threading.Event(), threading.Event()
    def download(self, url, name=""):
        started.set()
        assert release.wait(2)
        return self.add_bytes(b"synthetic file", "text/plain", name)
    monkeypatch.setattr(A.AttachmentStore, "add_url", download)
    monkeypatch.setattr(server.cfg, "API_KEY", "")
    monkeypatch.setattr(server.cfg, "CACHE_ENABLED", False)
    server.app.state.balancer = _FakeBalancer(["second chat answered", "file answered"])
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://audit") as client:
            first = asyncio.create_task(client.post("/v1/chat/completions", json={"model": "qwen", "messages": [
                {"role": "user", "content": [{"type": "text", "text": "Read this file"},
                    {"type": "file", "file": {"file_url": "https://example.org/probe.txt"}}]}]}))
            try:
                for _ in range(500):
                    if started.is_set():
                        break
                    await asyncio.sleep(.001)
                assert started.is_set()
                second = await asyncio.wait_for(client.post("/v1/chat/completions", json={"model": "qwen",
                    "messages": [{"role": "user", "content": "Second independent chat"}]}), .5)
                assert second.status_code == 200 and not first.done()
            finally:
                release.set()
                response = await first
            assert response.status_code == 200
    asyncio.run(check())


@pytest.mark.parametrize("kind", ["image_url", "file"])
def test_text_only_local_models_reject_attachments_before_charging(env, kind):
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    before = wallet(env, uid)
    part = {"type": kind, kind: {"url": "https://example.org/a.png"}} if kind == "image_url" else {
        "type": "file", "file": {"file_data": "data:text/plain;base64,YQ=="}}
    response = env.client.post("/v1/chat/completions", headers=bearer(key), json={"model": "model-1.0",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "Describe this"}, part]}]})
    assert response.status_code == 400 and response.json()["error"]["code"] == "unsupported_feature"
    assert wallet(env, uid) == before
    assert not env.db.all("SELECT * FROM reservations")


@pytest.mark.parametrize("events", [
    ["[DONE]"],
    [{"choices": [{"delta": {"content": "unfinished answer"}}]}, "[DONE]"],
    [{"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "read_file", "arguments": "{"}}]}}]}, "[DONE]"],
])
def test_native_nonstream_collector_rejects_unfinished_choices_even_with_done(events):
    async def check():
        stream = Stream(events)
        with pytest.raises(local_guard.GuardStopped, match="stream_interrupted"):
            await local_guard.collect(httpx.Response(200, stream=stream), 1, 1)
        assert stream.closed
    asyncio.run(check())


def test_native_collector_accepts_completed_choice_with_missing_transport_marker():
    async def check():
        events = [{"choices": [{"delta": {"content": "done"}, "finish_reason": "stop"}]}]
        result = await local_guard.collect(httpx.Response(200, stream=Stream(events)), 1, 1)
        assert result["choices"][0]["message"]["content"] == "done"
    asyncio.run(check())


def test_cancelled_direct_stream_stops_producer_before_client_reuse(monkeypatch):
    release, closed = threading.Event(), threading.Event()
    deleted = []
    monkeypatch.setattr(pool.cfg, "DEEPSEEK_AUTO_CLEANUP", True)
    def generate(**kw):
        try:
            yield {"type": "content", "text": "first"}
            assert release.wait(2)
            yield {"type": "content", "text": "must be discarded"}
        finally:
            closed.set()
    async def check():
        first = asyncio.Event()
        worker = pool.DeepSeekDirectWorker("synthetic-token")
        worker._client = SimpleNamespace(create_chat_session=lambda: "synthetic-session", stream_chat=generate,
                                         delete_chat_session=deleted.append)
        runmeta.start(on_progress=lambda text: first.set())
        task = asyncio.create_task(worker.execute_prompt("synthetic request"))
        try:
            await asyncio.wait_for(first.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert worker.state() == "BUSY" and not worker.is_available()
            assert not deleted
        finally:
            release.set()
            await asyncio.wait_for(asyncio.shield(worker._stream_future), 1)
        assert closed.is_set() and deleted == ["synthetic-session"]
        assert worker.state() == "ACTIVE" and worker.is_available()
    asyncio.run(check())


def test_closing_direct_generator_closes_http_response(monkeypatch):
    from cluster.deepseek_direct import DeepSeekDirectClient
    closed = []
    response = SimpleNamespace(status_code=200, iter_lines=lambda: iter([b'data: {"v":"first"}', b'data: {"v":"second"}']),
                               close=lambda: closed.append(True))
    client = DeepSeekDirectClient("synthetic-token")
    monkeypatch.setattr(client, "solve_pow", lambda *args: "synthetic-pow")
    monkeypatch.setattr(client.session, "post", lambda *args, **kw: response)
    stream = client.stream_chat("synthetic request", session_id="synthetic-session")
    assert next(stream)["text"] == "first"
    stream.close()
    assert closed == [True]
