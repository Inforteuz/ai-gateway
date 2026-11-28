"""OmniRoute as a second source beside the browser cluster (owner's call 2026-11-27).

Never a catalog model. Browser-model chat requests are shared with it by `omni_share`
(round-robin); a failure on one source goes to the other; with no free tab and no key
pool it takes the overflow. Same public model, same price.
"""
import json
import time

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import capacity
from .conftest import add_key, add_model, add_user, bearer
from .test_models_refine import login_admin


@pytest.fixture(autouse=True)
def fresh_cache():
    capacity._cache.update(at=0.0, free=None)
    capacity._balance["acc"] = 0
    yield
    capacity._cache.update(at=0.0, free=None)
    capacity._balance["acc"] = 0


def _env(env, *, qwen_free=True, cluster_status=200, omni=True, pool=False, share="0", omni_status=200):
    calls = {"cluster": 0, "omni": 0, "pool": 0, "omni_auth": None, "omni_model": None}
    cluster = FastAPI()

    @cluster.get("/cluster/status")
    async def status():
        return {"workers": [{"model": "qwen", "available": qwen_free}]}

    @cluster.post("/v1/chat/completions")
    async def cluster_chat(request: Request):
        calls["cluster"] += 1
        if cluster_status != 200:
            return JSONResponse({"error": {"message": "all tabs failed", "code": "model_unavailable"}}, status_code=cluster_status)
        return {"choices": [{"index": 0, "message": {"role": "assistant", "content": "from-cluster"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

    ext = FastAPI()

    @ext.post("/v1/chat/completions")
    async def ext_chat(request: Request):
        body = await request.json()
        who = "omni" if request.url.hostname == "front-tunnel" else "pool"
        calls[who] += 1
        if who == "omni":
            calls["omni_auth"], calls["omni_model"] = request.headers.get("authorization"), body.get("model")
            if omni_status != 200:
                return JSONResponse({"error": {"message": "no provider answered"}}, status_code=omni_status)
        # OmniRoute answers with the real upstream model's name; the client must never see it
        return {"model": "kimi-k3" if who == "omni" else "gemini-3.5-flash",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": f"from-{who}"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 100}}

    env.app.state.cluster_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=cluster), base_url="http://cluster")
    env.app.state.ext_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=ext), base_url="http://ext")
    settings = {"cluster_base_url": "http://cluster/v1", "fastpath_enabled": "0", "pool_spill_model": "gemini-zaxira",
                "pool_base_url": "http://gemini-pool:8088/v1", "pool_api_key": "pool-token" if pool else "",
                "omni_base_url": "http://front-tunnel:20128/v1" if omni else "", "omni_api_key": "omni-key" if omni else "",
                "omni_spill_model": "omni-zaxira", "omni_share": share}
    for k, v in settings.items():
        env.db.set_setting(k, v)
    add_model(env, "slow-agent", upstream="qwen", provider="cluster")
    add_model(env, "gemini-zaxira", upstream="gemini-3.5-flash", provider="pool", listed=0, price_in=1, price_out=1)
    add_model(env, "omni-zaxira", upstream="auto", provider="omni", listed=0, price_in=1, price_out=1)
    key, _ = add_key(env, add_user(env, balance_som=1000))
    return calls, key


def _ask(env, key):
    return env.client.post("/v1/chat/completions", headers=bearer(key),
                           json={"model": "slow-agent", "messages": [{"role": "user", "content": "hello there"}]})


def test_busy_model_spills_to_omniroute_when_no_pool(env):
    calls, key = _env(env, qwen_free=False)
    r = _ask(env, key)
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "from-omni"
    assert calls["cluster"] == 0 and calls["omni"] == 1
    assert calls["omni_auth"] == "Bearer omni-key" and calls["omni_model"] == "auto"
    row = env.db.one("SELECT model, cost, attribution FROM usage_logs")
    meta = json.loads(row["attribution"])
    assert row["model"] == "slow-agent" and meta["route_model"] == "omni-zaxira" and meta["route_reason"] == "capacity"
    assert row["cost"] == 1000 * 10_000 + 100 * 30_000          # the requested model's price
    assert r.json()["model"] == "slow-agent"                     # the client never sees OmniRoute


def test_pool_still_comes_first(env):
    calls, key = _env(env, qwen_free=False, pool=True)
    assert _ask(env, key).json()["choices"][0]["message"]["content"] == "from-pool"
    assert calls["omni"] == 0


def test_failed_model_without_fallback_retries_on_omniroute(env):
    calls, key = _env(env, cluster_status=503)
    r = _ask(env, key)
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "from-omni"
    assert calls["cluster"] >= 1 and calls["omni"] == 1
    assert r.headers.get("x-fallback-model") == "omni-zaxira"


def test_without_omniroute_the_error_passes_through(env):
    calls, key = _env(env, cluster_status=503, omni=False)
    assert _ask(env, key).status_code == 503 and calls["omni"] == 0


def test_omniroute_is_never_listed_and_its_key_is_never_returned(env):
    _env(env)
    ids = [m["id"] for m in env.client.get("/portal/models").json()["items"]]
    assert "omni-zaxira" not in ids
    login_admin(env)
    s = env.client.get("/portal/admin/settings").json()
    assert s["omni_api_key_set"] is True and "omni-key" not in json.dumps(s)


def test_requests_alternate_between_the_two_sources(env):
    calls, key = _env(env, share="50")
    answers = [_ask(env, key).json()["choices"][0]["message"]["content"] for _ in range(4)]
    assert answers == ["from-cluster", "from-omni", "from-cluster", "from-omni"]
    reasons = [json.loads(r["attribution"]).get("route_reason") for r in env.db.all("SELECT attribution FROM usage_logs ORDER BY id")]
    assert reasons.count("balance") == 2
    costs = {r["cost"] for r in env.db.all("SELECT cost FROM usage_logs WHERE status_code=200 AND attribution LIKE '%balance%'")}
    assert costs == {1000 * 10_000 + 100 * 30_000}              # OmniRoute's turn at the requested model's price


def test_share_zero_keeps_everything_on_the_cluster(env):
    calls, key = _env(env, share="0")
    for _ in range(3):
        _ask(env, key)
    assert calls["omni"] == 0 and calls["cluster"] == 3


def test_omniroute_failure_goes_back_to_the_browser_model(env):
    calls, key = _env(env, share="100", omni_status=503)
    r = _ask(env, key)
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "from-cluster"
    assert calls["omni"] == 1 and calls["cluster"] == 1


def test_attachments_never_go_to_omniroute(env):
    calls, key = _env(env, share="100")
    r = env.client.post("/v1/chat/completions", headers=bearer(key), json={"model": "slow-agent", "messages": [
        {"role": "user", "content": [{"type": "text", "text": "what is this"},
                                     {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}}]}]})
    assert calls["omni"] == 0 and calls["cluster"] == 1, r.text


