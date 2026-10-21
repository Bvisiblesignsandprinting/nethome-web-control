import sqlite3
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from app import panel_wifi as wifi

@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setattr(wifi.db,'DATABASE_URL',None)
    monkeypatch.setattr(wifi.db,'DB_PATH',tmp_path/'wifi.db')
    monkeypatch.setattr(wifi,'settings',type('Config',(),{'api_token':'test-only-backup-key','panel_wifi_key':None})())
    app=FastAPI()
    def auth(): raise HTTPException(401,'Login required')
    app.include_router(wifi.wifi_router(auth))
    c=TestClient(app);c.auth_dependency=auth
    return c

def test_requires_auth_and_no_public_access(client):
    assert client.get('/api/panel/wifi/aabbccddeeff').status_code==401
    assert client.post('/api/panel/wifi/aabbccddeeff',json={'profiles':[]}).status_code==401

def allow(c):c.app.dependency_overrides[c.auth_dependency]=lambda:None

def test_backup_encrypted_merge_and_restore_after_new_firmware(client):
    allow(client);url='/api/panel/wifi/aa:bb:cc:dd:ee:ff'
    assert client.post(url,json={'profiles':[{'ssid':'Old network','password':'old-password'}]}).status_code==200
    assert client.post(url,json={'profiles':[{'ssid':'New network','password':'new-password'}]}).json()['profile_count']==2
    # An empty/new firmware must not erase server backups.
    client.post(url,json={'profiles':[]})
    r=client.get(url);assert r.headers['cache-control']=='no-store, private'
    assert {x['ssid']:x['password'] for x in r.json()['profiles']}=={'Old network':'old-password','New network':'new-password'}
    with sqlite3.connect(wifi.db.DB_PATH) as db:
        raw=db.execute('select ciphertext from panel_wifi').fetchone()[0]
        assert 'password' not in raw and 'network' not in raw
    assert client.get('/api/panel/wifi/000000000000').json()=={'profiles':[]}

def test_update_not_duplicate_and_invalid_input(client):
    allow(client);url='/api/panel/wifi/aabbccddeeff'
    for password in ['first','second']:
        assert client.post(url,json={'profiles':[{'ssid':'Home','password':password}]}).status_code==200
    assert client.get(url).json()['profiles']==[{'ssid':'Home','password':'second'}]
    assert client.post(url,json={'profiles':[{'ssid':'X','password':'x'*65}]}).status_code==422
    assert client.get('/api/panel/wifi/not-a-device').status_code==422

def test_cipher_tampering_and_wrong_key_fail_closed(client,monkeypatch):
    allow(client);url='/api/panel/wifi/aabbccddeeff'
    client.post(url,json={'profiles':[{'ssid':'Home','password':'top-secret'}]})
    monkeypatch.setattr(wifi,'settings',type('Config',(),{'api_token':'wrong-key','panel_wifi_key':None})())
    r=client.get(url);assert r.status_code==503;assert 'top-secret' not in r.text
