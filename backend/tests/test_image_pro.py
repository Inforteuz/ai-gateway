"""Image Pro (2026-11-12): local-model prompt boost, base image model, upscaled result.
Media-only rows never take chat (a chat to them used to reach the cluster as "auto")."""
import asyncio
import base64

import httpx
from fastapi import FastAPI, Request

from cluster import server as cluster_server
from .conftest import add_key, add_model, add_user, bearer

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def _cluster(env):
    seen = []
    app = FastAPI()

    @app.post("/v1/images/generations")
    async def gen(request: Request):
        seen.append(await request.json())
        return {"created": 1, "data": [{"b64_json": base64.b64encode(PNG).decode(), "mime_type": "image/jpeg"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1000}}
    env.app.state.cluster_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://cluster")
    env.db.set_setting("cluster_base_url", "http://cluster/v1")
    env.db.set_setting("fastpath_enabled", "0")
    return seen


def test_image_pro_boosts_the_prompt_and_asks_for_upscaling(env):
    seen = _cluster(env)
    add_model(env, "img-pro", upstream="image-pro", provider="cluster")
    env.up["draft"] = "Photorealistic close-up of a silver laptop on an oak desk, soft window light, 50mm lens."
    key, _ = add_key(env, add_user(env, balance_som=1000))
    r = env.client.post("/v1/images/generations", headers=bearer(key), json={"model": "img-pro", "prompt": "noutbuk rasmi"})
    assert r.status_code == 200, r.text
    sent = seen[-1]
    assert sent["model"] == "gemini" and sent["sf_post"] == "upscale2"
    assert sent["prompt"].startswith("Photorealistic close-up") and env.up["last"]["messages"][-1]["content"] == "noutbuk rasmi"
    item = next(m for m in env.client.get("/portal/models").json()["items"] if m["id"] == "img-pro")
    assert item["capabilities"]["image_generation"] and item["capabilities"]["image_only"]


def test_media_rows_refuse_chat(env):
    _cluster(env)
    add_model(env, "img-pro", upstream="image-pro", provider="cluster")
    add_model(env, "vid", upstream="flow", provider="cluster")
    key, _ = add_key(env, add_user(env, balance_som=1000))
    for mid in ("img-pro", "vid"):
        r = env.client.post("/v1/chat/completions", headers=bearer(key), json={"model": mid, "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 400 and r.json()["error"]["code"] == "unsupported_endpoint"


def test_cluster_upscales_when_asked(monkeypatch):
    from cluster import video_post

    async def fake_upscale(data, factor=2):
        return b"\xff\xd8" + data
    monkeypatch.setattr(video_post, "upscale_image", fake_upscale)

    class _Bal:
        async def execute(self, **kw):
            return "", "gemini", None, [("image/png", base64.b64encode(PNG).decode())]
    monkeypatch.setattr(cluster_server.cfg, "API_KEY", "")
    monkeypatch.setattr(cluster_server.app.state, "balancer", _Bal(), raising=False)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=cluster_server.app), base_url="http://c") as c:
            return await c.post("/v1/images/generations", json={"prompt": "x", "model": "gemini", "sf_post": "upscale2"})
    r = asyncio.run(run())
    assert r.status_code == 200, r.text
    item = r.json()["data"][0]
    assert item["mime_type"] == "image/jpeg" and base64.b64decode(item["b64_json"]).startswith(b"\xff\xd8\x89PNG")