# --- Local (private) models when the GPU host fails (owner's call 2026-11-28) -----------------------------------
def _local(env, key, model="sable-1.0"):
    return env.client.post("/v1/chat/completions", headers=bearer(key),
                           json={"model": model, "messages": [{"role": "user", "content": "salom"}]})


def test_failed_local_model_answers_from_omniroute(env):
    calls, key = _env(env)
    add_model(env, "sable-1.0", provider="local", fallback="slow-agent")   # its own fallback is a browser model
    env.up["mode"] = "500"                                               # the GPU host is down
    r = _local(env, key)
    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["message"]["content"] == "from-omni" and r.json()["model"] == "sable-1.0"
    assert r.headers["x-fallback-model"] == "omni-zaxira"
    assert r.headers["x-sf-data-processing"] == "third_party"            # the client is told the text left
    assert calls["omni"] == 1 and calls["cluster"] == 0                  # OmniRoute replaces the own fallback


def test_local_cover_can_be_turned_off(env):
    calls, key = _env(env)
    env.db.set_setting("omni_cover_local", "0")
    add_model(env, "sable-1.0", provider="local", fallback="slow-agent")
    env.up["mode"] = "500"
    r = _local(env, key)
    assert r.status_code == 502 and calls["omni"] == 0 and calls["cluster"] == 0   # privacy kept: the error passes


def test_working_local_model_never_touches_omniroute(env):
    calls, key = _env(env, share="100")
    add_model(env, "sable-1.0", provider="local")
    r = _local(env, key)
    assert r.status_code == 200 and calls["omni"] == 0 and "x-sf-data-processing" not in r.headers


