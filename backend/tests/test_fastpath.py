"""Fixed fast replies for trivial messages."""
import httpx
from fastapi import FastAPI, Request

from app import fastpath as fp
from .conftest import add_key, add_model, add_user, bearer


def _b(text, **extra):
    return {"messages": [{"role": "system", "content": "uzun agent ko'rsatmasi " * 500},
                         {"role": "user", "content": text}], **extra}


def test_trivial_messages_are_eligible():
    for t in ("Hey", "Salom!", "Ishlayapsanmi", "Sen kimsan?", "rahmat 🙏", "Привет", "who are you?",
              "Assalomu alaykum", "ishlayapsanmi?", "Qalaysan", "Hey nima gap", "Nima gap?",
              "How are u", "Salom qalaysan!", "Hayrli tong Sable", "Xayrli tong, Sable!",
              "Good morning, Sable!", "Доброе утро, Sable!"):
        assert fp.eligible(_b(t)), t


def test_commands_and_real_tasks_are_not_eligible():
    for t in ("ok", "ha", "davom et", "Qayta harakat qil", "hello.py yarat", "salom dunyo dasturini yoz",
              "Sen kimsan va qanday ishlaysan, batafsil tushuntir?", "Hey nima gap kodni tuzat",
              "How are u going to fix the file?", "Hayrli tong Sable, papkalarni sana"):
        assert not fp.eligible(_b(t)), t
    assert not fp.eligible(_b("Hey", response_format={"type": "json_object"}))
    assert not fp.eligible(_b("Hey", tool_choice="required"))
    assert not fp.eligible({"messages": [{"role": "user", "content": [{"type": "text", "text": "Hey"},
                                                                       {"type": "image_url", "image_url": {"url": "x"}}]}]})
    assert not fp.eligible({"messages": [{"role": "user", "content": "Hey"},
                                         {"role": "tool", "tool_call_id": "1", "content": "natija"}]})


def test_payload_drops_history_and_tools():
    p = fp.payload(_b("Hey", tools=[{"type": "function", "function": {"name": "x"}}], stream=True), "qwen3:14b", 4000)
    assert [m["role"] for m in p["messages"]] == ["system", "user"]
    assert p["messages"][1]["content"] == "Hey" and "tools" not in p
    assert p["reasoning_effort"] == "none" and p["max_tokens"] == 200 and p["stream"] is True


def _fake(state, name):
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        state[name] = await request.json()
        return {"id": "c", "object": "chat.completion", "created": 1, "model": "x",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "Salom!"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 40, "completion_tokens": 5, "total_tokens": 45}}
    return app


def _setup(env):
    state = {}
    env.app.state.cluster_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=_fake(state, "cluster")),
                                                   base_url="http://cluster")
    up = env.app.state.config.upstream_url
    env.app.state.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=_fake(state, "local")),
                                           base_url=up)
    env.db.set_setting("cluster_base_url", "http://cluster/v1")
    add_model(env, "model-3.5", provider="cluster", upstream="gemini")
    uid = add_user(env, "fp@test.uz", balance_som=1000)
    key, _ = add_key(env, uid)
    return state, key


def test_gateway_answers_greeting_with_a_fixed_reply(env):
    # 2026-11-20: no model at all (the shared GPU made "kimsan" take 77 s; a local model named itself Qwen)
    state, key = _setup(env)
    r = env.client.post("/v1/chat/completions", headers=bearer(key),
                        json={"model": "model-3.5", **_b("Hey", tools=[{"type": "function", "function": {"name": "x"}}])})
    assert r.status_code == 200 and r.json()["model"] == "model-3.5"
    assert r.json()["choices"][0]["message"]["content"] == fp.REPLIES["greeting"]["en"]
    assert state == {}                                   # neither the cluster nor the GPU was asked
    assert env.db.scalar("SELECT COUNT(*) FROM usage_logs WHERE status_code=200") == 1


def test_identity_question_to_a_local_model_never_names_qwen(env):
    state, key = _setup(env)
    add_model(env, "model-2.0", provider="local", upstream="qwen3:14b")
    r = env.client.post("/v1/chat/completions", headers=bearer(key), json={"model": "model-2.0", **_b("Sen kimsan?")})
    text = r.json()["choices"][0]["message"]["content"]
    assert r.status_code == 200 and "Qwen" not in text and text == fp.REPLIES["identity"]["uz"]
    assert state == {}


def test_opencode_greeting_skips_cluster_routing_with_large_context(env, monkeypatch):
    state, key = _setup(env)

    def unexpected(*args):
        raise AssertionError("A fixed reply must not compose a browser prompt or check browser capacity")

    monkeypatch.setattr("app.routing.text_profile", unexpected)
    monkeypatch.setattr("app.capacity.pool_spill_target", unexpected)
    monkeypatch.setattr("app.admission.register", unexpected)
    response = env.client.post("/v1/chat/completions", headers=bearer(key), json={
        "model": "model-3.5", "reasoning_effort": "high", "stream": True,
        "messages": [
            {"role": "system", "content": "agent context " * 4000},
            {"role": "user", "content": "Hayrli tong Sable"},
        ],
        "tools": [{"type": "function", "function": {"name": "bash"}}],
    })
    assert response.status_code == 200 and fp.REPLIES["greeting"]["uz"] in response.text
    assert state == {}


def test_fixed_reply_streams_with_usage(env):
    state, key = _setup(env)
    r = env.client.post("/v1/chat/completions", headers=bearer(key),
                        json={"model": "model-3.5", "stream": True, "stream_options": {"include_usage": True},
                              **_b("кто ты")})
    assert r.status_code == 200 and fp.REPLIES["identity"]["ru"].split(" — ")[0] in r.text
    assert '"usage"' in r.text and "[DONE]" in r.text and state == {}


def test_real_task_still_goes_to_cluster_and_toggle_disables(env):
    state, key = _setup(env)
    env.client.post("/v1/chat/completions", headers=bearer(key), json={"model": "model-3.5", **_b("hello.py yarat")})
    assert "cluster" in state and "local" not in state
    state.clear()
    env.db.set_setting("fastpath_enabled", "0")
    env.client.post("/v1/chat/completions", headers=bearer(key), json={"model": "model-3.5", **_b("Hey")})
    assert "cluster" in state and "local" not in state
