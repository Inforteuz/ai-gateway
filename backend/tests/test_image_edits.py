"""Image edits: parsing the multipart body, edit intent in chat, waiting for images."""
import asyncio
import base64
import json

import pytest

from app.common import ApiError
from app.gateway import count_attachments, parse_image_edit
from cluster import server
from cluster import pool as pool_mod

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _multipart(fields: list[tuple[str, bytes, str | None, str | None]]) -> tuple[bytes, str]:
    boundary = "sfTESTboundary"
    out = b""
    for name, data, filename, ctype in fields:
        out += f"--{boundary}\r\n".encode()
        disp = f'form-data; name="{name}"' + (f'; filename="{filename}"' if filename else "")
        out += f"Content-Disposition: {disp}\r\n".encode()
        if ctype:
            out += f"Content-Type: {ctype}\r\n".encode()
        out += b"\r\n" + data + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return out, f"multipart/form-data; boundary={boundary}"


def test_multipart_edit_is_converted_to_data_uris():
    raw, ctype = _multipart([
        ("model", b"model-4.0", None, None),
        ("prompt", "Fonini o‘zgartir".encode(), None, None),
        ("n", b"1", None, None),
        ("image", PNG, "mushuk.png", "image/png"),
    ])
    body = parse_image_edit(raw, ctype)
    assert body["model"] == "model-4.0"
    assert body["prompt"] == "Fonini o‘zgartir"
    assert body["n"] == 1
    assert body["image"] == ["data:image/png;base64," + base64.b64encode(PNG).decode()]
    assert count_attachments(body) == 1


def test_multipart_sniffs_octet_stream_and_rejects_non_image():
    raw, ctype = _multipart([("prompt", b"x", None, None),
                             ("image[]", PNG, "a.bin", "application/octet-stream")])
    assert parse_image_edit(raw, ctype)["image"][0].startswith("data:image/png;base64,")
    raw, ctype = _multipart([("prompt", b"x", None, None),
                             ("image", b"%PDF-1.4 hello", "a.pdf", "application/pdf")])
    with pytest.raises(ApiError) as e:
        parse_image_edit(raw, ctype)
    assert e.value.code == "invalid_image"


def test_json_edit_body_accepts_single_image():
    body = parse_image_edit(b'{"prompt": "x", "image": "data:image/png;base64,AAAA"}', "application/json")
    assert body["image"] == ["data:image/png;base64,AAAA"]
    with pytest.raises(ApiError):
        parse_image_edit(b"not json", "application/json")


def test_chat_edit_intent_needs_an_attached_image():
    assert server._wants_image_edit("Shu rasmning fonini o‘zgartir", True)
    assert server._wants_image_edit("Change the background to a beach", True)
    assert not server._wants_image_edit("Shu rasmning fonini o'zgartir", False)
    assert not server._wants_image_edit("Rasmda nima bor?", True)


class _Adapter:
    def __init__(self, finished: bool):
        self.finished = finished
        self.calls = 0

    async def read_images(self, ws):
        return []

    async def finished_without_image(self, ws):
        self.calls += 1
        return self.finished


def _tab(adapter):
    tab = object.__new__(pool_mod.WorkerTab)
    tab.adapter = adapter
    return tab


def test_wait_for_images_stops_early_when_model_answered_with_text(monkeypatch):
    monkeypatch.setattr(pool_mod.cfg, "IMAGE_IDLE_GRACE", 0)
    sleeps = []

    async def fake_sleep(sec):
        sleeps.append(sec)

    monkeypatch.setattr(pool_mod.asyncio, "sleep", fake_sleep)
    got = asyncio.run(_tab(_Adapter(finished=True))._wait_for_images(None, timeout=180))
    assert got == []
    assert len(sleeps) == 3   # three "finished, no image" in a row — then it stops


def test_reset_phase_uses_image_chat_for_image_requests():
    calls = []

    class A:
        async def reset_chat(self, ws, timeout=10.0):
            calls.append("temp")

        async def reset_chat_for_images(self, ws, timeout=10.0):
            calls.append("image")

    class Conn:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *a):
            return False

    tab = _tab(A())
    tab.model_name = "chatgpt"

    async def connect(**kw):
        return Conn()

    tab.connect = connect
    asyncio.run(tab._reset_phase(for_images=True))
    asyncio.run(tab._reset_phase())
    assert calls == ["image", "temp"]


def test_image_tokens_do_not_count_base64():
    big = base64.b64encode(b"x" * 300_000).decode()
    resp = server._completion_payload(answer="Mana rasm", images=[("image/png", big)], used_model="chatgpt",
                                      prompt_text="prompt", stream=False, fingerprint="fp", cached=False)
    usage = json.loads(resp.body)["usage"]
    assert usage["completion_tokens"] < 2000
    pay = server._image_payload("chatgpt", "chiz", [("image/png", big), ("image/png", big)])
    assert pay["usage"]["completion_tokens"] == 2 * server.cfg.IMAGE_OUTPUT_TOKENS


def test_wait_for_images_returns_once_generation_is_done(monkeypatch):
    class A:
        async def read_images(self, ws):
            return ["blob:rasm1"]

        async def generation_done(self, ws):
            return True

    async def fake_sleep(sec):
        pass

    monkeypatch.setattr(pool_mod.asyncio, "sleep", fake_sleep)
    calls = []
    a = A()
    orig = a.read_images

    async def counting(ws):
        calls.append(1)
        return await orig(ws)

    a.read_images = counting
    assert asyncio.run(_tab(a)._wait_for_images(None, timeout=180)) == ["blob:rasm1"]
    assert len(calls) == 2   # found once, confirmed once — no 3 s stability wait


def test_image_analysis_is_not_mistaken_for_edit():
    # An image-analysis request was taken for an edit and waited 110 s for an image
    assert not server._wants_image_edit("Rasmdagi kodni o'qib ber va kvadratlarni sana", True)
    assert not server._wants_image_edit("Rasmdagi orqa fon qanday rangda?", True)
    assert server._wants_image_edit("Rasmni tahrirla: fonini oq qil", True)


def test_qwen_daily_limit_text_is_quota():
    kind, kw = pool_mod.classify_response("You've reached today's chat limit. Upgrade your membership to unlock more chats.")
    assert kind == pool_mod.CLASS_QUOTA


def test_image_intent_only_from_last_user_message():
    msgs = [server.ChatMessage(role="system", content="You can draw diagrams and generate an image if asked."),
            server.ChatMessage(role="user", content="Salom"),
            server.ChatMessage(role="assistant", content="Salom!"),
            server.ChatMessage(role="user", content="Ishlayapsanmi")]
    assert server._last_user_text(msgs) == "Ishlayapsanmi"
    assert not server._wants_image(server._last_user_text(msgs))
    msgs.append(server.ChatMessage(role="user", content=[{"type": "text", "text": "Mushuk rasm chizib ber"}]))
    assert server._wants_image(server._last_user_text(msgs))


def test_model_without_image_gen_is_not_forced():
    class W:
        def __init__(self, name, img):
            self.model_name = name
            self.adapter = type("A", (), {"caps": type("C", (), {"image_gen": img})()})()

    class P:
        workers = [W("qwen", False), W("chatgpt", True)]

        def workers_for_model(self, name):
            return [w for w in self.workers if w.model_name == name]

    assert not server._model_can(P(), "qwen", "image_gen")
    assert server._model_can(P(), "chatgpt", "image_gen")
    assert server._model_can(P(), "auto", "image_gen")