def test_local_cover_needs_omniroute_configured(env):
    calls, key = _env(env, omni=False)
    add_model(env, "sable-1.0", provider="local")
    env.up["mode"] = "500"
    assert _local(env, key).status_code == 502 and calls["omni"] == 0


def test_admin_reads_and_sets_local_cover(env):
    _env(env)
    login_admin(env)
    assert str(env.client.get("/portal/admin/settings").json()["omni_cover_local"]) == "1"
    r = env.client.put("/portal/admin/settings", json={"omni_cover_local": "0"}, headers={"X-SF-CSRF": "1"})
    assert r.status_code == 200, r.text
    assert str(env.client.get("/portal/admin/settings").json()["omni_cover_local"]) == "0"
    assert env.client.put("/portal/admin/settings", json={"omni_cover_local": "yes"}, headers={"X-SF-CSRF": "1"}).status_code == 422


# --- Status: public level and per-model fallback; tiers for admins only (owner's call 2026-11-28) -----------------
from app import cluster_watch, monitor, tiers as tiers_mod


def _gpu_down(env):
    monitor.record(env.app, "ollama_down", None, "HTTP 502", int(time.time()))


def test_local_models_show_fallback_when_gpu_down_and_covered(env):
    _env(env)
    add_model(env, "sable-1.0", provider="local")
    st = cluster_watch.public_status(env.db, "ollama_down")
    item = next(x for x in st["items"] if x["model"] == "sable-1.0")
    assert item["state"] == "fallback" and st["level"] == "degraded" and "sable-1.0" in st["fallback"]
    assert not any(d["model"] == "sable-1.0" for d in st["degraded"])     # not reported as down


def test_local_models_show_down_without_cover(env):
    _env(env)
    env.db.set_setting("omni_cover_local", "0")
    add_model(env, "sable-1.0", provider="local")
    st = cluster_watch.public_status(env.db, "ollama_down")
    assert next(x for x in st["items"] if x["model"] == "sable-1.0")["state"] == "down"


def test_public_status_names_no_tiers(env):
    _env(env)
    add_model(env, "sable-1.0", provider="local")
    _gpu_down(env)
    r = env.client.get("/portal/status").json()
    assert r["state"] == "degraded" and r["model_online"] is True
    assert "zaxira" in r["label"].lower()
    text = json.dumps(r).lower()
    assert "tier" not in text and "omni" not in text and "gpu" not in text   # architecture stays off public pages


def test_admin_stats_show_three_tiers(env):
    _env(env)
    tiers_mod._omni_probe.update(at=0.0, ok=None)
    _gpu_down(env)
    login_admin(env)
    t = env.client.get("/portal/admin/stats?days=7").json()["tiers"]
    assert t["tier1"]["state"] == "down"
    assert set(t) == {"tier1", "tier2", "tier3"}
    assert t["tier3"]["or"] in ("up", "down") and "omniroute" not in json.dumps(t).lower() and t["tier3"]["covers_local"] is True


# --- Admin: Tier 3 (OR) account status through a separate manage-scope key ---------------------------------------
from app import or_status


def _or_admin(env, *, providers_status=200, connections=None):
    or_status._cache.update(at=0.0, key=None, data=None)
    seen = {"auth": None}
    adm = FastAPI()

    @adm.get("/api/providers")
    async def providers(request: Request):
        seen["auth"] = request.headers.get("authorization")
        if providers_status != 200:
            return JSONResponse({"error": {"message": "nope"}}, status_code=providers_status)
        return {"connections": connections or [], "total": len(connections or [])}

    env.app.state.ext_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=adm), base_url="http://front-tunnel")
    return seen


def test_or_status_needs_its_own_manage_key(env):
    _env(env)
    login_admin(env)
    r = env.client.get("/portal/admin/or-status").json()
    assert r["configured"] is True and r["manage_key_set"] is False and r["providers"] == []


