from __future__ import annotations
import asyncio
import json
import time
from types import SimpleNamespace
import httpx
import pytest
from app import local_guard
from app import attribution
from cluster import balancer as B, settings as cfg
from cluster.circuit import ModelCircuit, CircuitState
from cluster.pool import WorkerTab, _ACCOUNT_BLOCKED, _ACCOUNT_DISPATCH


def test_candidate_inspection_does_not_claim_half_open_probe():
    c = ModelCircuit('test', state=CircuitState.OPEN, opened_at=time.monotonic()-100)
    assert all(c.is_available() for _ in range(20))
    assert c.probes_inflight == 0
    assert c.allow_request()
    assert not c.allow_request()
    c.release_probe()
    assert c.allow_request()


def test_account_quarantine_applies_to_sibling_tabs(monkeypatch):
    # The one-request-per-account rule; parallel tabs are covered in test_parallel_tabs.py
    monkeypatch.setattr(cfg, "PARALLEL_TAB_MODELS", frozenset())
    async def run():
        adapter = SimpleNamespace()
        a=WorkerTab(19701,'a','qwen','ws://unused',adapter)
        b=WorkerTab(19701,'b','qwen','ws://unused',adapter)
        c=WorkerTab(19702,'c','qwen','ws://unused',adapter)
        try:
            a.set_cooldown(1200, reason='captcha:slider')
            assert not b.is_available() and c.is_available()
            a.apply_probe(True,'ok')
            assert b.is_available()
            await a.dispatch_lock.acquire()
            assert not b.is_available() and c.is_available()
            a.dispatch_lock.release()
        finally:
            _ACCOUNT_BLOCKED.pop(a.account_key,None)
            _ACCOUNT_DISPATCH.pop(a.account_key,None)
            _ACCOUNT_DISPATCH.pop(c.account_key,None)
    asyncio.run(run())


def test_queue_uses_first_available_account_and_releases_on_cancel(monkeypatch):
    async def run():
        class W:
            model_name='qwen';health_score=1;cooldown_until=0;short_tab='x';last_used=0;adapter=SimpleNamespace(clean_text=lambda t:t)
            def __init__(self,port,count):
                self.browser_port=port;self.total_requests=count;self.lock=asyncio.Lock()
            def is_available(self):return not self.lock.locked()
            async def execute_prompt(self,*a,**kw):return str(self.browser_port),[]
        a,b=W(1,0),W(2,1)
        for w in (a,b):await w.lock.acquire()
        pool=SimpleNamespace(workers=[a,b],workers_for_model=lambda m:[a,b])
        bal=B.ClusterBalancer(pool)
        task=asyncio.create_task(bal._dispatch('p','qwen',1,time.monotonic(),False,healed=True))
        await asyncio.sleep(.01)
        b.lock.release()
        answer=await asyncio.wait_for(task,.2)
        assert answer[0]=='2' and not b.lock.locked()
        a.lock.release()
        # A cancelled waiter returns its single queue slot and holds no locks.
        for w in (a,b):await w.lock.acquire()
        task=asyncio.create_task(bal._dispatch('p','qwen',1,time.monotonic(),False,healed=True))
        await asyncio.sleep(.01);task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert not any(bal._waiting.values())
        a.lock.release();b.lock.release()
    monkeypatch.setattr(cfg,'PREWARM_ENABLED',False)
    asyncio.run(run())


class Stream(httpx.AsyncByteStream):
    def __init__(self,events,delay=0):self.events=events;self.delay=delay;self.closed=False
    async def __aiter__(self):
        for e in self.events:
            if self.delay:await asyncio.sleep(self.delay)
            yield ('data: '+(e if isinstance(e,str) else json.dumps(e))+'\n\n').encode()
    async def aclose(self):self.closed=True


def test_json_guard_stops_repeating_reasoning_and_closes_upstream():
    async def run():
        stream=Stream([{'choices':[{'delta':{'reasoning_content':'I need to check this again. '*20}}]}]*30)
        resp=httpx.Response(200,stream=stream)
        with pytest.raises(local_guard.GuardStopped,match='repetition_loop'):
            await local_guard.collect(resp,1,1)
        assert stream.closed
    asyncio.run(run())


