import os
import pytest
os.environ['NETHOME_API_TOKEN'] = 'change-me-before-remote-access'

from fastapi.testclient import TestClient
from app import db
from app.main import app, access_auth


@pytest.fixture(scope='module')
def client(tmp_path_factory):
    # MCP's session manager has one lifespan per process. Share the client,
    # isolate persistence, and explicitly authenticate schedule requests.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(db, 'DATABASE_URL', None)
        patch.setattr(db, 'DB_PATH', tmp_path_factory.mktemp('api') / 'nethome.db')
        app.dependency_overrides[access_auth] = lambda: None
        try:
            with TestClient(app) as value:
                yield value
        finally:
            app.dependency_overrides.clear()


def test_health(client):
    r = client.get('/api/health')
    assert r.status_code == 200
    assert r.json()['ok'] is True
    assert 'google_voice_bridge_configured' not in r.json()


def test_google_voice_bridge_is_retired(client):
    assert client.get('/api/google-voice/config').status_code == 404
    assert client.post('/api/google-voice/config', json={}).status_code == 404
    assert client.post('/api/message/google-voice', json={}).status_code == 404

    html = client.get('/static/index.html').text
    javascript = client.get('/static/app.js').text
    assert 'Google Voice' not in html
    assert 'google-voice' not in javascript


def test_schedule_crud(client):
    payload = {
        'name': 'Test',
        'schedule_type': 'weekdays',
        'days': '',
        'start_date': '2026-09-18',
        'end_date': '2026-10-18',
        'time_local': '07:30',
        'mode': 'Cool',
        'temperature': 72,
        'fan': 'Auto',
        'enabled': True,
    }
    r = client.post('/api/schedules', json=payload)
    assert r.status_code == 200, r.text
    sid = r.json()['id']
    assert r.json()['days'] == 'Mon,Tue,Wed,Thu,Fri'
    r = client.patch(f'/api/schedules/{sid}', json={'temperature':70, 'enabled':False})
    assert r.status_code == 200 and r.json()['temperature'] == 70
    assert r.json()['enabled'] == 0
    r = client.delete(f'/api/schedules/{sid}')
    assert r.status_code == 200


def test_one_time_schedule_requires_date(client):
    r = client.post('/api/schedules', json={
        'name':'One time', 'schedule_type':'one_time', 'days':'',
        'time_local':'18:00', 'mode':'Cool', 'temperature':72
    })
    assert r.status_code == 422
