from dataclasses import replace
from types import SimpleNamespace as S
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from app import db, tuya_plugs as plugs, tuya_sharing_client as sharing


@pytest.fixture
def rig(monkeypatch, tmp_path):
    monkeypatch.setattr(db, 'DATABASE_URL', None)
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'test.db')
    db.init_db()
    monkeypatch.setattr(sharing, 'settings', replace(sharing.settings, api_token='private-key'))
    sharing._save('session', {'token_info': {'access_token': 'secret-token'}})
    clock = S(now=100000.0)
    monkeypatch.setattr(plugs.time, 'time', lambda: clock.now)
    devices = [S(id=r, name=r, category='cz', online=True,
                 local_key='secret-local-key', function={'switch_1': S(type='Boolean')}) for r in plugs.ROLES]
    powers = {r: False for r in plugs.ROLES}
    calls, writes = [], []
    offline = set()
    rejected = set()
    def get(path, params):
        calls.append(params)
        return {'success': True, 'result': [{'id': r, 'online': r not in offline,
                  'status': [{'code': 'switch_1', 'value': powers[r]}]} for r in params['devIds'].split(',')]}
    def post(path, _, body):
        role = path.split('/')[4]
        writes.append((role, body))
        if role in rejected:
            return {'success': False}
        powers[role] = body['commands'][0]['value']
        return {'success': True}
    client = S(customer_api=S(get=get, post=post), device_map={d.id:d for d in devices},
               update_device_cache=lambda: calls.append('scan'),
               device_repository=S(query_devices_by_ids=lambda ids:[d for d in devices if d.id in ids]))
    monkeypatch.setattr(sharing, '_manager', lambda _:client)
    return S(devices=devices, powers=powers, calls=calls, writes=writes, offline=offline,
             rejected=rejected, clock=clock)


def bind():
    return plugs.assign(0, {r:r for r in plugs.ROLES})


def test_unassigned_does_not_poll_or_write_cache(rig):
    assert all(not r['assigned'] for r in plugs.status()['roles'])
    assert not rig.calls and not rig.writes
    assert sharing._load('plug-cache') is None


def test_discovery_sanitized_bounded_cached_and_no_switching(rig):
    data = plugs.discover()
    assert len(data['devices']) == 3
    assert 'secret' not in str(data)
    assert plugs.discover()['cached'] and rig.calls == ['scan']
    assert not rig.writes


@pytest.mark.parametrize('category,codes', [('kt', ['switch_1']), ('wsdcg', ['switch_1']),
                         ('cz', ['switch_1', 'switch_2']), ('cz', ['switch_usb'])])
def test_reject_other_devices_and_multirelay(rig, category, codes):
    d=rig.devices[0];d.category=category;d.function={c:S(type='Boolean') for c in codes}
    assert plugs.candidate(d) is None
    with pytest.raises(HTTPException): bind()
    assert plugs.config()['revision'] == 0


def test_duplicate_or_stale_assignment_rejected(rig):
    with pytest.raises(HTTPException): plugs.assign(0, {'roof':'roof','walls':'roof','utility':''})
    bind()
    with pytest.raises(HTTPException): plugs.assign(0, {})
    assert not rig.writes
    assert db.list_activity()==[]


def test_lights_and_utility_isolation(rig):
    bind()
    assert plugs.command('lights', True, 1)['ok']
    assert rig.powers == {'roof':True,'walls':True,'utility':False}
    assert plugs.command('utility', True, 1)['ok']
    assert [r for r,_ in rig.writes] == ['roof','walls','utility']
    assert plugs.command('utility', False, 1)['ok']
    assert rig.powers == {'roof':True,'walls':True,'utility':False}


def test_preflight_checks_both_lights_before_sending(rig):
    bind();rig.offline.add('walls')
    with pytest.raises(HTTPException): plugs.command('lights', True, 1)
    assert not rig.writes


def test_partial_failure_never_claims_success_or_rolls_back(rig):
    bind();rig.rejected.add('walls')
    result=plugs.command('lights', True, 1)
    assert not result['ok']
    assert rig.powers == {'roof':True,'walls':False,'utility':False}
    assert len(rig.writes)==2


def test_status_shared_cache_single_bulk_read_and_expiry(rig):
    bind()
    with ThreadPoolExecutor(max_workers=5) as pool: list(pool.map(lambda _:plugs.status(),range(5)))
    assert len(rig.calls)==1
    assert rig.calls[0]['devIds']=='roof,walls,utility'
    rig.clock.now+=60
    plugs.status()
    assert len(rig.calls)==2


def test_api_requires_auth_and_strict_bool_and_same_origin(rig):
    def auth(): raise HTTPException(401)
    app=FastAPI();app.include_router(plugs.router(auth));c=TestClient(app)
    assert c.get('/api/plugs').status_code==401
    app.dependency_overrides[auth]=lambda:None
    assert c.post('/api/plugs/command',json={'revision':0,'target':'utility','power':'true'}).status_code==422
    assert c.post('/api/plugs/discover',headers={'Origin':'https://evil.test'}).status_code==403
    assert not rig.writes
