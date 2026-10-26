"""Keyword-based checks only look at the text they are meant for."""
from cluster import pool as P


def test_code_answer_with_service_words_is_ok():
    code = ("```python\nimport time\n\ndef call():\n    try:\n        return api()\n"
            "    except RateLimit:\n        print('rate limit, please wait and try again later')\n```")
    assert P.classify_response(code) == (P.CLASS_OK, "")


def test_long_answer_with_service_words_is_ok():
    answer = "Captcha va puzzle haqida: " + "batafsil tushuntirish. " * 30 + "Something went wrong bo'lsa qayta urining."
    assert P.classify_response(answer)[0] == P.CLASS_OK


def test_answer_about_topic_user_asked_is_ok():
    assert P.classify_response("Rate limit — vaqt birligidagi so'rovlar chegarasi.",
                               "rate limit nima?")[0] == P.CLASS_OK


def test_real_short_banners_still_detected():
    assert P.classify_response("You've reached your limit. Upgrade your plan.")[0] == P.CLASS_QUOTA
    assert P.classify_response("Grok is under heavy usage right now. High demand.")[0] == P.CLASS_PROVIDER_ERROR
    assert P.classify_response("Session expired. Please log in.")[0] == P.CLASS_LOGIN


def test_repeated_digits_are_valid_answers():
    assert not P.is_degenerate_response("3333")
    assert not P.is_degenerate_response("1111")
    assert P.is_degenerate_response("....")
    assert P.is_degenerate_response("")


def test_matrix_of_zeros_is_not_a_loop():
    from app import loops
    code = "grid = [\n" + "    [0, 0, 0, 0, 0, 0, 0, 0],\n" * 25 + "]\n"
    assert not loops.is_looping(code)


def test_real_degenerate_loop_still_caught():
    from app import loops
    assert loops.is_looping("Javob: " + "men takrorlayapman va to'xtamayapman. " * 80)


def test_oversized_local_request_gets_context_error(env):
    from .conftest import add_key, add_user, bearer
    uid = add_user(env, "ctx@test.uz", balance_som=1000)
    key, _ = add_key(env, uid)
    big = "salom dunyo " * 7000            # ~84k chars ≈ 21k tokens > 16384
    r = env.client.post("/v1/chat/completions", headers=bearer(key),
                        json={"model": "model-1.0", "messages": [{"role": "user", "content": big}]})
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "context_length_exceeded" and "maximum context length" in err["message"]


def test_tools_count_toward_context(env):
    from app.gateway import context_tokens
    tools = [{"type": "function", "function": {"name": f"t{i}", "description": "x" * 400, "parameters": {}}} for i in range(20)]
    without = context_tokens({"messages": [{"role": "user", "content": "salom"}]})
    with_tools = context_tokens({"messages": [{"role": "user", "content": "salom"}], "tools": tools})
    assert with_tools - without > 2000


def test_local_models_advertise_tool_calling(env):
    from .conftest import add_key, add_user, bearer
    uid = add_user(env, "cap@test.uz")
    key, _ = add_key(env, uid)
    data = {m["id"]: m for m in env.client.get("/v1/models", headers=bearer(key)).json()["data"]}
    assert data["model-1.0"]["capabilities"]["tool_calling"] is True
    assert data["model-1.0"]["capabilities"]["vision"] is False


def test_brand_mask_only_self_identification():
    from cluster.compose import NAME, mask_brand as m
    assert m("Men ChatGPT'man, OpenAI tomonidan yaratilganman.") == f"Men {NAME}'man, {NAME} tomonidan yaratilganman."
    assert m("I am Claude, made by Anthropic.") == f"I am {NAME}, made by {NAME}."
    for keep in ("```python\nfrom openai import OpenAI\nclient = OpenAI()\n```",
                 "Faylni /home/user/projects/my-app/hello.py ga yozdim.",
                 "ChatGPT va Gemini ni solishtirsak: ChatGPT matnda kuchli.",
                 "`qwen3:14b` modeli va `gpt-4o` ishlatiladi.",
                 "pip install anthropic openai", "Sayt: openai.com va example.uz"):
        assert m(keep) == keep, keep


def test_tool_call_arguments_are_not_brand_masked():
    from cluster import tools as tp
    from cluster.compose import mask_brand
    raw = ('```yaml\ntool_calls:\n  - name: write\n    arguments:\n'
           '      path: /home/user/my-app/hello.py\n      content: "from openai import OpenAI"\n```')
    calls, rest = tp.parse_tool_calls(raw, ["write"])
    rest = mask_brand(rest)
    if calls:
        args = calls[0]["function"]["arguments"]
        assert "my-app" in args and "openai" in args


def test_filename_line_joined_to_fence_for_aider():
    from cluster.compose import tighten_filename_fences as t
    assert t("Yangi fayl:\n\nhello.py\n\n```\nprint(1)\n```") == "Yangi fayl:\n\nhello.py\n```\nprint(1)\n```"
    assert t("**src/app.js**\n\n```js\nx\n```") == "**src/app.js**\n```js\nx\n```"
    for keep in ("Mana kod:\n\n```py\nx\n```", "Fayl hello.py ichida.\n\n```\nx\n```"):
        assert t(keep) == keep


def test_fit_prompt_keeps_task_and_tools_drops_old_history(monkeypatch):
    from cluster import settings as cfg
    from cluster.compose import compile_prompt, fit_prompt
    # Disable the normal agent history cap here so this test still exercises
    # the provider-size fallback itself.
    monkeypatch.setattr(cfg, "AGENT_MAX_HISTORY_TURNS", 64)
    msgs = [{"role": "system", "content": "Agent ko'rsatmasi. " * 400}]
    for i in range(30):
        msgs += [{"role": "user", "content": f"eski savol {i} " + "x" * 900},
                 {"role": "assistant", "content": f"eski javob {i} " + "y" * 900}]
    msgs.append({"role": "user", "content": "OXIRGI VAZIFA: hello.py yarat"})
    tools_instr = "[Vositalar — MAJBURIY protokol]\nwrite: faylga yozadi"
    p = compile_prompt(msgs, tools_instruction=tools_instr) if "tools_instruction" in compile_prompt.__code__.co_varnames else compile_prompt(msgs)
    assert len(p) > 31000
    f = fit_prompt(p, 31000)
    assert len(f) <= 31000
    assert "OXIRGI VAZIFA: hello.py yarat" in f
    assert "eski savol 29" in f and "eski savol 0 " not in f      # the newest stays, the oldest goes
    assert "qisqartirildi" in f and "[Vositalar — MAJBURIY protokol]" in f


def test_fit_prompt_noop_when_short():
    from cluster.compose import fit_prompt
    assert fit_prompt("[System]:\nqisqa\n\n[Current task]:\nsalom", 31000).endswith("salom")
