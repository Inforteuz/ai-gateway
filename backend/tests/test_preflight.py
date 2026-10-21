"""After a restart, an unchecked worker's session is verified before a request is sent to it."""
import asyncio

import pytest

from cluster import pool as pool_mod


class _Conn:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *a):
        return False


def _tab(results):
    calls = []

    class A:
        async def check_session(self, ws, timeout=5.0, recover=True):
            calls.append(recover)
            return results.pop(0)

    tab = object.__new__(pool_mod.WorkerTab)
    tab.adapter = A()
    tab.model_name, tab.browser_port, tab.tab_id = "deepseek", 9224, "T"
    tab.last_probe, tab.needs_attention, tab.cooldown_until, tab.last_error = None, None, 0.0, None

    async def connect(**kw):
        return _Conn()

    tab.connect = connect
    return tab, calls


def test_dead_session_is_caught_before_sending():
    tab, calls = _tab([(False, "no_chat_input"), (False, "no_chat_input")])
    with pytest.raises(RuntimeError):
        asyncio.run(tab._preflight_session())
    assert calls == [False, True] and tab.needs_attention == "login" and tab.cooldown_until > 0


def test_healthy_session_checked_once_after_restart():
    tab, calls = _tab([(True, "ok")])
    asyncio.run(tab._preflight_session())
    asyncio.run(tab._preflight_session())          # not checked a second time
    assert calls == [False] and tab.needs_attention is None