def test_or_status_summarizes_accounts_without_secrets(env):
    _env(env)
    env.db.set_setting("omni_manage_key", "mgmt-key")
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 600))
    seen = _or_admin(env, connections=[
        {"provider": "grok-cli", "isActive": True, "testStatus": "active", "accessToken": "eyJsecretsecretsecret123", "email": "a@b.c"},
        {"provider": "grok-cli", "isActive": True, "testStatus": "active", "rateLimitedUntil": future},
        {"provider": "trae", "isActive": False, "testStatus": "error", "lastError": "boom sk-abcdefgh123456 failed",
         "lastErrorAt": "2026-11-28T13:44:53Z"},
    ])
    login_admin(env)
    r = env.client.get("/portal/admin/or-status").json()
    assert seen["auth"] == "Bearer mgmt-key"                              # the manage key, not the traffic key
    rows = {p["provider"]: p for p in r["providers"]}
    assert rows["grok-cli"]["accounts"] == 2 and rows["grok-cli"]["active"] == 2 and rows["grok-cli"]["limited"] == 1
    assert rows["grok-cli"]["state"] == "up" and rows["trae"]["state"] == "off"
    assert "sk-abcdefgh123456" not in rows["trae"]["last_error"] and "boom" in rows["trae"]["last_error"]
    text = json.dumps(r)
    assert "eyJsecret" not in text and "a@b.c" not in text and "mgmt-key" not in text


def test_or_status_reports_a_key_without_manage_scope(env):
    _env(env)
    env.db.set_setting("omni_manage_key", "weak-key")
    _or_admin(env, providers_status=403)
    login_admin(env)
    assert env.client.get("/portal/admin/or-status").json()["error"] == "kalitda manage huquqi yo'q"


def test_manage_key_is_write_only(env):
    _env(env)
    login_admin(env)
    h = {"X-SF-CSRF": "1"}
    assert env.client.put("/portal/admin/settings", json={"omni_manage_key": " mk-123 "}, headers=h).status_code == 200
    s = env.client.get("/portal/admin/settings").json()
    assert s["omni_manage_key_set"] is True and "mk-123" not in json.dumps(s)
    env.client.put("/portal/admin/settings", json={"omni_manage_key": ""}, headers=h)          # empty keeps it
    assert env.client.get("/portal/admin/settings").json()["omni_manage_key_set"] is True
    env.client.put("/portal/admin/settings", json={"clear_omni_manage_key": True}, headers=h)
    assert env.client.get("/portal/admin/settings").json()["omni_manage_key_set"] is False


def test_or_status_counts_our_traffic(env):
    calls, key = _env(env, share="50")
    for _ in range(2):
        _ask(env, key)
    login_admin(env)
    t = env.client.get("/portal/admin/or-status").json()["traffic"]
    assert t["requests"] == 1 and t["ok"] == 1 and t["by_reason"]["balance"]["requests"] == 1


# --- Pictures shared with OR (owner's call 2026-11-28) -------------------------------------------------------------
PNG = "iVBORw0KGgo="


def _img_env(env, *, share="50", free=True, cluster_status=200, omni_status=200, omni_empty=False):
    calls = {"cluster": 0, "omni": 0, "omni_body": None, "cluster_model": None}
    cluster = FastAPI()

    @cluster.get("/cluster/status")
    async def status():
        return {"workers": [{"model": "qwen", "available": free}, {"model": "image-pro", "available": free}]}

    @cluster.post("/v1/images/generations")
    @cluster.post("/v1/images/edits")
    async def cluster_img(request: Request):
        calls["cluster"] += 1
        calls["cluster_model"] = (await request.json()).get("model")
        if cluster_status != 200:
            return JSONResponse({"error": {"message": "tab failed", "code": "model_unavailable"}}, status_code=cluster_status)
        return {"data": [{"b64_json": PNG, "mime_type": "image/png"}], "usage": {"prompt_tokens": 10, "completion_tokens": 1000}}

    ext = FastAPI()

    @ext.post("/v1/images/generations")
    @ext.post("/v1/images/edits")
    async def omni_img(request: Request):
        calls["omni"] += 1
        calls["omni_body"] = await request.json()
        if omni_status != 200:
            return JSONResponse({"error": {"message": "no provider"}}, status_code=omni_status)
        return {"created": 1, "data": [] if omni_empty else [{"b64_json": PNG}]}   # OR reports no usage

    env.app.state.cluster_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=cluster), base_url="http://cluster")
    env.app.state.ext_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=ext), base_url="http://front-tunnel")
    for k, v in {"cluster_base_url": "http://cluster/v1", "fastpath_enabled": "0", "pool_api_key": "",
                 "omni_base_url": "http://front-tunnel:20128/v1", "omni_api_key": "omni-key", "omni_share": "0",
                 "omni_image_model": "omni-rasm", "omni_image_share": share}.items():
        env.db.set_setting(k, v)
    add_model(env, "sable-3.1", upstream="qwen", provider="cluster")
    add_model(env, "sable-image-pro", upstream="image-pro", provider="cluster")
    add_model(env, "omni-rasm", upstream="antigravity/gemini-3.1-flash-image", provider="omni", listed=0, price_in=1, price_out=1)
    key, _ = add_key(env, add_user(env, balance_som=100000))
    capacity._image_balance["acc"] = 0
    return calls, key


