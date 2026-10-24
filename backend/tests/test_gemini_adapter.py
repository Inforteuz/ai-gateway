"""Gemini adapter: registration, code-label cleanup and the multimodal path."""
import asyncio

import pytest

from cluster import cdp
from cluster.adapters import get_adapter
from cluster.adapters.base_adapter import BaseAIAdapter
from cluster.adapters.gemini_adapter import GeminiAdapter, _fix_code_labels
from cluster.pool import detect_model_from_url


def test_gemini_is_registered_and_detected_by_host():
    assert get_adapter("gemini") is not None
    assert detect_model_from_url("https://gemini.google.com/app/12dd9d5d") == "gemini"
    assert detect_model_from_url("https://accounts.google.com/signin") is None


def test_chip_stands_for_the_configured_model():
    """The chip shows a short, translated name; the menu shows the full one."""
    assert GeminiAdapter._chip_is("Pro", "3.1 Pro")
    assert GeminiAdapter._chip_is("Flash", "3.6 Flash")
    assert GeminiAdapter._chip_is("Flash-Lite", "3.5 Flash-Lite")
    assert GeminiAdapter._chip_is("Argon", "4 Argon")
    # "Flash" must not pass for Flash-Lite, nor Pro for Flash
    assert not GeminiAdapter._chip_is("Flash", "3.5 Flash-Lite")
    assert not GeminiAdapter._chip_is("Pro", "3.6 Flash")
    # An account with no paid plan sits on "Auto": the menu has to be opened
    assert not GeminiAdapter._chip_is("Gemini Авто", "3.1 Pro")
    assert not GeminiAdapter._chip_is("", "3.1 Pro")


def test_variant_is_offered_only_when_a_fallback_model_differs(monkeypatch):
    from cluster import settings as ccfg
    g = GeminiAdapter()
    monkeypatch.setattr(ccfg, "GEMINI_MODEL", "3.1 Pro")
    monkeypatch.setattr(ccfg, "GEMINI_FALLBACK_MODEL", "")
    assert not g.has_variant("fallback")
    monkeypatch.setattr(ccfg, "GEMINI_FALLBACK_MODEL", "3.1 Pro")
    assert not g.has_variant("fallback")
    monkeypatch.setattr(ccfg, "GEMINI_FALLBACK_MODEL", "3.6 Flash")
    assert g.has_variant("fallback")


def test_unconfigured_picker_is_left_alone(monkeypatch):
    """The default must not touch the account's own choice."""
    from cluster import settings as ccfg
    monkeypatch.setattr(ccfg, "GEMINI_MODEL", "")
    assert asyncio.run(GeminiAdapter().select_variant(object(), None)) is None


def test_code_label_moves_into_opening_fence_only():
    assert _fix_code_labels("Python\n```\nx = 1\n```") == "```python\nx = 1\n```"
    # A code line right before the closing fence ("fi") is not taken as a language name
    assert _fix_code_labels("Bash\n```\nif x; then\necho\nfi\n```\nTamom") == \
        "```bash\nif x; then\necho\nfi\n```\nTamom"
    assert _fix_code_labels("Oddiy matn\nikkinchi") == "Oddiy matn\nikkinchi"


# --- Multimodal (measured live) -------------------------------------------------


def test_multimodal_caps_enabled_and_exposed_in_catalog():
    c = GeminiAdapter.caps
    assert (c.vision, c.files, c.image_gen, c.delete_chats) == (True, True, True, True)
    from app.catalog import _cluster_caps
    caps = _cluster_caps("gemini")
    assert caps["vision"] and caps["file_input"] and caps["image_generation"]


def test_upload_prefers_paste_and_removes_russian_chip():
    assert GeminiAdapter.UPLOAD_METHODS[0] == "paste"
    assert 'закрыть прикрепленный' in GeminiAdapter.ATTACHMENT_REMOVE


