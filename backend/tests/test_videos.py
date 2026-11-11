"""/v1/videos: async video jobs on the cluster's video model (app/videos.py).

The cluster is a fake FastAPI app behind httpx.ASGITransport. The worker runs in the app's
lifespan as in production; a few tests claim and process a job directly instead.
"""
import base64
import re
import time

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import integrity, videos
from .conftest import SOM, add_key, add_model, add_user, bearer, wallet

MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x01" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
PRICE = 5_000 * SOM          # price_out_per_1m of the video model = the price of one clip
BODY = {"model": "veo-1", "prompt": "A cat surfing at sunset"}


@pytest.fixture
def cluster(env):
    state = {"calls": [], "mode": "ok"}
    app = FastAPI()

    @app.post("/v1/videos/generations")
    async def generate(request: Request):
        state["calls"].append({"body": await request.json(), "auth": request.headers.get("authorization")})
        mode = state["mode"]
        if mode == "500":
            return JSONResponse({"error": {"message": "renderer crashed", "code": "upstream_failed"}}, status_code=500)
        if mode == "empty":
            return {"model": "flow", "data": []}
        if mode == "timeout":
            raise httpx.ReadTimeout("no clip in time")
        return {"model": "flow", "data": [{"b64_mp4": base64.b64encode(MP4).decode(), "mime_type": "video/mp4",
                                           "seconds": 8, "size": "1280x720"}]}

    env.app.state.cluster_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://cluster")
    env.db.set_setting("cluster_base_url", "http://cluster/v1")
    env.db.set_setting("cluster_api_key", "cluster-token")
    add_model(env, "veo-1", upstream="flow", provider="cluster", price_in=0, price_out=PRICE)
    return state


def paused(env):
    """No job starts: the queue only fills (video_max_concurrent=0)."""
    env.db.set_setting("video_max_concurrent", "0")


def consistent(env):
    violations, _ = integrity.run(lambda sql: env.db.all(sql))
    assert violations == {}, violations


