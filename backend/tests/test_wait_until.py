"""Polling instead of fixed sleeps: returns as soon as the page is ready, gives up at the timeout."""
import asyncio
import time

from cluster.adapters import base_adapter as BA
from cluster.adapters.deepai_adapter import DeepAIAdapter


def test_wait_until_returns_as_soon_as_ready(monkeypatch):
    answers = iter([False, False, True])

    async def fake_eval(ws, js, timeout=5):
        return next(answers)

    monkeypatch.setattr(BA.cdp, "evaluate", fake_eval)
    a = DeepAIAdapter()
    t0 = time.monotonic()
    assert asyncio.run(a.wait_until(None, "x", timeout=3.0, interval=0.05)) is True
    assert time.monotonic() - t0 < 0.5


def test_wait_until_gives_up_and_survives_page_errors(monkeypatch):
    async def failing(ws, js, timeout=5):
        raise RuntimeError("page re-rendering")

    monkeypatch.setattr(BA.cdp, "evaluate", failing)
    t0 = time.monotonic()
    assert asyncio.run(DeepAIAdapter().wait_until(None, "x", timeout=0.3, interval=0.05)) is False
    assert 0.3 <= time.monotonic() - t0 < 1.0


def test_deepai_fresh_check_requires_a_clean_url_and_no_answers():
    js = DeepAIAdapter._FRESH_JS
    # a sent message adds #<uuid> before any answer appears; answers render as these classes
    assert "!location.hash" in js and ".markdownContainer, .outputBox" in js and "box.value === ''" in js
