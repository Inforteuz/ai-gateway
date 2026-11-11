"""Local Qwen and the Gemini pool say who they are from the brand, not "Men Qwen" (2026-11-12)."""
from app import branding
from .conftest import CHAT, add_key, add_user, bearer


def test_brand_line_merges_into_the_clients_system_prompt():
    msgs = branding.with_identity([{"role": "system", "content": "Be short."}, {"role": "user", "content": "hi"}])
    assert len(msgs) == 2 and msgs[0]["content"].startswith(branding.IDENTITY_NOTE) and msgs[0]["content"].endswith("Be short.")
    msgs = branding.with_identity([{"role": "user", "content": "sen kimsan?"}])
    assert msgs[0] == {"role": "system", "content": branding.IDENTITY_NOTE} and msgs[1]["role"] == "user"
    parts = branding.with_identity([{"role": "system", "content": [{"type": "text", "text": "x"}]}])
    assert parts[0]["content"][0]["text"] == branding.IDENTITY_NOTE


def test_local_model_gets_the_brand_line(env):
    uid = add_user(env, balance_som=100_000)
    key, _ = add_key(env, uid)
    r = env.client.post("/v1/chat/completions", headers=bearer(key), json=CHAT)
    assert r.status_code == 200, r.text
    sent = env.up["last"]["messages"]
    assert sent[0]["role"] == "system" and "never name the underlying model" in sent[0]["content"]
    assert sent[-1] == CHAT["messages"][-1]
