"""Exercise persisted lighting drafts, isolation, concurrent edits and local-clock timing."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import db, lighting, lighting_store
from app.main import access_auth, app as full_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_URL", None)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "lighting.db")
    db.init_db()
    app = FastAPI()
    app.include_router(lighting.lighting_router(access_auth))
    app.dependency_overrides[access_auth] = lambda: None
    with TestClient(app) as client:
        yield client


def schedule(**changes):
    return {"name": "Evening", "zone_id": "rgb-walls", "command": {"power": True, "color": "#ff8800", "brightness": 60},
            "repeat": "weekly", "days": [0, 4], "time_local": "18:00", "timezone": "America/New_York", "enabled": True, **changes}


def test_routes_require_login(client):
    client.app.dependency_overrides.clear()
    for method, path, body in [
        ("GET", "", None), ("PATCH", "/zones/rgb-walls", {"revision": 0, "command": {"power": True}}),
        ("POST", "/schedules", {"revision": 0, "schedule": schedule()}),
        ("POST", "/schedules/preview", schedule()),
        ("PUT", "/schedules/missing", {"revision": 0, "schedule": schedule()}),
        ("DELETE", "/schedules/missing?revision=0", None),
    ]:
        assert client.request(method, "/api/lighting" + path, json=body).status_code == 401


def test_defaults_are_unconnected_and_no_rows_created_by_reading(client):
    r = client.get("/api/lighting")
    assert r.status_code == 200 and r.headers['cache-control'] == 'no-store'
    value = r.json()
    assert value['revision'] == 0 and not value['hardware_ready'] and value['delivery'] == 'preview_only'
    assert [z['id'] for z in value['zones']] == list(lighting.ZONES)
    assert all(z['reported'] is None and z['connection'] == 'not_connected' for z in value['zones'])
    assert db.list_activity() == []


def test_zone_settings_persist_without_touching_ac_queue(client):
    payload = {"revision": 0, "command": {"power": True, "color": "#12ab34", "brightness": 0}}
    r = client.patch('/api/lighting/zones/rgb-walls', json=payload)
    assert r.status_code == 200, r.text
    desired = r.json()['zones'][0]['desired']
    assert {k: desired[k] for k in ('power','color','brightness')} == {"power": True, "color": "#12AB34", "brightness": 0}
    assert desired['effect']['mode'] == 'solid' and desired['effect']['red_balance'] == 140
    assert client.get('/api/lighting').json() == r.json()
    assert r.json()['zones'][0]['reported'] is None
    assert db.list_schedules() == []
    with db._sqlite_conn() as conn:
        assert conn.execute('select count(*) from schedule_executions').fetchone()[0] == 0
    assert [x['source'] for x in db.list_activity()] == ['lighting']
    assert db.list_activity()[0]['result'] == 'saved-preview'


@pytest.mark.parametrize('command', [{}, {'power': 'true'}, {'brightness': -1}, {'brightness': 101},
    {'brightness': 2.5}, {'brightness': True}, {'color': 'red'}, {'color': '#123'},
    {'color': '<svg/onload=alert(1)>'}, {'connection': 'connected'}, {'power': None}])
def test_invalid_zone_commands_never_write(client, command):
    assert client.patch('/api/lighting/zones/rgb-walls', json={'revision': 0, 'command': command}).status_code == 422
    assert client.get('/api/lighting').json()['revision'] == 0


def test_regular_lights_support_only_power_and_unknown_zone_rejected(client):
    assert client.patch('/api/lighting/zones/chandeliers', json={'revision': 0, 'command': {'brightness': 50}}).status_code == 422
    assert client.patch('/api/lighting/zones/regular-walls-roof', json={'revision': 0, 'command': {'color': 'white'}}).status_code == 422
    assert client.patch('/api/lighting/zones/roof', json={'revision': 0, 'command': {'power': True}}).status_code == 404
    r = client.patch('/api/lighting/zones/chandeliers', json={'revision': 0, 'command': {'power': True}})
    assert r.status_code == 200 and r.json()['zones'][3]['desired'] == {'power': True}


def test_custom_fade_and_color_balance_persist(client):
    effect = {"mode":"fade", "colors":["#ff0000","#00ff00","#0000ff"], "cycle_seconds":30,
              "transition":100, "red_balance":155, "green_balance":100, "blue_balance":95}
    r = client.patch('/api/lighting/zones/rgb-waterfall', json={'revision':0,'command':{'power':True,'brightness':65,'effect':effect}})
    assert r.status_code == 200, r.text
    saved = r.json()['zones'][1]['desired']['effect']
    assert saved['colors'] == ['#FF0000','#00FF00','#0000FF']
    assert saved['red_balance'] == 155 and saved['cycle_seconds'] == 30
    assert client.get('/api/lighting').json()['zones'][1]['desired']['effect'] == saved


@pytest.mark.parametrize('effect', [
    {'mode':'fade','colors':['#FF0000']},
    {'mode':'blink','colors':['#FF0000','#0000FF'],'transition':50},
    {'mode':'fade','colors':['red','#0000FF']},
    {'mode':'fade','colors':['#FF0000','#0000FF'],'red_balance':201},
    {'mode':'strobe','colors':['#FF0000','#0000FF']},
])
def test_invalid_effects_never_write(client, effect):
    r = client.patch('/api/lighting/zones/rgb-walls', json={'revision':0,'command':{'effect':effect}})
    assert r.status_code == 422
    assert client.get('/api/lighting').json()['revision'] == 0


def test_stale_revision_does_not_overwrite(client):
    first = client.patch('/api/lighting/zones/rgb-walls', json={'revision': 0, 'command': {'color': 'white'}})
    assert first.status_code == 200
    r = client.patch('/api/lighting/zones/rgb-walls', json={'revision': 0, 'command': {'color': '#000000'}})
    assert r.status_code == 409
    assert client.get('/api/lighting').json()['zones'][0]['desired']['color'] == 'white'


def test_concurrent_writers_have_one_winner(client):
    def write(color):
        def update(value): value['zones']['rgb-walls']['color'] = color
        try:
            return lighting_store.document(update, revision=0)['revision']
        except HTTPException as error:
            return error.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(write, ['#000000', '#FFFFFF'])) == [1, 409]


def test_schedule_create_edit_disable_delete_and_preview(client):
    before = client.get('/api/lighting').json()
    preview = client.post('/api/lighting/schedules/preview', json=schedule())
    assert preview.status_code == 200 and len(preview.json()['occurrences']) == 3
    assert client.get('/api/lighting').json() == before
    r = client.post('/api/lighting/schedules', json={'revision': 0, 'schedule': schedule()})
    assert r.status_code == 200, r.text
    saved = r.json()['schedules'][0]
    assert saved['status'] == 'waiting_for_connection' and len(saved['next_occurrences']) == 3
    sid = saved['id']
    assert client.get('/api/lighting').json()['schedules'][0]['id'] == sid
    edited = schedule(name='Later', time_local='20:15', enabled=False)
    r = client.put('/api/lighting/schedules/' + sid, json={'revision': 1, 'schedule': edited})
    assert r.status_code == 200 and r.json()['schedules'][0]['name'] == 'Later'
    assert r.json()['schedules'][0]['next_occurrences'] == []
    assert client.delete('/api/lighting/schedules/' + sid + '?revision=1').status_code == 409
    assert client.delete('/api/lighting/schedules/' + sid + '?revision=2').status_code == 200
    assert client.get('/api/lighting').json()['schedules'] == []


@pytest.mark.parametrize('changes', [{'time_local':'25:00'}, {'time_local':'8:00'}, {'days':[]},
    {'days':[0,0]}, {'days':[7]}, {'days':['1']}, {'timezone':'not/a/zone'}, {'name':'   '},
    {'repeat':'monthly'}, {'repeat':'one_time','days':[]}, {'command':{'color':'white'}},
    {'zone_id':'chandeliers'}, {'enabled':'true'}, {'repeat':'daily', 'days':[1]}])
def test_invalid_schedules_are_rejected(client, changes):
    r = client.post('/api/lighting/schedules', json={'revision': 0, 'schedule': schedule(**changes)})
    assert r.status_code in (404, 422), r.text
    assert client.get('/api/lighting').json()['revision'] == 0


def test_regular_off_schedule_and_one_time_validation(client):
    s = schedule(zone_id='regular-walls-roof', command={'power': False}, repeat='daily', days=[])
    assert client.post('/api/lighting/schedules', json={'revision':0,'schedule':s}).status_code == 200
    past = schedule(repeat='one_time', days=[], date_local='2020-01-01')
    assert client.post('/api/lighting/schedules', json={'revision':1,'schedule':past}).status_code == 422
    assert client.post('/api/lighting/schedules', json={'revision':1,'schedule':past | {'enabled':False}}).status_code == 200


def test_missing_schedule_rolls_back_and_corruption_is_not_reset(client):
    assert client.put('/api/lighting/schedules/missing', json={'revision':0,'schedule':schedule()}).status_code == 404
    assert client.delete('/api/lighting/schedules/missing?revision=0').status_code == 404
    assert lighting_store.document()['revision'] == 0
    with db._sqlite_conn() as conn:
        conn.execute("insert into activity(created_at,source,action,result,details) values (?, 'lighting-config','settings','active','not-json')", (db._now(),))
    assert client.get('/api/lighting').status_code == 503


def test_future_times_use_zone_and_skip_dst_gap_and_second_fold():
    s = schedule(repeat='daily', days=[], time_local='02:30')
    values = lighting.occurrences(s, datetime(2027,3,14,5,tzinfo=timezone.utc))
    assert values[0]['utc'] == '2027-03-15T06:30:00+00:00'
    s['time_local'] = '01:30'
    values = lighting.occurrences(s, datetime(2026,11,1,4,tzinfo=timezone.utc))
    assert values[0]['utc'] == '2026-11-01T05:30:00+00:00'
    values = lighting.occurrences(s, datetime(2026,11,1,5,31,tzinfo=timezone.utc))
    assert values[0]['utc'] == '2026-11-02T06:30:00+00:00'


def test_full_app_registers_lighting_routes_and_static_assets(client):
    full_app.dependency_overrides[access_auth] = lambda: None
    try:
        app_client = TestClient(full_app, raise_server_exceptions=True)
        try:
            assert app_client.get('/api/lighting').status_code == 200
            assert app_client.get('/static/lighting.js').status_code == 200
            assert app_client.get('/static/lighting.css').status_code == 200
        finally:
            app_client.close()
    finally:
        full_app.dependency_overrides.clear()
