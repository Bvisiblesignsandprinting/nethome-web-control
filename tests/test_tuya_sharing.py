import time
from dataclasses import replace
from types import SimpleNamespace as S
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from app import db, main, tuya_sharing_client as sharing
from app import tuya_client as legacy


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(db, 'DATABASE_URL', None)
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'test.db')
    db.init_db()
    monkeypatch.setattr(sharing, 'settings', replace(sharing.settings, api_token='test-private-server-key', tuya_device_id='sensor'))
    session = {'code':'private-code', 'device_id':'sensor', 'sensor_name':'Room sensor',
               'terminal_id':'terminal', 'endpoint':'https://apigw.tuyaus.com',
               'token_info':{'access_token':'private-token', 'refresh_token':'private-refresh'},
               'temperature_definition':{'scale':1,'unit':'℃'},
               'humidity_definition':{'scale':0,'unit':'%'}}
    sharing._save('session', session)
    clock = S(now=100000.0)
    monkeypatch.setattr(sharing.time, 'time', lambda: clock.now)
    monkeypatch.setattr(sharing, 'load_smart_control_config', lambda:{'enabled':True})
    calls=[]
    def get(*args, **kwargs):
        calls.append(1)
        return {'success':True,'result':[{'id':'sensor','online':True,'status':[
            {'code':'va_temperature','value':188},{'code':'va_humidity','value':49}]}]}
    monkeypatch.setattr(sharing, '_manager', lambda *args, **kwargs:S(customer_api=S(get=get)))
    return session, clock, calls


def test_encrypted_credentials_are_not_in_activity(setup):
    session,_,_=setup
    stored=db.load_sharing_record('session')
    assert all(secret not in stored for secret in ('private-token','private-code','private-refresh'))
    assert sharing._load('session')==session
    assert db.list_activity()==[]


def test_30_second_cache_across_instances_and_concurrency(setup):
    _,clock,calls=setup
    with ThreadPoolExecutor(max_workers=5) as pool:
        rows=list(pool.map(lambda _:sharing.indoor_sensor(1),range(5)))
    assert len(calls)==1
    assert all(row['temperature_f']==65.8 and row['humidity']==49 for row in rows)
    clock.now+=29
    assert sharing.indoor_sensor(1)['cached']
    clock.now+=1
    assert not sharing.indoor_sensor(1)['cached']
    assert len(calls)==2
    assert rows[0]['updated_at'] is None
    assert rows[0]['measurement_time_verified'] is False


def test_off_mode_does_not_call_tuya(setup, monkeypatch):
    _,_,calls=setup
    monkeypatch.setattr(sharing,'load_smart_control_config',lambda:{'enabled':False})
    with pytest.raises(sharing.SharingError,match='paused'):sharing.indoor_sensor()
    assert not calls


def test_expired_data_not_reused_after_error_and_backoff(setup,monkeypatch):
    _,clock,calls=setup
    sharing.indoor_sensor()
    clock.now+=41
    def failed(*args,**kwargs):
        calls.append(1)
        raise RuntimeError('private-token')
    monkeypatch.setattr(sharing,'_manager',failed)
    with pytest.raises(sharing.SharingError,match='no stale'):sharing.indoor_sensor()
    with pytest.raises(sharing.SharingError,match='temporarily'):sharing.indoor_sensor()
    assert len(calls)==2
    assert 'sensor' not in db.load_sensor_cache()


def test_sharing_failure_never_falls_back_to_trial(setup,monkeypatch):
    def failed(*a,**kw):raise sharing.SharingError('private-token')
    monkeypatch.setattr(sharing,'indoor_sensor',failed)
    monkeypatch.setattr(legacy.TuyaCloudClient,'_get',lambda *a,**kw:pytest.fail('Developer API must not be called'))
    with pytest.raises(legacy.TuyaCloudError) as caught:legacy.TuyaCloudClient().indoor_sensor()
    assert 'private-token' not in str(caught.value)