def test_guard_collects_fragmented_tools_and_usage():
    async def run():
        events=[{'id':'x','model':'private','choices':[{'index':0,'delta':{'tool_calls':[{'index':0,'id':'call_x','function':{'name':'read_','arguments':'{"'}}]}}]},
                {'choices':[{'index':0,'delta':{'tool_calls':[{'index':0,'function':{'name':'file','arguments':'path":"a"}'}}]},'finish_reason':'tool_calls'}]},
                {'choices':[],'usage':{'prompt_tokens':10,'completion_tokens':3}},'[DONE]']
        data=await local_guard.collect(httpx.Response(200,stream=Stream(events)),1,1)
        tool=data['choices'][0]['message']['tool_calls'][0]
        assert tool['function']=={'name':'read_file','arguments':'{"path":"a"}'}
        assert data['usage']['completion_tokens']==3
    asyncio.run(run())


def test_guard_total_deadline_also_bounds_silent_socket():
    async def run():
        stream=Stream(['[DONE]'],delay=.2)
        with pytest.raises(local_guard.GuardStopped,match='generation_budget_exceeded'):
            await local_guard.collect(httpx.Response(200,stream=stream),.01,1)
        assert stream.closed
    asyncio.run(run())


def test_request_attribution_excludes_content_and_arguments():
    meta=attribution.request_meta({'user-agent':'opencode/1','x-sf-project':'shop','x-sf-purpose':'support'},
                                  {'messages':[{'content':'SECRET'}], 'tools':[{'function':{'name':'read_file','description':'SECRET'}}]})
    attribution.observe(meta,{'choices':[{'message':{'tool_calls':[{'function':{'name':'read_file','arguments':'SECRET'}}]}}]})
    assert meta['client']=='opencode' and meta['called_tools']==['read_file']
    assert 'SECRET' not in json.dumps(meta)


def test_tool_loop_guard_is_safe_attribution():
    meta = {}
    attribution.observe(meta, {"gateway": {"tool_protocol": {
        "loop_guard": "repeated_tool_action", "loop_guard_count": 2,
        "initial_calls": True}}})
    assert meta["tool_protocol"]["loop_guard"] == "repeated_tool_action"
    assert meta["tool_protocol"]["loop_guard_count"] == 2


def test_attachment_codes_localize_parameters_and_unknowns_survive():
    from cluster.attachments import AttachmentStore, AttachmentError
    from cluster import settings
    from app.gateway import upstream_error
    from app.upstream_errors import localize
    with pytest.raises(AttachmentError) as caught:
        with AttachmentStore() as store:store.add_bytes(b'x'*(settings.MAX_ATTACHMENT_BYTES+1),'text/plain')
    err=caught.value
    assert err.code=='attachment_too_large'
    uz,en=localize(err.code,err.details)
    assert str(err.details['limit_kb']) in uz and 'limit' in en
    data=json.dumps({'error':{'code':err.code,'details':err.details,'message':str(err)}}).encode()
    out=upstream_error(httpx.Response(400),data)
    assert out[1]==err.code and out[2]==uz and out[4]==en
    assert 'new error' in upstream_error(httpx.Response(400),b'{"error":{"code":"new_code","message":"new error"}}')[2]


def test_regenerate_never_clicks_a_verification_challenge(monkeypatch):
    from cluster.adapters.base_adapter import BaseAIAdapter
    from cluster import cdp
    class Adapter(BaseAIAdapter):
        name='test'
        async def reset_chat(self,ws):pass
        async def send_prompt(self,ws,prompt):return True
        async def read_response(self,ws,timeout=45):return ''
    adapter=Adapter("test")
    calls=[]
    async def challenge(ws):return 'slider'
    async def evaluate(*args,**kwargs):calls.append(args);return True
    monkeypatch.setattr(adapter,'detect_challenge',challenge)
    monkeypatch.setattr(cdp,'evaluate',evaluate)
    assert asyncio.run(adapter.regenerate_response(None)) is False
    assert not calls


def test_failed_probe_does_not_reload_when_recovery_disabled(monkeypatch):
    from cluster.adapters.base_adapter import BaseAIAdapter
    from cluster import cdp
    class Adapter(BaseAIAdapter):
        name='test'
        async def reset_chat(self,ws):pass
        async def send_prompt(self,ws,prompt):return True
        async def read_response(self,ws,timeout=45):return ''
    adapter=Adapter("test")
    async def challenge(ws):return 'slider'
    async def evaluate(*args,**kwargs):return {'hasInput':True}
    async def reload(ws):raise AssertionError('read-only probe reloaded')
    monkeypatch.setattr(adapter,'detect_challenge',challenge)
    monkeypatch.setattr(adapter,'reload_after_challenge',reload)
    monkeypatch.setattr(cdp,'evaluate',evaluate)
    assert asyncio.run(adapter.check_session(None,recover=False))==(False,'captcha')


