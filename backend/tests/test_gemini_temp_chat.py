"""Gemini: every request in a temporary chat — cold load after a Chrome restart.

The first request after a restart ended up in the chat history: the input field
was ready, but the "Временный чат" (Temporary chat) button appeared a few seconds
later and the adapter took that for logged-out mode.
"""
import asyncio
import types

import pytest

from cluster.adapters import gemini_adapter as ga
from cluster.adapters.gemini_adapter import GeminiAdapter


class FakeGeminiPage:
    """`button_after` — the button appears after this many checks; `logged_out` — logged-out mode."""

    def __init__(self, button_after=0, logged_out=False, temp=False, drawer=False):
        self.button_after, self.logged_out, self.temp = button_after, logged_out, temp
        self.drawer = drawer
        self.button_checks, self.clicks = 0, 0

    async def evaluate(self, ws, js, timeout=20.0, await_promise=False):
        if js == ga._TEMP_STATE_JS:
            return self.temp
        if js == ga._TEMP_BUTTON_JS:
            self.button_checks += 1
            if self.logged_out or self.button_checks <= self.button_after:
                return None
            return {"x": 10, "y": 20}
        if js == ga._LOGGED_OUT_JS:
            return self.logged_out
        if js == GeminiAdapter._DRAWER_OPEN_JS:
            return self.drawer
        if js == GeminiAdapter._CLOSE_PANEL_JS:
            self.drawer = False
            return True
        if "новый чат" in js:                  # no SPA "Новый чат" (New chat) button
            return False
        if "model-response" in js and "rich-textarea" in js:   # is_fresh_chat: clean /app
            return True
        raise AssertionError("unexpected JS: " + js[:80])

    async def click_at(self, ws, x, y):
        self.clicks += 1
        if not self.drawer:            # an open drawer swallows the click
            self.temp = True


@pytest.fixture
def page(monkeypatch):
    async def fast_sleep(_):
        return None
    monkeypatch.setattr(ga, "asyncio", types.SimpleNamespace(sleep=fast_sleep))

    def install(fake):
        monkeypatch.setattr(ga, "cdp", fake)
        a = GeminiAdapter()

        async def goto(ws, url):
            return None

        async def wait_for_input(ws, timeout=10.0, selector=None):
            return True
        a.goto, a.wait_for_input = goto, wait_for_input
        return a
    return install


def test_reset_waits_for_late_temp_button_after_cold_start(page):
    fake = FakeGeminiPage(button_after=4)          # the button shows up ~2 s late
    a = page(fake)
    asyncio.run(a.reset_chat(object()))
    assert fake.clicks == 1 and fake.temp is True


def test_reset_closes_open_drawer_before_clicking(page):
    fake = FakeGeminiPage(drawer=True)
    asyncio.run(page(fake).reset_chat(object()))
    assert fake.drawer is False and fake.clicks == 1 and fake.temp is True


def test_reset_logged_out_does_not_wait_or_click(page):
    fake = FakeGeminiPage(logged_out=True)
    asyncio.run(page(fake).reset_chat(object()))
    assert fake.clicks == 0 and fake.button_checks == 1


def test_reset_already_temporary_is_not_toggled_off(page):
    fake = FakeGeminiPage(temp=True)
    asyncio.run(page(fake).reset_chat(object()))
    assert fake.clicks == 0 and fake.button_checks == 0


def test_fresh_check_rejects_page_before_temp_button_appears(page):
    # A regular page before the button appears — used to be accepted as "clean"
    assert asyncio.run(page(FakeGeminiPage(button_after=99)).is_fresh_chat(object())) is False
    assert asyncio.run(page(FakeGeminiPage(temp=True)).is_fresh_chat(object())) is True
    assert asyncio.run(page(FakeGeminiPage(logged_out=True)).is_fresh_chat(object())) is True


def test_temp_state_prefers_chat_window_class():
    assert "chat-window.is-temporary-chat" in ga._TEMP_STATE_JS
