import json
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from app import radio

@pytest.fixture
def client():
    app=FastAPI()
    def auth(): raise HTTPException(401,'Login required')
    app.include_router(radio.radio_router(auth))
    c=TestClient(app);c.auth_dep=auth
    return c

def allow(c):c.app.dependency_overrides[c.auth_dep]=lambda:None

def test_radio_requires_owner_auth(client):
    assert client.get('/api/music/radio/stations').status_code==401
    assert client.post('/api/music/radio/stations/y24-1/playback').status_code==401

def test_live_catalog_has_unique_safe_direct_streams(client):
    allow(client);r=client.get('/api/music/radio/stations');assert r.status_code==200
    rows=r.json()['stations'];assert len(rows)==60;assert len({v['id'] for v in rows})==60
    assert r.json()['source_url']=='https://www.yiddish24.com/streams'
    assert rows[0]['url']=='https://music.y24.app/1';assert rows[3]['url']=='https://music2.y24.app/37'
    assert all(v['format']=='mp3' and v['name'] and v['title'] for v in rows)
    assert 'private' in r.headers['cache-control']

def test_playback_uses_only_curated_station_and_never_proxies(client):
    allow(client);r=client.post('/api/music/radio/stations/y24-1/playback')
    assert r.status_code==200;assert r.json()['url']=='https://music.y24.app/1';assert r.json()['live'] is True
    assert r.headers['cache-control']=='no-store'
    assert client.post('/api/music/radio/stations/localhost/playback').status_code==404
    assert client.post('/api/music/radio/stations/y24-1/playback',json={'url':'http://127.0.0.1'}).json()['url']=='https://music.y24.app/1'

def test_catalog_rejects_foreign_domains_and_unsafe_urls(tmp_path,monkeypatch):
    p=tmp_path/'stations.json';monkeypatch.setattr(radio,'CATALOG_PATH',p)
    for url in ['https://music.y24.app.evil.test/1','http://music.y24.app/1','https://user@music.y24.app/1','https://music.y24.app/1?target=localhost','https://music.y24.app:8443/1']:
        p.write_text(json.dumps([{'url':url}]))
        with pytest.raises(RuntimeError):radio.stations()
