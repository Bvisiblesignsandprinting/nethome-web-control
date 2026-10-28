import concurrent.futures
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from app import db
import app.tuya_client as module
from app.tuya_client import TuyaCloudClient, TuyaCloudError

@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(db, 'DATABASE_URL', None)
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'sensor.db')
    db.init_db()
    monkeypatch.setattr(module, 'settings', replace(module.settings, tuya_client_id='test-id', tuya_client_secret='test-secret', tuya_device_id='test-device'))
    clock = SimpleNamespace(value=100000.0)
    monkeypatch.setattr(module.time, 'time', lambda: clock.value)
    calls = []
    def request(self, path):
        calls.append(path)
        if 'shadow/properties' in path:
            return {'result': {'properties': [{'code':'temp_current','value':220,'time':100000000}, {'code':'humidity_value','value':45,'time':100000000}]}}
        return {'result': {'online': True}}
    monkeypatch.setattr(TuyaCloudClient, '_get', request)
    return clock, calls

def test_cold_instances_share_cache_and_short_cache_cannot_bypass(setup):
    clock,calls=setup
    assert TuyaCloudClient().indoor_sensor()['temperature_f']==71.6
    for _ in range(6):
        result=TuyaCloudClient().indoor_sensor(max_cache_age_seconds=20)
        assert result['cached'] and result['cache_ttl_seconds']==30
    assert len(calls)==2
    clock.value+=30
    assert not TuyaCloudClient().indoor_sensor()['cached']
    assert len(calls)==3  # only properties; online detail remains shared
    clock.value+=3601
    TuyaCloudClient().indoor_sensor()
    assert len(calls)==5

def test_concurrent_instances_make_one_provider_refresh(setup):
    _,calls=setup
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results=list(pool.map(lambda _:TuyaCloudClient().indoor_sensor(),range(8)))
    assert len(calls)==2
    assert all(row['temperature_f']==71.6 for row in results)

def test_quota_failure_is_shared_across_new_instances(setup,monkeypatch):
    clock,calls=setup
    def exhausted(self,path):
        calls.append(path)
        raise TuyaCloudError('Tuya API error 28841004: quota exhausted',code='28841004')
    monkeypatch.setattr(TuyaCloudClient,'_get',exhausted)
    for _ in range(5):
        with pytest.raises(TuyaCloudError) as caught:TuyaCloudClient().indoor_sensor()
        assert caught.value.code=='28841004' and caught.value.retry_after>=21600
    assert len(calls)==1
    clock.value+=21601
    with pytest.raises(TuyaCloudError):TuyaCloudClient().indoor_sensor()
    assert len(calls)==2

def test_expired_reading_is_not_returned_on_failed_refresh(setup,monkeypatch):
    clock,_=setup
    TuyaCloudClient().indoor_sensor()
    clock.value+=301
    def failed(self,path):raise TuyaCloudError('network failed')
    monkeypatch.setattr(TuyaCloudClient,'_get',failed)
    with pytest.raises(TuyaCloudError,match='network failed'):TuyaCloudClient().indoor_sensor()
    with pytest.raises(TuyaCloudError,match='network failed'):TuyaCloudClient().indoor_sensor()

def test_database_failure_does_not_bypass_limiter(setup,monkeypatch):
    _,calls=setup
    monkeypatch.setattr(module,'load_sensor_cache',lambda:(_ for _ in ()).throw(RuntimeError('DB unavailable')))
    with pytest.raises(TuyaCloudError,match='protect quota'):TuyaCloudClient().indoor_sensor()
    assert calls==[]

def test_crashed_worker_reservation_prevents_repeat_calls(setup):
    clock,calls=setup
    state={'identity':[module.settings.tuya_base_url,'test-id','test-device'],'next_fetch_at':clock.value+300,'error':'Sensor refresh is pending'}
    db.save_sensor_cache(state)
    with pytest.raises(TuyaCloudError,match='pending'):TuyaCloudClient().indoor_sensor()
    assert calls==[]

def test_cache_identity_prevents_using_another_sensor_reading(setup,monkeypatch):
    _,calls=setup
    TuyaCloudClient().indoor_sensor()
    monkeypatch.setattr(module,'settings',replace(module.settings,tuya_device_id='different-device'))
    assert not TuyaCloudClient().indoor_sensor()['cached']
    assert len(calls)==4

def test_cache_is_single_row_and_not_activity_feed(setup):
    clock,_=setup
    TuyaCloudClient().indoor_sensor()
    clock.value+=301
    TuyaCloudClient().indoor_sensor()
    with db._sqlite_conn() as conn:
        assert conn.execute("select count(*) from activity where source='tuya-cache'").fetchone()[0]==1
    assert db.list_activity()==[]
    assert 'test-secret' not in str(db.load_sensor_cache())


def test_successful_legacy_five_minute_deadline_does_not_delay_refresh(setup):
    clock, calls = setup
    TuyaCloudClient().indoor_sensor()
    state = db.load_sensor_cache()
    state['next_fetch_at'] = clock.value + 300
    db.save_sensor_cache(state)
    clock.value += 31
    result = TuyaCloudClient().indoor_sensor()
    assert not result['cached'] and result['cache_ttl_seconds'] == 30
    assert len(calls) == 3


def test_transient_network_failure_can_retry_after_thirty_seconds(setup, monkeypatch):
    clock, calls = setup
    original = TuyaCloudClient._get
    def failed(self, path):
        calls.append(path)
        raise TuyaCloudError('network failed')
    monkeypatch.setattr(TuyaCloudClient, '_get', failed)
    with pytest.raises(TuyaCloudError) as caught:
        TuyaCloudClient().indoor_sensor()
    assert caught.value.retry_after == 30
    monkeypatch.setattr(TuyaCloudClient, '_get', original)
    clock.value += 30
    assert TuyaCloudClient().indoor_sensor()['ok']
