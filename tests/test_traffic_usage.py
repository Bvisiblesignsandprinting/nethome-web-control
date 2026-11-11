from contextlib import contextmanager
from unittest.mock import Mock

import pytest

from app import db, music, tuya_sharing_client as sharing


def test_pairing_presence_stays_fresh_without_reading_credentials(monkeypatch, tmp_path):
    monkeypatch.setattr(db, 'DATABASE_URL', None)
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'traffic.db')
    db.init_db()
    monkeypatch.setattr(sharing, 'load_sharing_record', lambda *_: pytest.fail('Presence check downloaded credential'))
    assert not sharing.configured()
    db.save_sharing_record('session', 'encrypted-private-pairing')
    assert sharing.configured()
    db.save_sharing_record('session', '')
    assert not sharing.configured()


@pytest.mark.parametrize('changed,heartbeat,updates', [(True,False,1),(False,True,1),(False,False,0)])
def test_server_side_comparison_preserves_changes_and_heartbeats(monkeypatch, changed, heartbeat, updates):
    monkeypatch.setattr(db, 'DATABASE_URL', 'test-only')
    cursor = Mock()
    cursor.fetchone.return_value = {'id': 7, 'changed': changed}
    @contextmanager
    def checkout():
        connection = Mock()
        connection.cursor.return_value.__enter__ = Mock(return_value=cursor)
        connection.cursor.return_value.__exit__ = Mock(return_value=False)
        yield connection
    monkeypatch.setattr(db, '_pg_conn', checkout)
    db._save_current_record('cloud','device_state','success','{"state":1}',heartbeat=heartbeat)
    assert sum(call.args[0].startswith('update ') for call in cursor.execute.call_args_list) == updates


def test_library_paging_reuses_scan_but_refresh_expiry_and_delete_are_fresh(monkeypatch):
    store = music.MusicStorage()
    store.origin='https://example.supabase.co'
    store.key='test-key'
    now=[0.0]
    monkeypatch.setattr(music.time,'monotonic',lambda: now[0])
    rows=[{'id':str(i),'name':f'{i:032x}_song.mp3','created_at':f'{i:04}', 'metadata':{'size':100}}
          for i in range(250)]
    scans=[]
    def request(method,path,payload=None):
        if path.startswith('/bucket/'):
            return {'public':False,'file_size_limit':music.MAX_FILE_BYTES,'allowed_mime_types':['audio/mpeg']}
        if method=='DELETE':
            rows.pop()
            return {}
        assert path.startswith('/object/list/')
        scans.append(payload['offset'])
        return rows[payload['offset']:payload['offset']+100]
    monkeypatch.setattr(store,'request',request)
    first=store.tracks(0)
    second=store.tracks(100)
    third=store.tracks(200)
    assert [len(p['tracks']) for p in (first,second,third)]==[100,100,50]
    assert len(scans)==3  # One catalog scan for all three pages, rather than nine requests.
    rows.append({'id':'new','name':'a'*32+'_new.mp3','created_at':'9999','metadata':{'size':100}})
    assert store.tracks(0)['tracks'][0]['title']=='new'
    assert len(scans)==6
    now[0]=61
    store.tracks(100)
    assert len(scans)==9
    store.delete('a'*32+'_new.mp3')
    assert len(store.tracks(200)['tracks'])==50
    assert len(scans)==12
