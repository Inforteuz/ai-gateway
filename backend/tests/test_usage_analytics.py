import json
from app.db import now
from .conftest import add_user, add_key, CSRF


def seed(env):
    uid=add_user(env,email='keys@example.com')
    other=add_user(env,email='other@example.com')
    _,a=add_key(env,uid,name='Production')
    _,b=add_key(env,uid,name='Agent')
    _,idle=add_key(env,uid,name='Unused')
    _,revoked=add_key(env,uid,name='Old key')
    _,foreign=add_key(env,other,name='Other user secret name')
    with env.db.tx() as c:
        c.execute("UPDATE api_keys SET status='revoked' WHERE id=?",(revoked,))
        for rid,k,u,pt,ct,cost,meta,estimated in [
            ('a1',a,uid,100,50,1000,{'project':'shop','purpose':'support','client':'opencode','called_tools':['read_file','edit_file','read_file']},0),
            ('b1',b,uid,200,70,2000,{'project':'api','purpose':'coding','client':'codex','called_tools':['read_file']},1),
            ('r1',revoked,uid,10,5,50,{},0),
            ('f1',foreign,other,9999,9999,9999,{'project':'private-project'},0)]:
            c.execute('''INSERT INTO usage_logs(request_id,user_id,key_id,model,endpoint,prompt_tokens,completion_tokens,cost,status_code,stream,estimated,created_at,attribution)
                VALUES (?,?,?,'sable-3.5','chat/completions',?,?,?,200,0,?,?,?)''',(rid,u,k,pt,ct,cost,estimated,now(),json.dumps(meta)))
    return uid,a,b,idle,revoked,foreign


def login(env,email='keys@example.com'):
    r=env.client.post('/portal/auth/login',headers=CSRF,json={'email':email,'password':'parol-12345'})
    assert r.status_code==200


def test_user_analytics_is_scoped_and_counts_each_key(env):
    uid,a,b,idle,revoked,foreign=seed(env);login(env)
    r=env.client.get('/portal/usage/analytics');assert r.status_code==200
    d=r.json();keys={k['id']:k for k in d['keys']}
    assert set(keys)=={a,b,idle,revoked} and keys[idle]['requests']==0
    assert keys[a]['prompt_tokens']==100 and keys[b]['completion_tokens']==70
    assert keys[revoked]['status']=='revoked'
    assert d['totals']['prompt_tokens']==310 and d['totals']['cost']==3050
    assert d['totals']['estimated_tokens']==270
    text=r.text
    assert 'Other user secret name' not in text and 'private-project' not in text
    for key in keys.values():
        assert 'key_hash' not in key and 'secret_enc' not in key and 'email' not in key
    assert sum(x['prompt_tokens'] for x in d['daily'])==310 and len(d['daily'])==30


def test_selected_key_filters_daily_models_and_attribution(env):
    uid,a,b,idle,revoked,foreign=seed(env);login(env)
    d=env.client.get(f'/portal/usage/analytics?key_id={a}&days=7').json()
    assert d['totals']['prompt_tokens']==100 and len(d['daily'])==7
    assert {k['id'] for k in d['keys']}=={a,b,idle,revoked}
    assert {m['label'] for m in d['dimensions']['project']}=={'shop'}
    assert sum(x['prompt_tokens'] for x in env.client.get(f'/portal/usage/daily?key_id={a}').json()['items'])==100
    assert sum(x['prompt_tokens'] for x in env.client.get(f'/portal/usage/models?key_id={a}').json()['items'])==100
    assert env.client.get(f'/portal/usage/analytics?key_id={foreign}').status_code==404
    assert env.client.get('/portal/usage/analytics?key_id=999999').status_code==404


def test_multiple_tool_groups_are_explicitly_nonadditive_and_deduplicated(env):
    _,a,*_=seed(env);login(env)
    d=env.client.get(f'/portal/usage/analytics?key_id={a}').json()
    tools={r['label']:r for r in d['dimensions']['tool']}
    assert not d['tool_tokens_additive']
    assert tools['read_file']['requests']==1 and tools['read_file']['prompt_tokens']==100
    assert tools['edit_file']['prompt_tokens']==100
    assert d['totals']['prompt_tokens']==100


def test_admin_can_filter_account_or_key_but_user_cannot_access_global_analytics(env):
    uid,a,b,idle,revoked,foreign=seed(env);login(env)
    assert env.client.get('/portal/admin/usage/analytics').status_code==403
    add_user(env,email='admin@example.com',role='admin');login(env,'admin@example.com')
    d=env.client.get(f'/portal/admin/usage/analytics?user_id={uid}').json()
    assert {k['id'] for k in d['keys']}=={a,b,idle,revoked}
    assert all(k['email']=='keys@example.com' for k in d['keys'])
    assert env.client.get(f'/portal/admin/usage/analytics?user_id={uid}&key_id={foreign}').status_code==404
    assert env.client.get(f'/portal/admin/usage?key_id={a}').json()['total']==1


def test_analytics_auth_and_empty_period(env):
    assert env.client.get('/portal/usage/analytics').status_code==401
    add_user(env,email='keys@example.com');login(env)
    d=env.client.get('/portal/usage/analytics?days=900').json()
    assert d['totals']['requests']==0 and d['keys']==[] and len(d['daily'])==90