def _draw(env, key, model="sable-3.1", edit=False):
    body = {"model": model, "prompt": "kulib turgan echki", "size": "1024x1024"}
    if edit:
        body["image"] = "data:image/png;base64," + PNG
    return env.client.post("/v1/images/edits" if edit else "/v1/images/generations", headers=bearer(key), json=body)


def test_pictures_alternate_between_cluster_and_or(env):
    calls, key = _img_env(env, share="50")
    rs = [_draw(env, key) for _ in range(4)]
    assert all(r.status_code == 200 for r in rs), [r.text for r in rs]
    assert calls["cluster"] == 2 and calls["omni"] == 2
    assert calls["omni_body"]["model"] == "antigravity/gemini-3.1-flash-image"
    rows = env.db.all("SELECT model, cost, completion_tokens, attribution FROM usage_logs WHERE status_code=200 ORDER BY id")
    or_rows = [r for r in rows if json.loads(r["attribution"]).get("route_model") == "omni-rasm"]
    assert len(or_rows) == 2 and all(r["model"] == "sable-3.1" for r in or_rows)
    assert {r["completion_tokens"] for r in or_rows} == {1000}                 # billed as one picture, not ~free
    cluster_cost = next(r["cost"] for r in rows if r not in or_rows)
    assert all(abs(r["cost"] - cluster_cost) <= cluster_cost * 0.01 for r in or_rows)   # same price as the cluster (prompt estimate aside)


def test_busy_picture_model_draws_on_or(env):
    calls, key = _img_env(env, share="0", free=False)
    r = _draw(env, key)
    assert r.status_code == 200 and calls["omni"] == 1 and calls["cluster"] == 0
    assert json.loads(env.db.one("SELECT attribution FROM usage_logs")["attribution"])["route_reason"] == "capacity"


def test_failed_cluster_picture_retries_on_or(env):
    calls, key = _img_env(env, share="0", cluster_status=503)
    r = _draw(env, key)
    assert r.status_code == 200 and r.headers.get("x-fallback-model") == "omni-rasm"
    assert calls["cluster"] >= 1 and calls["omni"] == 1


def test_or_picture_failure_goes_back_to_the_cluster(env):
    calls, key = _img_env(env, share="100", omni_status=503)
    r = _draw(env, key)
    assert r.status_code == 200 and calls["omni"] == 1 and calls["cluster"] == 1


def test_empty_or_picture_is_a_failure_not_free(env):
    calls, key = _img_env(env, share="100", omni_empty=True)
    r = _draw(env, key)
    assert r.status_code == 200 and calls["cluster"] == 1                      # redrawn on the cluster
    assert env.db.one("SELECT COUNT(*) n FROM usage_logs WHERE status_code=200 AND cost=0")["n"] == 0


def test_image_pro_stays_on_the_cluster(env):
    calls, key = _img_env(env, share="100")
    assert _draw(env, key, model="sable-image-pro").status_code == 200
    assert calls["omni"] == 0 and calls["cluster"] == 1


def test_picture_edits_go_to_or_with_the_source_image(env):
    calls, key = _img_env(env, share="100")
    r = _draw(env, key, edit=True)
    assert r.status_code == 200 and calls["omni"] == 1 and str(calls["omni_body"].get("image", "")).startswith("data:image/png")


def test_hidden_rows_cannot_be_asked_for_by_name(env):
    calls, key = _env(env)
    for hidden in ("omni-zaxira", "gemini-zaxira"):
        r = env.client.post("/v1/chat/completions", headers=bearer(key),
                            json={"model": hidden, "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 404, hidden
    assert calls["omni"] == 0 and calls["pool"] == 0
