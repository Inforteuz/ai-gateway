"""Catalog speed trend and per-language model descriptions (2026-11-12)."""
import pytest

from app.routes_account import _real_speed
from .conftest import CSRF, add_model, add_user
from .test_models_refine import login_admin


def _log(env, model, latencies, uid):
    """Oldest first; the speed code reads the newest rows first."""
    with env.db.tx() as c:
        for i, ms in enumerate(latencies):
            c.execute("INSERT INTO usage_logs (request_id, user_id, model, endpoint, status_code, latency_ms, created_at)"
                      " VALUES (?, ?, ?, 'chat/completions', 200, ?, ?)", (f"{model}-{i}", uid, model, ms, 1000 + i))


@pytest.mark.parametrize("before, now, trend", [
    (2000, 5000, "slower"),
    (5000, 2000, "faster"),
    (3000, 3200, "steady"),
])
def test_speed_trend_compares_the_latest_window_with_the_one_before(env, before, now, trend):
    add_model(env, "m-trend")
    uid = add_user(env)
    _log(env, "m-trend", [before] * 20 + [now] * 20, uid)
    got = _real_speed(env.db)["m-trend"]
    assert got["typical_latency_ms"] == now and got["previous_latency_ms"] == before and got["trend"] == trend


def test_no_trend_without_an_earlier_window(env):
    add_model(env, "m-new")
    uid = add_user(env)
    _log(env, "m-new", [3000] * 8, uid)
    got = _real_speed(env.db)["m-new"]
    assert got["typical_latency_ms"] == 3000 and got["trend"] is None and got["previous_latency_ms"] is None


def test_descriptions_per_language_reach_the_catalog(env):
    login_admin(env)
    add_model(env, "m-text")
    r = env.client.patch("/portal/admin/models/m-text", headers=CSRF, json={
        "description": "Tezkor model", "description_ru": "Быстрая модель", "description_en": "Fast model"})
    assert r.status_code == 200, r.text
    item = next(m for m in env.client.get("/portal/models").json()["items"] if m["id"] == "m-text")
    assert (item["description"], item["description_ru"], item["description_en"]) == ("Tezkor model", "Быстрая модель", "Fast model")
    # A patch that leaves them out keeps them
    env.client.patch("/portal/admin/models/m-text", headers=CSRF, json={"display_name": "M"})
    item = next(m for m in env.client.get("/portal/models").json()["items"] if m["id"] == "m-text")
    assert item["description_ru"] == "Быстрая модель"


def _video_job(env, uid, i, status, created, completed, model="sable-video"):
    with env.db.tx() as c:
        c.execute("INSERT INTO video_jobs (id, user_id, model, public_model, upstream, prompt, status, seconds, size,"
                  " created_at, completed_at) VALUES (?, ?, ?, ?, 'flow', 'p', ?, '8', '1280x720', ?, ?)",
                  (f"video_{i:024d}", uid, model, model, status, created, completed))


def test_video_models_get_time_and_success_from_their_jobs(env):
    """Clips are async jobs: the catalog showed a dash although their real durations were known."""
    add_model(env, "sable-video", upstream="flow", provider="cluster")
    uid = add_user(env)
    _video_job(env, uid, 1, "completed", 1000, 1068)
    _video_job(env, uid, 2, "completed", 2000, 2072)
    _video_job(env, uid, 3, "failed", 3000, 3030)
    got = _real_speed(env.db)["sable-video"]
    assert got["typical_latency_ms"] == 70000 and got["samples"] == 2 and got["success_rate"] == round(2 / 3, 3)


def test_one_real_image_pro_measurement_is_shown(env):
    add_model(env, "sable-image-pro", upstream="image-pro", provider="cluster")
    uid = add_user(env)
    with env.db.tx() as c:
        c.execute("INSERT INTO usage_logs (request_id, user_id, model, endpoint, status_code, latency_ms, created_at)"
                  " VALUES ('ip-1', ?, 'sable-image-pro', 'images/generations', 200, 21000, 1000)", (uid,))
    assert _real_speed(env.db)["sable-image-pro"]["typical_latency_ms"] == 21000


def test_video_without_jobs_stays_unmeasured(env):
    add_model(env, "sable-video-pro", upstream="flow-pro", provider="cluster")
    assert "sable-video-pro" not in _real_speed(env.db)


def test_pictures_do_not_slow_down_a_text_models_speed(env):
    add_model(env, "sable-3.1", upstream="qwen", provider="cluster")
    uid = add_user(env)
    _log(env, "sable-3.1", [2000] * 6, uid)
    with env.db.tx() as c:
        for i in range(6):
            c.execute("INSERT INTO usage_logs (request_id, user_id, model, endpoint, status_code, latency_ms, created_at)"
                      " VALUES (?, ?, 'sable-3.1', 'images/generations', 200, 150000, ?)", (f"img-{i}", uid, 5000 + i))
    assert _real_speed(env.db)["sable-3.1"]["typical_latency_ms"] == 2000
