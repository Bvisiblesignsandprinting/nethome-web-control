import os
os.environ['NETHOME_API_TOKEN'] = 'change-me-before-remote-access'

from fastapi.testclient import TestClient
from app.main import app


def test_health():
    with TestClient(app) as client:
        r = client.get('/api/health')
        assert r.status_code == 200
        assert r.json()['ok'] is True


def test_schedule_crud():
    with TestClient(app) as client:
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


def test_one_time_schedule_requires_date():
    with TestClient(app) as client:
        r = client.post('/api/schedules', json={
            'name':'One time', 'schedule_type':'one_time', 'days':'',
            'time_local':'18:00', 'mode':'Cool', 'temperature':72
        })
        assert r.status_code == 422
