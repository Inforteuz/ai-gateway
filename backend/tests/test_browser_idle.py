import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from cluster import server
from cluster.adapters import gemini_adapter as gemini
from cluster.adapters.base_adapter import BaseAIAdapter


@pytest.mark.parametrize('ready', [True, False])
def test_quiet_background_is_applied_before_wait_and_after_a_new_document(monkeypatch, ready):
    events = []
    async def prepare(self, ws): events.append('quiet'); return True
    async def wait(self, ws, timeout, selector):
        events.append(('wait', timeout, selector)); return ready
    monkeypatch.setattr(gemini.GeminiAdapter, 'prepare_page', prepare)
    monkeypatch.setattr(BaseAIAdapter, 'wait_for_input', wait)
    assert asyncio.run(gemini.GeminiAdapter().wait_for_input(None, timeout=7)) is ready
    assert events == ['quiet', ('wait', 7, gemini.GEMINI_INPUT)] + (['quiet'] if ready else [])


def test_style_failure_does_not_misclassify_a_session(monkeypatch):
    async def evaluate(*args, **kwargs): raise RuntimeError('document navigated')
    async def check(self, ws, timeout, recover): return True, 'ok'
    monkeypatch.setattr(gemini.cdp, 'evaluate', evaluate)
    monkeypatch.setattr(BaseAIAdapter, 'check_session', check)
    assert asyncio.run(gemini.GeminiAdapter().check_session(None, recover=False)) == (True, 'ok')


def test_background_startup_preparation_skips_busy_workers_and_releases_locks():
    async def run():
        seen = []
        @asynccontextmanager
        async def connection(): yield 'idle-page'
        async def connect(**kwargs): return connection()
        async def prepare(ws): seen.append(ws)
        idle = SimpleNamespace(model_name='gemini', adapter=SimpleNamespace(prepare_page=prepare),
                               lock=asyncio.Lock(), connect=connect)
        busy = SimpleNamespace(model_name='gemini', adapter=idle.adapter,
                               lock=asyncio.Lock(), connect=connect)
        await busy.lock.acquire()
        await server._prepare_worker_pages(SimpleNamespace(workers=[idle, busy]))
        assert seen == ['idle-page'] and not idle.lock.locked() and busy.lock.locked()
        busy.lock.release()
    asyncio.run(run())


def test_cancelling_startup_preparation_releases_the_worker():
    async def run():
        entered = asyncio.Event()
        @asynccontextmanager
        async def connection(): yield object()
        async def connect(**kwargs): return connection()
        async def prepare(ws): entered.set(); await asyncio.Event().wait()
        worker = SimpleNamespace(model_name='gemini', adapter=SimpleNamespace(prepare_page=prepare),
                                 lock=asyncio.Lock(), connect=connect)
        task = asyncio.create_task(server._prepare_worker_pages(SimpleNamespace(workers=[worker])))
        await entered.wait()
        assert worker.lock.locked()
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert not worker.lock.locked()
    asyncio.run(run())