def test_tool_name_fragments_are_merged_without_retaining_arguments():
    meta={}
    for name in ('read_','file'):
        attribution.observe(meta,{'choices':[{'index':0,'delta':{'tool_calls':[{'index':0,'function':{'name':name,'arguments':'DO_NOT_KEEP'}}]}}]})
    safe=attribution.persisted(meta)
    assert safe['called_tools']==['read_file']
    assert 'DO_NOT_KEEP' not in json.dumps(safe) and not any(k.startswith('_')for k in safe)


def test_recovery_is_single_flight_per_port(monkeypatch):
    from cluster import self_heal
    import concurrent.futures
    import threading
    entered,release=threading.Event(),threading.Event()
    fn='fast_recover' if hasattr(self_heal,'fast_recover') else 'tezkor_tiklash'
    def recover(port):entered.set();release.wait(1);return True
    monkeypatch.setattr(self_heal,'_'+fn+'_unlocked',recover)
    with concurrent.futures.ThreadPoolExecutor(2) as executor:
        first=executor.submit(getattr(self_heal,fn),19099)
        assert entered.wait(1)
        assert executor.submit(getattr(self_heal,fn),19099).result() is False
        release.set();assert first.result() is True


def test_usage_attribution_is_atomic_private_and_filterable(env):
    from .conftest import add_key,add_user,bearer,CHAT
    uid=add_user(env,balance_som=1000);other=add_user(env,email="other@example.com",balance_som=1000)
    key,_=add_key(env,uid)
    response=env.client.post('/v1/chat/completions',headers=bearer(key)|{'User-Agent':'opencode/1','X-SF-Project':'shop','X-SF-Purpose':'support'},json=CHAT)
    assert response.status_code==200
    record=env.db.one('SELECT attribution FROM usage_logs WHERE user_id=?',(uid,))
    data=json.loads(record['attribution'])
    assert data['client']=='opencode' and data['project']=='shop' and data['provider']=='local'
    from app.routes_account import usage
    assert usage(user={'id':uid},db=env.db,q='shop')['total']==1
    assert usage(user={'id':other},db=env.db,q='shop')['total']==0
    from app.routes_admin import all_usage
    report=all_usage(db=env.db,project='shop',client='opencode')
    assert report['total']==1 and report['items'][0]['user_id']==uid
    assert report['totals']['prompt_tokens']>0
    env.client.cookies.clear()
    assert env.client.get('/portal/admin/usage').status_code==401


def test_discovery_refresh_does_not_block_a_warm_pool(monkeypatch):
    from cluster.pool import ClusterPool
    import threading
    entered,release=threading.Event(),threading.Event()
    def scan():entered.set();release.wait(1)
    async def run():
        pool=ClusterPool([19701]);pool.workers=[object()]
        monkeypatch.setattr(pool,'_scan_sync',scan)
        await asyncio.wait_for(pool.scan(),.05)
        assert pool._scan_task is not None
        assert await asyncio.to_thread(entered.wait,.5)
        # A second request reuses the same refresh task.
        task=pool._scan_task
        await asyncio.wait_for(pool.scan(),.05)
        assert pool._scan_task is task
        release.set();await task
    asyncio.run(run())


def test_provider_capacity_is_global_and_reservation_release_reclaims_it(env):
    from app import admission,billing
    from .conftest import add_user,add_key,reserve
    from concurrent.futures import ThreadPoolExecutor
    uid=add_user(env,balance_som=1000);_,kid=add_key(env,uid)
    ids=['slot_'+str(i)for i in range(10)]
    for rid in ids:
        reserve(env,uid,kid,0,request_id=rid)
        admission.register(env.db,rid,'primary','local')
    # Each thread uses a separate DB connection, as separate API workers do.
    with ThreadPoolExecutor(10)as executor:
        claims=list(executor.map(lambda rid:admission.try_acquire(env.db,rid,'primary','local',2),ids))
    assert sum(claims)==2
    assert admission.snapshot(env.db)['local']=={'active':2,'queued':8}
    first=ids[claims.index(True)]
    billing.release(env.db,request_id=first,user_id=uid)
    waiting=ids[claims.index(False)]
    assert admission.try_acquire(env.db,waiting,'primary','local',2)
    assert env.db.scalar('SELECT COUNT(*) FROM inference_slots WHERE request_id=?',(first,))==0
    # A verifier competes for external capacity with primary external requests.
    admission.register(env.db,waiting,'verification','external')
    assert admission.try_acquire(env.db,waiting,'verification','external',1)
    another=next(rid for rid in ids if rid not in (first,waiting))
    admission.release(env.db,another,'primary')
    admission.register(env.db,another,'primary','external')
    assert not admission.try_acquire(env.db,another,'primary','external',1)
