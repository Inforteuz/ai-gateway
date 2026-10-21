"""Fast path: trivial messages go to the fast GPU model instead of the web-chat model."""
import httpx
from fastapi import FastAPI, Request

from app import fastpath as fp
from .conftest import add_key, add_model, add_user, bearer


def _b(text, **extra):
    return {"messages": [{"role": "system", "content": "uzun agent ko'rsatmasi " * 500},
                         {"role": "user", "content": text}], **extra}


def test_trivial_messages_are_eligible():
    for t in ("Hey", "Salom!", "Ishlayapsanmi", "Sen kimsan?", "rahmat 🙏", "Привет", "who are you?",
              "Assalomu alaykum", "ishlayapsanmi?", "Qalaysan"):
        assert fp.eligible(_b(t)), t


def test_commands_and_real_tasks_are_not_eligible():
    for t in ("ok", "ha", "davom et", "Qayta harakat qil", "hello.py yarat", "salom dunyo dasturini yoz",
              "Sen kimsan va qanday ishlaysan, batafsil tushuntir?"):
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


def test_gateway_routes_greeting_to_fast_local_model(env):
    state, key = _setup(env)
    r = env.client.post("/v1/chat/completions", headers=bearer(key),
                        json={"model": "model-3.5", **_b("Hey", tools=[{"type": "function", "function": {"name": "x"}}])})
    assert r.status_code == 200 and r.json()["model"] == "model-3.5"
    assert "local" in state and "cluster" not in state
    assert state["local"]["model"] == "qwen3:14b" and len(state["local"]["messages"]) == 2


def test_real_task_still_goes_to_cluster_and_toggle_disables(env):
    state, key = _setup(env)
    env.client.post("/v1/chat/completions", headers=bearer(key), json={"model": "model-3.5", **_b("hello.py yarat")})
    assert "cluster" in state and "local" not in state
    state.clear()
    env.db.set_setting("fastpath_enabled", "0")
    env.client.post("/v1/chat/completions", headers=bearer(key), json={"model": "model-3.5", **_b("Hey")})
    assert "cluster" in state and "local" not in state