def test_upload_state_counts_preview_chips(monkeypatch):
    async def fake_eval(ws, js, timeout=20.0, **kw):
        assert "uploader-file-preview" in js
        return {"imgs": 2, "named": 5, "busy": True}
    monkeypatch.setattr(cdp, "evaluate", fake_eval)
    st = asyncio.run(GeminiAdapter().upload_state(None, ["a.txt"]))
    # File names are not counted separately — one chip is one marker for both images and documents
    assert st == {"imgs": 2, "named": 0, "busy": True}


@pytest.mark.parametrize("consent", [False, True])
def test_attach_files_never_accepts_upload_consent(monkeypatch, consent):
    calls = []

    async def fake_super(self, ws, paths, images_only=False, timeout=40.0):
        return True

    async def fake_eval(ws, js, timeout=20.0, **kw):
        calls.append(js)
        return consent

    async def fake_clear(self, ws, timeout=8.0):
        calls.append("clear")
        return 1

    monkeypatch.setattr(BaseAIAdapter, "attach_files", fake_super)
    monkeypatch.setattr(GeminiAdapter, "clear_attachments", fake_clear)
    monkeypatch.setattr(cdp, "evaluate", fake_eval)
    ok = asyncio.run(GeminiAdapter().attach_files(None, ["/x/doira.png"]))
    assert ok is (not consent)
    # Only "Отмена" (Cancel) is clicked in the dialog — never "Принять" (Accept)
    assert "Отмена" in calls[0] and "Принять" not in calls[0]
    assert ("clear" in calls) is consent


def test_download_images_reads_blob_through_canvas(monkeypatch):
    async def fake_eval(ws, js, timeout=20.0, **kw):
        assert "toDataURL" in js and "blob:https://gemini.google.com/abc" in js
        return "data:image/png;base64,QUJD"

    async def fake_fetch(ws, url, **kw):
        return ("image/jpeg", "WFla")

    monkeypatch.setattr(cdp, "evaluate", fake_eval)
    monkeypatch.setattr(cdp, "fetch_as_base64", fake_fetch)
    out = asyncio.run(GeminiAdapter().download_images(
        None, ["blob:https://gemini.google.com/abc", "https://lh3.googleusercontent.com/x"]))
    assert out == [("image/png", "QUJD"), ("image/jpeg", "WFla")]


def test_images_read_only_from_last_model_response():
    js = GeminiAdapter._IMAGES_JS
    assert "model-response" in js and "user-query" in js


_real_sleep = asyncio.sleep


def _no_sleep(_s):
    return _real_sleep(0)


def test_attach_files_reloads_and_retries_once(monkeypatch):
    results, steps = [False, True], []

    async def fake_super(self, ws, paths, images_only=False, timeout=40.0):
        steps.append("attach")
        return results.pop(0)

    async def fake_eval(ws, js, timeout=20.0, **kw):
        return False if "на основе изображений" in js else {"chips": 0}

    async def fake_step(name):
        async def f(self, ws, *a, **kw):
            steps.append(name)
        return f

    monkeypatch.setattr(BaseAIAdapter, "attach_files", fake_super)
    monkeypatch.setattr(cdp, "evaluate", fake_eval)
    for name in ("clear_attachments", "goto", "reset_chat"):
        monkeypatch.setattr(GeminiAdapter, name, asyncio.run(fake_step(name)))
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    assert asyncio.run(GeminiAdapter().attach_files(None, ["/x/doira.png"])) is True
    assert steps == ["attach", "clear_attachments", "goto", "reset_chat", "attach"]


def test_clear_attachments_clicks_one_chip_per_round(monkeypatch):
    chips = [1, 1]

    async def fake_eval(ws, js, timeout=20.0, **kw):
        assert "uploader-file-preview" in js
        return bool(chips and chips.pop())

    monkeypatch.setattr(cdp, "evaluate", fake_eval)
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    assert asyncio.run(GeminiAdapter().clear_attachments(None)) == 2
