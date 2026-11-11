import json
from contextlib import contextmanager
from unittest.mock import Mock
import pytest
from app import db


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DATABASE_URL', None)
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'usage.db')
    db.init_db()


def rows(source):
    with db._sqlite_conn() as conn:
        return [dict(r) for r in conn.execute('select * from activity where source=? order by id', (source,))]


def test_repeated_settings_preserve_row_and_dont_write_noops(store):
    db.save_smart_control_config({'enabled': True, 'target_temperature_f': 74.5, 'sensor_calibration_f': 1.2})
    initial = rows('smart-control-config')[0]
    for _ in range(100):
        db.save_smart_control_config({'enabled': True})
    assert rows('smart-control-config') == [initial]
    db.save_smart_control_config({'last_evaluation': {'reason': 'comfort'}})
    saved = rows('smart-control-config')
    assert len(saved) == 1 and saved[0]['id'] == initial['id']
    config = db.load_smart_control_config()
    assert config['enabled'] and config['target_temperature_f'] == 74.5
    assert config['sensor_calibration_f'] == 1.2


def test_sensor_and_auth_updates_do_not_accumulate_rows(store):
    for i in range(100):
        db.save_sensor_cache({'observed_at': i, 'temperature_f': 73.2})
        db.save_cloud_session({'token': 'test-token', 'expires': i})
        db.save_sharing_record('session', 'test-encrypted-' + str(i))
    assert len(rows('tuya-cache')) == len(rows('cloud-auth')) == len(rows('tuya-sharing')) == 1
    assert db.load_sensor_cache()['observed_at'] == 99
    assert db.load_cloud_session()['expires'] == 99
    assert db.load_sharing_record('session') == 'test-encrypted-99'


def test_poll_snapshots_keep_fresh_timestamp_and_commands_remain_events(store):
    db.save_device_state({'running': False}, True)
    first_id = rows('cloud')[0]['id']
    with db._sqlite_conn() as conn:
        conn.execute("update activity set created_at='2020-01-01T00:00:00+00:00'")
    for i in range(100):
        db.save_device_state({'running': bool(i % 2)}, True)
        db.add_activity('tuya', 'indoor_sensor', 'success', str(i))
    latest = db.get_latest_device_state()
    assert latest['state']['running'] and latest['updated_at'].startswith('2026-')
    assert rows('cloud')[0]['id'] == first_id and len(rows('cloud')) == 1
    assert len(rows('tuya')) == 1
    for _ in range(3):
        db.add_activity('web', 'device_command', 'success', 'verified')
        db.add_activity('tuya', 'indoor_sensor', 'error', 'offline')
        db.add_activity('tuya', 'indoor_sensor', 'success', '73F')
    assert len(rows('web')) == 3
    assert len([r for r in rows('tuya') if r['result'] == 'error']) == 3


def test_existing_history_and_other_private_records_survive(store):
    with db._sqlite_conn() as conn:
        for i in range(3):
            conn.execute("insert into activity(created_at,source,action,result,details) values(?,?,?,?,?)",
                         ('2026-01-01', 'smart-control-config', 'settings', 'active', json.dumps({'enabled': bool(i)})))
    before = rows('smart-control-config')
    db.save_sms_config({'phone': 'test'})
    db.save_sharing_record('pending', 'test-pending')
    db.save_smart_control_config({'target_temperature_f': 72})
    after = rows('smart-control-config')
    assert len(after) == 3 and after[:2] == before[:2]
    assert db.load_sms_config() == {'phone': 'test'}
    assert db.load_sharing_record('pending') == 'test-pending'


def test_latest_state_uses_observation_time_across_sources(store):
    db.save_device_state({'reading': 'old-cloud'}, True, source='cloud')
    db.save_device_state({'reading': 'worker'}, True, source='worker')
    db.save_device_state({'reading': 'new-cloud'}, True, source='cloud')
    assert db.get_latest_device_state()['state']['reading'] == 'new-cloud'


def test_cloud_lock_returns_pooled_connection_without_closing(monkeypatch):
    monkeypatch.setattr(db, 'DATABASE_URL', 'test-only')
    connection = Mock()
    cursor = Mock()
    connection.cursor.return_value.__enter__ = Mock(return_value=cursor)
    connection.cursor.return_value.__exit__ = Mock(return_value=False)
    released = []
    @contextmanager
    def checkout():
        try:
            yield connection
        finally:
            released.append(True)
    monkeypatch.setattr(db, '_pg_conn', checkout)
    with pytest.raises(RuntimeError):
        with db.cloud_operation_lock():
            raise RuntimeError('uncertain command: never replay')
    assert released == [True]
    connection.close.assert_not_called()
    assert cursor.execute.call_count == 2


def test_postgres_noop_uses_no_update_or_delete(monkeypatch):
    monkeypatch.setattr(db, 'DATABASE_URL', 'test-only')
    cursor = Mock()
    cursor.fetchone.return_value = {'id': 7, 'changed': False}
    @contextmanager
    def checkout():
        connection = Mock()
        connection.cursor.return_value.__enter__ = Mock(return_value=cursor)
        connection.cursor.return_value.__exit__ = Mock(return_value=False)
        yield connection
    monkeypatch.setattr(db, '_pg_conn', checkout)
    db._save_current_record('private', 'settings', 'active', '{}')
    assert cursor.execute.call_count == 1
    assert 'for update' in cursor.execute.call_args.args[0]


def test_pool_is_lazy_bounded_and_reused(monkeypatch):
    import psycopg_pool
    constructor = Mock()
    monkeypatch.setattr(psycopg_pool, 'ConnectionPool', constructor)
    monkeypatch.setattr(db, '_PG_POOL', None)
    monkeypatch.setattr(db, 'DATABASE_URL', 'postgresql://test.invalid/test')
    first = db._get_pg_pool()
    assert db._get_pg_pool() is first
    constructor.assert_called_once()
    options = constructor.call_args.kwargs
    assert options['min_size'] == 0 and options['max_size'] == 6
    assert options['kwargs']['prepare_threshold'] is None
    assert options['kwargs']['connect_timeout'] == 8