def wait_done(env, vid, key, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = env.client.get(f"/v1/videos/{vid}", headers=bearer(key))
        assert r.status_code == 200, r.text
        if r.json()["status"] in ("completed", "failed"):
            return r.json()
        time.sleep(0.1)
    raise AssertionError(f"{vid} did not finish")


def run_one(env):
    """Claims the oldest queued job and processes it in the app's event loop (no worker involved)."""
    job = videos.claim(env.db, env.app.state.instance_id, 1)
    assert job is not None
    return env.client.portal.call(videos.process, env.app, job)


def usage(env, uid):
    return env.db.all("SELECT * FROM usage_logs WHERE user_id=? ORDER BY id", (uid,))


def charges(env, uid):
    return env.db.all("SELECT amount FROM ledger WHERE user_id=? AND kind='usage'", (uid,))


def test_create_poll_download_charges_the_clip_price_once(env, cluster):
    uid = add_user(env, balance_som=12_000)
    key, key_id = add_key(env, uid)
    r = env.client.post("/v1/videos", json={**BODY, "seconds": "8", "size": "1280x720"}, headers=bearer(key))
    assert r.status_code == 200, r.text
    v = r.json()
    assert re.fullmatch(r"video_[0-9a-f]{24}", v["id"]) and r.headers["X-Request-Id"] == v["id"]
    assert {k: v[k] for k in ("object", "model", "status", "progress", "seconds", "size", "error")} == {
        "object": "video", "model": "veo-1", "status": "queued", "progress": 0, "seconds": "8",
        "size": "1280x720", "error": None}
    assert v["created_at"] > 0

    done = wait_done(env, v["id"], key)
    assert done["status"] == "completed" and done["progress"] == 100 and done["error"] is None
    assert done["expires_at"] == done["completed_at"] + 86400

    call = cluster["calls"][0]
    assert call["auth"] == "Bearer cluster-token"
    assert call["body"] == {"model": "flow", "prompt": BODY["prompt"], "image": None, "seconds": 8, "size": "1280x720"}

    c = env.client.get(f"/v1/videos/{v['id']}/content", headers=bearer(key))
    assert c.status_code == 200 and c.content == MP4 and c.headers["content-type"] == "video/mp4"

    # Reading the status or the clip again charges nothing more
    env.client.get(f"/v1/videos/{v['id']}", headers=bearer(key))
    env.client.get(f"/v1/videos/{v['id']}/content", headers=bearer(key))
    assert wallet(env, uid) == (12_000 * SOM - PRICE, 0)
    rows = usage(env, uid)
    assert len(rows) == 1
    assert (rows[0]["request_id"], rows[0]["endpoint"], rows[0]["status_code"], rows[0]["cost"],
            rows[0]["completion_tokens"], rows[0]["key_id"]) == (v["id"], "videos", 200, PRICE, 1_000_000, key_id)
    assert [r["amount"] for r in charges(env, uid)] == [-PRICE]
    job = env.db.one("SELECT prompt, input_image, hold, cost FROM video_jobs WHERE id=?", (v["id"],))
    assert (job["prompt"], job["input_image"], job["hold"], job["cost"]) == (None, None, 0, PRICE)
    assert len(cluster["calls"]) == 1
    consistent(env)


def test_price_is_held_while_queued(env, cluster):
    paused(env)
    uid = add_user(env, balance_som=12_000)
    key, _ = add_key(env, uid)
    vid = env.client.post("/v1/videos", json=BODY, headers=bearer(key)).json()["id"]
    assert wallet(env, uid) == (12_000 * SOM, PRICE)
    consistent(env)
    # The second clip would need more than is left after the first one's hold
    r = env.client.post("/v1/videos", json=BODY, headers=bearer(key))
    r2 = env.client.post("/v1/videos", json=BODY, headers=bearer(key))
    assert r.status_code == 200 and r2.status_code == 402
    assert r2.json()["error"]["code"] == "insufficient_balance"
    assert env.client.get(f"/v1/videos/{vid}", headers=bearer(key)).json()["status"] == "queued"
    assert cluster["calls"] == []

    run_one(env)
    run_one(env)
    assert wallet(env, uid) == (12_000 * SOM - 2 * PRICE, 0)
    assert len(charges(env, uid)) == 2
    consistent(env)


def test_insufficient_balance_is_402_and_creates_nothing(env, cluster):
    uid = add_user(env, balance_som=100)
    key, _ = add_key(env, uid)
    r = env.client.post("/v1/videos", json=BODY, headers=bearer(key))
    assert r.status_code == 402 and r.json()["error"]["code"] == "insufficient_balance"
    assert env.db.scalar("SELECT COUNT(*) FROM video_jobs") == 0
    assert wallet(env, uid) == (100 * SOM, 0)


def test_free_key_is_not_charged(env, cluster):
    uid = add_user(env)
    key, _ = add_key(env, uid, free=1)
    vid = env.client.post("/v1/videos", json=BODY, headers=bearer(key)).json()["id"]
    assert wait_done(env, vid, key)["status"] == "completed"
    assert env.client.get(f"/v1/videos/{vid}/content", headers=bearer(key)).content == MP4
    assert wallet(env, uid) == (0, 0)
    assert [r["cost"] for r in usage(env, uid)] == [0]
    assert charges(env, uid) == []
    consistent(env)


@pytest.mark.parametrize("mode, code, status", [("500", "upstream_failed", 502), ("empty", "upstream_failed", 502),
                                                ("timeout", "upstream_timeout", 504)])
def test_failure_releases_the_hold_and_charges_nothing(env, cluster, mode, code, status):
    paused(env)
    cluster["mode"] = mode
    uid = add_user(env, balance_som=12_000)
    key, _ = add_key(env, uid)
    vid = env.client.post("/v1/videos", json=BODY, headers=bearer(key)).json()["id"]
    assert wallet(env, uid)[1] == PRICE
    run_one(env)
    v = env.client.get(f"/v1/videos/{vid}", headers=bearer(key)).json()
    assert v["status"] == "failed" and v["error"]["code"] == code and v["expires_at"] is None
    assert v["error"]["message"] and "flow" not in v["error"]["message"]   # the upstream name never leaks
    assert wallet(env, uid) == (12_000 * SOM, 0)
    rows = usage(env, uid)
    assert len(rows) == 1 and rows[0]["status_code"] == status and rows[0]["cost"] == 0 and rows[0]["error"]
    assert charges(env, uid) == []
    r = env.client.get(f"/v1/videos/{vid}/content", headers=bearer(key))
    assert r.status_code == 404 and r.json()["error"]["code"] == "video_not_ready"
    consistent(env)


def test_another_users_video_is_not_found(env, cluster):
    a = add_user(env, "a@test.uz", balance_som=12_000)
    b = add_user(env, "b@test.uz", balance_som=12_000)
    key_a, _ = add_key(env, a)
    key_b, _ = add_key(env, b)
    vid = env.client.post("/v1/videos", json=BODY, headers=bearer(key_a)).json()["id"]
    wait_done(env, vid, key_a)
    for path in (f"/v1/videos/{vid}", f"/v1/videos/{vid}/content"):
        r = env.client.get(path, headers=bearer(key_b))
        assert r.status_code == 404 and r.json()["error"]["code"] == "video_not_found"
    assert env.client.get("/v1/videos", headers=bearer(key_b)).json()["data"] == []
    assert env.client.get("/v1/videos/not-an-id", headers=bearer(key_a)).status_code == 404
    assert env.client.get(f"/v1/videos/{vid}").status_code == 401


def test_only_video_models_and_real_prompts_are_accepted(env, cluster):
    add_model(env, "chat-cluster", upstream="qwen", provider="cluster")
    add_model(env, "chat-local", upstream="qwen3:14b", provider="local")
    uid = add_user(env, balance_som=12_000)
    key, _ = add_key(env, uid)
    for model in ("chat-cluster", "chat-local"):
        r = env.client.post("/v1/videos", json={**BODY, "model": model}, headers=bearer(key))
        assert r.status_code == 400 and r.json()["error"]["code"] == "unsupported_endpoint"
    r = env.client.post("/v1/videos", json={**BODY, "model": "no-such-model"}, headers=bearer(key))
    assert r.status_code == 404 and "veo-1" in r.json()["error"]["message"]
    for prompt in ("", "   ", None, 42):
        r = env.client.post("/v1/videos", json={**BODY, "prompt": prompt}, headers=bearer(key))
        assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_prompt"
    assert env.client.post("/v1/videos", json={**BODY, "seconds": "abc"}, headers=bearer(key)).json()["error"]["code"] == "invalid_seconds"
    assert env.client.post("/v1/videos", json={**BODY, "size": "big"}, headers=bearer(key)).json()["error"]["code"] == "invalid_size"
    r = env.client.post("/v1/videos", json={**BODY, "input_reference": "https://example.com/cat.png"}, headers=bearer(key))
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_image"
    assert env.client.post("/v1/videos", content=b"not json", headers=bearer(key)).json()["error"]["code"] == "invalid_json"
    assert env.db.scalar("SELECT COUNT(*) FROM video_jobs") == 0
    assert wallet(env, uid) == (12_000 * SOM, 0)
    # Rejections are in the usage log like rejected chat requests, at no cost
    assert {r["endpoint"] for r in usage(env, uid)} == {"videos"} and {r["cost"] for r in usage(env, uid)} == {0}


def test_multipart_upload_sends_the_image_as_a_data_uri(env, cluster):
    paused(env)
    uid = add_user(env, balance_som=12_000)
    key, _ = add_key(env, uid)
    r = env.client.post("/v1/videos", headers=bearer(key),
                        data={"model": "veo-1", "prompt": "Animate this", "seconds": "4", "size": "720x1280"},
                        files={"input_reference": ("ref.png", PNG, "image/png")})
    assert r.status_code == 200, r.text
    assert (r.json()["seconds"], r.json()["size"]) == ("4", "720x1280")
    run_one(env)
    body = cluster["calls"][0]["body"]
    assert body["image"] == "data:image/png;base64," + base64.b64encode(PNG).decode()
    assert (body["prompt"], body["seconds"], body["size"]) == ("Animate this", 4, "720x1280")
    v = env.client.get(f"/v1/videos/{r.json()['id']}", headers=bearer(key)).json()
    assert v["status"] == "completed" and (v["seconds"], v["size"]) == ("8", "1280x720")   # what the cluster made
    assert env.db.one("SELECT input_image FROM video_jobs")["input_image"] is None
    # A JSON data URI works the same way; a file that is not an image does not
    r = env.client.post("/v1/videos", headers=bearer(key),
                        json={**BODY, "input_reference": "data:image/png;base64," + base64.b64encode(PNG).decode()})
    assert r.status_code == 200
    r = env.client.post("/v1/videos", headers=bearer(key), data={"model": "veo-1", "prompt": "x"},
                        files={"input_reference": ("ref.png", b"<svg/>", "image/png")})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_image"


def test_jobs_left_running_by_a_dead_process_are_failed_and_released(env, cluster):
    paused(env)
    uid = add_user(env, balance_som=12_000)
    key, _ = add_key(env, uid)
    dead = env.client.post("/v1/videos", json=BODY, headers=bearer(key)).json()["id"]
    live = env.client.post("/v1/videos", json=BODY, headers=bearer(key)).json()["id"]
    with env.db.tx() as c:
        c.execute("UPDATE video_jobs SET status='in_progress', owner='old-host:1:gone', started_at=1 WHERE id=?", (dead,))
        c.execute("UPDATE video_jobs SET status='in_progress', owner=?, started_at=1 WHERE id=?",
                  (env.app.state.instance_id, live))
    assert wallet(env, uid) == (12_000 * SOM, 2 * PRICE)

    assert videos.recover(env.db) == 1
    v = env.client.get(f"/v1/videos/{dead}", headers=bearer(key)).json()
    assert v["status"] == "failed" and v["error"]["code"] == "generation_interrupted"
    assert env.client.get(f"/v1/videos/{live}", headers=bearer(key)).json()["status"] == "in_progress"
    assert wallet(env, uid) == (12_000 * SOM, PRICE)
    assert [(r["request_id"], r["cost"]) for r in usage(env, uid)] == [(dead, 0)]
    consistent(env)
    # A second pass finds nothing more; a late finish of the failed job changes nothing
    assert videos.recover(env.db) == 0
    assert videos.finish_job(env.db, dead, status_code=200, clip=MP4) is None
    assert wallet(env, uid) == (12_000 * SOM, PRICE)


def test_list_is_newest_first_and_clips_expire(env, cluster):
    uid = add_user(env, balance_som=20_000)
    key, _ = add_key(env, uid)
    ids = [env.client.post("/v1/videos", json=BODY, headers=bearer(key)).json()["id"] for _ in range(3)]
    for vid in ids:
        wait_done(env, vid, key)
    page = env.client.get("/v1/videos?limit=2", headers=bearer(key)).json()
    assert page["object"] == "list" and [v["id"] for v in page["data"]] == ids[::-1][:2] and page["has_more"]
    rest = env.client.get(f"/v1/videos?limit=2&after={page['last_id']}", headers=bearer(key)).json()
    assert [v["id"] for v in rest["data"]] == [ids[0]] and not rest["has_more"]
    assert len(cluster["calls"]) == 3

    with env.db.tx() as c:
        c.execute("UPDATE video_jobs SET completed_at = completed_at - 90000 WHERE id=?", (ids[0],))
        c.execute("UPDATE video_files SET created_at = created_at - 90000 WHERE job_id=?", (ids[0],))
    r = env.client.get(f"/v1/videos/{ids[0]}/content", headers=bearer(key))
    assert r.status_code == 404 and r.json()["error"]["code"] == "video_expired"
    assert videos.purge_files(env.db) == 1
    assert env.db.scalar("SELECT COUNT(*) FROM video_files") == 2
    assert env.client.get(f"/v1/videos/{ids[1]}/content", headers=bearer(key)).content == MP4