def test_endpoint_and_units_fail_closed(setup,monkeypatch):
    session,_,_=setup
    with pytest.raises(sharing.SharingError):
        sharing.normalize({'va_temperature':188,'va_humidity':49},True,{**session,'temperature_definition':{'scale':1,'unit':'F'}})
    with pytest.raises(sharing.SharingError):
        sharing.normalize({'va_temperature':True,'va_humidity':49},True,session)


def test_authentication_and_cross_site_requests(setup,monkeypatch):
    monkeypatch.setattr(main,'settings',replace(main.settings,api_token='api-key'))
    # Existing tests have already exercised this singleton MCP lifespan.
    client=TestClient(main.app)
    assert client.get('/api/sensor-sharing').status_code==401
    assert client.get('/sensor-setup').status_code==401
    headers={'Authorization':'Bearer api-key'}
    result=client.get('/api/sensor-sharing',headers=headers)
    assert result.status_code==200 and result.json()['connected']
    assert 'private-' not in result.text
    assert client.post('/api/sensor-sharing/begin',json={'code':'code'},headers={**headers,'Origin':'https://evil.example'}).status_code==403


def test_setup_rejects_wrong_or_ambiguous_device(setup,monkeypatch):
    session,clock,_=setup
    sharing._save('pending',{'code':'code','token':'qr','created_at':clock.now,'last_check':0})
    monkeypatch.setattr(sharing,'_login',lambda:S(login_result=lambda *a:(True,{'terminal_id':'t','endpoint':session['endpoint']})))
    wrong=S(id='other',category='wsdcg',status={'va_temperature':188,'va_humidity':49})
    monkeypatch.setattr(sharing,'_manager',lambda *a,**kw:S(update_device_cache=lambda:None,device_map={'other':wrong}))
    with pytest.raises(sharing.SharingError,match='not uniquely'):sharing.confirm()
    assert sharing._load('session')==session


def test_setup_saves_connection_and_is_idempotent(setup,monkeypatch):
    session,clock,_=setup
    sharing._save('pending',{'code':'code','token':'qr','created_at':clock.now,'last_check':0})
    info={'terminal_id':'t','endpoint':session['endpoint'],'access_token':'new-secret'}
    monkeypatch.setattr(sharing,'_login',lambda:S(login_result=lambda *a:(True,info)))
    sensor=S(id='sensor',name='Room sensor',online=True,category='wsdcg',
             status={'va_temperature':188,'va_humidity':49},
             status_range={'va_temperature':S(values='{"scale":1,"unit":"℃"}'),
                           'va_humidity':S(values='{"scale":0,"unit":"%"}')})
    monkeypatch.setattr(sharing,'_manager',lambda *a,**kw:S(update_device_cache=lambda:None,device_map={'sensor':sensor}))
    result=sharing.confirm()
    assert result['connected'] and result['sensor']['temperature_f']==65.8
    assert 'new-secret' not in str(result)
    assert sharing.confirm()['connected']
    assert sharing._load('session')['token_info']['access_token']=='new-secret'


def test_migrates_old_40_second_cache_without_resetting_error_backoff(setup,monkeypatch):
    _,clock,calls=setup
    row=sharing.indoor_sensor()
    cached=db.load_sensor_cache()
    cached['next_fetch_at']=clock.now+40
    db.save_sensor_cache(cached)
    clock.now+=30
    sharing.indoor_sensor()
    assert len(calls)==2
    # A failed refresh must wait 60 seconds, even after a new process starts.
    clock.now+=30
    monkeypatch.setattr(sharing,'_manager',lambda *a,**kw:(_ for _ in ()).throw(RuntimeError('failed')))
    with pytest.raises(sharing.SharingError):sharing.indoor_sensor()
    clock.now+=59
    with pytest.raises(sharing.SharingError,match='temporarily'):sharing.indoor_sensor()
    assert db.load_sensor_cache()['next_fetch_at']==clock.now+1


def test_default_client_dispatch_does_not_keep_40_second_delay(setup,monkeypatch):
    intervals=[]
    monkeypatch.setattr(sharing,'indoor_sensor',lambda interval:intervals.append(interval) or {'ok':True})
    legacy.TuyaCloudClient().indoor_sensor()
    assert intervals==[30]
