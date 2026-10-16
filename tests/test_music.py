"""Exercise private-library boundaries without production keys or appliance calls."""
import json
from urllib.error import HTTPError, URLError

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import music
from app.main import access_auth


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(music.music_router(access_auth))
    return TestClient(app)


@pytest.fixture
def authenticated(client):
    client.app.dependency_overrides[access_auth] = lambda: None
    return client


@pytest.fixture
def storage(monkeypatch):
    store = music.MusicStorage()
    store.origin = 'https://example.supabase.co'
    store.key = 'test-key-not-a-credential'
    monkeypatch.setattr(music, 'storage', store)
    return store


def test_all_routes_require_login(client):
    for method, path, body in [
        ('GET', '/status', None), ('GET', '/tracks', None),
        ('POST', '/uploads', {'filename': 'song.mp3', 'size': 4}),
        ('POST', '/tracks/anything/playback', None), ('DELETE', '/tracks/anything', None),
    ]:
        response = client.request(method, '/api/music' + path, json=body)
        assert response.status_code == 401


def test_unconfigured_library_is_explicit(authenticated, storage):
    storage.key = ''
    assert authenticated.get('/api/music/status').json() == {
        'storage_ready': False, 'max_file_bytes': music.MAX_FILE_BYTES,
        'formats': ['mp3'], 'panel_playback_ready': False,
    }
    assert authenticated.get('/api/music/tracks').status_code == 503


@pytest.mark.parametrize('filename,size', [
    ('song.wav', 5), ('../song.mp3', 5), ('folder\\song.mp3', 5),
    ('song.mp3', 0), ('song.mp3', music.MAX_FILE_BYTES + 1), ('song.mp3', '10'),
])
def test_invalid_uploads_never_reach_storage(authenticated, storage, monkeypatch, filename, size):
    def fail(*args):
        pytest.fail('Invalid upload reached storage')
    monkeypatch.setattr(storage, 'request', fail)
    assert authenticated.post('/api/music/uploads', json={'filename': filename, 'size': size}).status_code == 422


def bucket():
    return {'public': False, 'file_size_limit': music.MAX_FILE_BYTES, 'allowed_mime_types': ['audio/mpeg']}


def test_upload_uses_unique_object_and_only_exposes_signed_url(authenticated, storage, monkeypatch):
    calls = []
    def request(method, path, payload=None):
        calls.append((method, path, payload))
        return bucket() if path.startswith('/bucket/') else {'url': path + '?token=upload-ticket'}
    monkeypatch.setattr(storage, 'request', request)
    response = authenticated.post('/api/music/uploads', json={'filename': 'My Song.MP3', 'size': 1000})
    assert response.status_code == 200
    body = response.json()
    assert body['id'].endswith('_My-Song.mp3')
    assert body['upload_url'].startswith(storage.origin + '/storage/v1/object/upload/sign/')
    assert storage.key not in response.text
    assert response.headers['cache-control'] == 'no-store'
    second = authenticated.post('/api/music/uploads', json={'filename': 'My Song.MP3', 'size': 1000})
    assert second.json()['id'] != body['id']
    assert calls[1][2] == {}


@pytest.mark.parametrize('changes', [{'public': True}, {'file_size_limit': None},
    {'file_size_limit': music.MAX_FILE_BYTES + 1}, {'allowed_mime_types': ['*/*']}])
def test_insecure_bucket_blocks_upload(authenticated, storage, monkeypatch, changes):
    monkeypatch.setattr(storage, 'request', lambda *args: bucket() | changes)
    assert authenticated.post('/api/music/uploads', json={'filename': 'song.mp3', 'size': 10}).status_code == 503


def test_paginated_tracks_ignore_folders_and_foreign_paths(authenticated, storage, monkeypatch):
    name = 'a' * 32 + '_My-Song.mp3'
    def request(method, path, payload=None):
        if path.startswith('/bucket/'):
            return bucket()
        assert payload['offset'] == 100
        return [{'name': name, 'id': 'object-id', 'metadata': {'size': 1200}},
                {'name': 'foreign.mp3', 'id': 'other'}, {'name': 'folder', 'id': None}]
    monkeypatch.setattr(storage, 'request', request)
    result = authenticated.get('/api/music/tracks?offset=100').json()
    assert result['next_offset'] is None
    assert result['tracks'] == [{'id': name, 'title': 'My Song', 'size': 1200, 'created_at': None}]
    assert authenticated.get('/api/music/tracks?offset=-1').status_code == 422


def test_playback_checks_existence_and_issues_expiring_url(authenticated, storage, monkeypatch):
    name = 'a' * 32 + '_song.mp3'
    calls = []
    def request(method, path, payload=None):
        calls.append((method, path, payload))
        if path.startswith('/bucket/'):
            return bucket()
        return {'signedURL': '/object/sign/nethome-music/tracks/' + name + '?token=play-ticket'}
    monkeypatch.setattr(storage, 'request', request)
    response = authenticated.post('/api/music/tracks/' + name + '/playback')
    assert response.status_code == 200
    assert calls[1][1].startswith('/object/info/')
    assert calls[2][2] == {'expiresIn': 3600}
    assert response.json()['playback_device'] == 'browser'


def test_delete_is_limited_to_music_prefix(authenticated, storage, monkeypatch):
    name = 'a' * 32 + '_song.mp3'
    calls = []
    monkeypatch.setattr(storage, 'request', lambda *args: calls.append(args))
    assert authenticated.delete('/api/music/tracks/' + name).json() == {'ok': True}
    assert calls == [('DELETE', '/object/nethome-music', {'prefixes': ['tracks/' + name]})]
    assert authenticated.delete('/api/music/tracks/not-a-track.mp3').status_code == 404


@pytest.mark.parametrize('error', [URLError('secret detail'), HTTPError('url', 403, 'secret detail', {}, None)])
def test_storage_errors_do_not_leak_secrets(storage, monkeypatch, error):
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(music, 'urlopen', fail)
    with pytest.raises(music.HTTPException) as exc:
        storage.request('GET', '/bucket/nethome-music')
    assert exc.value.status_code == 502
    assert 'secret' not in exc.value.detail


def test_storage_http_request_uses_server_key(storage, monkeypatch):
    from io import BytesIO
    def open_request(request, timeout):
        assert request.full_url == 'https://example.supabase.co/storage/v1/object/list/nethome-music'
        assert request.get_header('Authorization') == 'Bearer ' + storage.key
        assert json.loads(request.data) == {'prefix': 'tracks/'}
        assert timeout == 15
        return BytesIO(b'[]')
    monkeypatch.setattr(music, 'urlopen', open_request)
    assert storage.request('POST', '/object/list/nethome-music', {'prefix': 'tracks/'}) == []


def test_signed_url_rejects_foreign_origins(storage):
    with pytest.raises(music.HTTPException):
        storage.absolute_signed_url('https://other.example/object/a')
