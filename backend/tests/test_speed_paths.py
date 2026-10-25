"""Latency reductions must preserve fresh-chat isolation and history ownership."""
import asyncio
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from cluster import pool, runmeta, settings as cfg
from cluster.adapters import chatgpt_adapter as chatgpt
from cluster.adapters.gemini_adapter import GeminiAdapter


@pytest.mark.parametrize('hint_age,fresh,expected',[(0,True,True),(3600,True,True),(0,False,False),(3600,False,False)])
def test_clean_chat_state_is_checked_even_without_a_recent_warm_hint(monkeypatch,hint_age,fresh,expected):
    checks=[]
    @asynccontextmanager
    async def connection():yield object()
    async def connect(open_timeout=5):return connection()
    async def is_fresh(ws):checks.append(1);return fresh
    worker=pool.WorkerTab(19001,'speed-test','gemini','ws://unused',SimpleNamespace(is_fresh_chat=is_fresh))
    worker.warm_since=time.time()-hint_age if hint_age else 0
    worker.connect=connect
    monkeypatch.setattr(cfg,'PREWARM_ENABLED',True)
    assert asyncio.run(worker._reuse_clean_chat()) is expected and checks==[1]


def test_image_requests_and_disabled_prewarm_do_not_reuse_chat(monkeypatch):
    worker=pool.WorkerTab(19001,'speed-test','gemini','ws://unused',object())
    assert asyncio.run(worker._reuse_clean_chat(for_images=True)) is False
    monkeypatch.setattr(cfg,'PREWARM_ENABLED',False)
    assert asyncio.run(worker._reuse_clean_chat()) is False


def test_prewarm_does_not_navigate_an_already_clean_chat(monkeypatch):
    events=[]
    @asynccontextmanager
    async def connection():yield object()
    async def connect(open_timeout=5):return connection()
    async def fresh(ws):return True
    async def reset():events.append('reset')
    worker=pool.WorkerTab(19002,'speed-test','gemini','ws://unused',SimpleNamespace(is_fresh_chat=fresh))
    worker.connect=connect;worker._reset_phase=reset
    monkeypatch.setattr(cfg,'PREWARM_ENABLED',True)
    asyncio.run(worker.prewarm())
    assert not events and worker.warm_since>0


def test_gemini_reuse_checks_user_messages_and_active_generation(monkeypatch):
    async def evaluate(ws,js,timeout=8):
        if 'const p =' in js:
            assert 'model-response, user-query' in js and 'offsetWidth' in js and '__STOP__' not in js
            return False
        raise AssertionError('A dirty chat must stop before temporary-mode check')
    from cluster.adapters import gemini_adapter
    monkeypatch.setattr(gemini_adapter.cdp,'evaluate',evaluate)
    assert asyncio.run(GeminiAdapter().is_fresh_chat(None)) is False


def test_chat_registry_has_no_sleep_after_last_inspection(monkeypatch):
    checks=[];sleeps=[];saved=[]
    async def evaluate(ws,js,timeout=5):checks.append(1);return {'id':'abc123','inSidebar':False}
    async def sleep(seconds):sleeps.append(seconds)
    monkeypatch.setattr(chatgpt,'cdp',SimpleNamespace(evaluate=evaluate))
    monkeypatch.setattr(chatgpt,'asyncio',SimpleNamespace(sleep=sleep))
    monkeypatch.setattr(chatgpt,'_ledger_add',saved.append)
    asyncio.run(chatgpt.ChatGPTAdapter()._record_if_persisted(None))
    assert len(checks)==3 and sleeps==[.8,.8] and saved==[]


def test_delayed_sidebar_entry_still_gets_registered(monkeypatch):
    states=iter([False,False,True]);saved=[]
    async def evaluate(ws,js,timeout=5):return {'id':'our-chat','inSidebar':next(states)}
    async def sleep(seconds):pass
    monkeypatch.setattr(chatgpt,'cdp',SimpleNamespace(evaluate=evaluate))
    monkeypatch.setattr(chatgpt,'asyncio',SimpleNamespace(sleep=sleep))
    monkeypatch.setattr(chatgpt,'_ledger_add',saved.append)
    asyncio.run(chatgpt.ChatGPTAdapter()._record_if_persisted(None))
    assert saved==['our-chat']


def test_gemini_send_confirmation_detects_new_user_query_and_polls_immediately(monkeypatch):
    from cluster.adapters import gemini_adapter as ga
    seen={}
    async def evaluate(ws,js,timeout=20,**kw):
        if js=="document.querySelectorAll('user-query').length":return 0
        if 'q.setText' in js:return 30
        if 'return { x:' in js:return {'x':10,'y':20}
        if 'replace(/\\s+/g' in js:return 30
        return True
    async def click(ws,x,y):pass
    async def wait(ws,js,timeout,interval):
        seen.update(js=js,timeout=timeout,interval=interval)
        return True
    monkeypatch.setattr(ga,'cdp',SimpleNamespace(evaluate=evaluate,click_at=click))
    adapter=ga.GeminiAdapter();adapter.wait_until=wait
    assert asyncio.run(adapter.send_prompt(None,'hello'))
    assert "querySelectorAll('user-query').length > 0" in seen['js']
    assert seen['timeout']==10 and seen['interval']==.1
