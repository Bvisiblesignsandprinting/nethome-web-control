from datetime import datetime, timedelta, timezone
from dataclasses import replace
from unittest.mock import Mock
import pytest
from app import db, scheduler, main, tuya_sharing_client as sharing
from app.sensor_health import sensor_control_issue
from app.tuya_client import TuyaCloudError


@pytest.fixture
def thermostat(monkeypatch, tmp_path):
    monkeypatch.setattr(db, 'DATABASE_URL', None)
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'thermostat.db')
    db.init_db()
    now = datetime.now(timezone.utc)
    config = dict(enabled=True, target_temperature_f=72, preferred_mode='auto',
                  deadband_f=1, min_command_interval_minutes=6,
                  last_command_at=None, comfort_since=None)
    sensor = dict(ok=True, online=True, source='tuya-sharing', temperature_f=65,
                  measurement_time_verified=False, updated_at=None,
                  fetched_at=now.isoformat(), temperature_observed_at=now.isoformat())
    state = dict(running=False, mode=4, target_temperature_c=22, fan_speed=40)
    unit = Mock()
    unit.status.side_effect = lambda: dict(state)
    unit.command.return_value = dict(running=False, mode=4, verified=True)
    def save(patch):
        config.update(patch)
        return dict(config)
    monkeypatch.setattr(scheduler, 'settings', replace(scheduler.settings, allow_writes=True))
    monkeypatch.setattr(scheduler, 'load_smart_control_config', lambda: dict(config))
    monkeypatch.setattr(scheduler, 'save_smart_control_config', save)
    monkeypatch.setattr(scheduler, 'save_device_state', Mock())
    monkeypatch.setattr(scheduler, 'add_activity', Mock())
    monkeypatch.setattr(scheduler, '_outside_temperature', lambda c, n: (40, c))
    monkeypatch.setattr(scheduler, 'midea', unit)
    monkeypatch.setattr(scheduler.tuya, 'indoor_sensor', Mock(side_effect=lambda **kw: dict(sensor)))
    return now, config, sensor, state, unit


def test_recent_cloud_snapshot_can_heat_without_fabricating_measurement_time(thermostat):
    now, config, sensor, state, unit = thermostat
    result = scheduler.run_smart_control(now)
    assert result['action'] == 'adjusted' and result['midea_mode'] == 'heat'
    assert unit.command.call_args.args[0]['running']
    assert sensor['updated_at'] is None and not sensor['measurement_time_verified']


def test_screenshot_overheat_stops_heat_before_six_minute_wait(thermostat):
    now, config, sensor, state, unit = thermostat
    sensor['temperature_f'] = 87.2
    state.update(running=True, mode=4)
    config['last_command_at'] = (now - timedelta(seconds=10)).isoformat()
    result = scheduler.run_smart_control(now)
    assert result['reason'] == 'target_reached'
    unit.command.assert_called_once_with({'action': 'off'})


def test_hot_room_starts_cooling_after_restart_interval(thermostat):
    now, config, sensor, state, unit = thermostat
    sensor['temperature_f'] = 87.2
    state['target_temperature_c'] = 30
    result = scheduler.run_smart_control(now)
    assert result['midea_mode'] == 'cool'
    assert result['midea_setpoint_f'] == 70


def test_heat_setpoint_does_not_ratchet_up_each_cycle(thermostat):
    now, config, sensor, state, unit = thermostat
    state['target_temperature_c'] = 30
    assert scheduler.run_smart_control(now)['midea_setpoint_f'] == 73.5


def test_cooling_stops_at_target_immediately(thermostat):
    now, config, sensor, state, unit = thermostat
    sensor['temperature_f'] = 72
    state.update(running=True, mode=2)
    assert scheduler.run_smart_control(now)['reason'] == 'comfort_circulation'
    unit.command.assert_called_once_with({'action':'set','mode':'fan','fan':'low','running':True})


@pytest.mark.parametrize('issue', ['offline', 'fetch_old', 'frozen', 'missing_observation', 'nan', 'failure'])
def test_sensor_failures_stop_running_heat(thermostat, monkeypatch, issue):
    now, config, sensor, state, unit = thermostat
    state.update(running=True, mode=4)
    if issue == 'offline': sensor['online'] = False
    if issue == 'fetch_old': sensor['fetched_at'] = (now - timedelta(seconds=91)).isoformat()
    if issue == 'frozen': sensor['temperature_observed_at'] = (now - timedelta(minutes=15)).isoformat()
    if issue == 'missing_observation': sensor.pop('temperature_observed_at')
    if issue == 'nan': sensor['temperature_f'] = float('nan')
    if issue == 'failure':
        monkeypatch.setattr(scheduler.tuya, 'indoor_sensor', Mock(side_effect=TuyaCloudError('unavailable')))
    result = scheduler.run_smart_control(now)
    assert result['reason'].startswith('sensor_')
    unit.command.assert_called_once_with({'action': 'off'})


def test_unchanged_sensor_cannot_restart_heat_until_temperature_changes(thermostat):
    now, config, sensor, state, unit = thermostat
    sensor['temperature_observed_at'] = (now - timedelta(minutes=16)).isoformat()
    assert scheduler.run_smart_control(now)['reason'] == 'sensor_not_changing'
    unit.command.assert_not_called()
    sensor['temperature_observed_at'] = (now + timedelta(seconds=30)).isoformat()
    sensor['fetched_at'] = sensor['temperature_observed_at']
    assert scheduler.run_smart_control(now + timedelta(seconds=30))['action'] == 'adjusted'


def test_overlapping_wakeups_do_not_duplicate_commands(thermostat):
    now, config, sensor, state, unit = thermostat
    first = scheduler.run_smart_control(now)
    assert scheduler.run_smart_control(now + timedelta(seconds=1)) == first
    unit.command.assert_called_once()


def test_smart_off_never_commands_ac(thermostat):
    now, config, sensor, state, unit = thermostat
    config['enabled'] = False
    state.update(running=True, mode=4)
    assert scheduler.run_smart_control(now)['action'] == 'none'
    unit.assert_not_called()
    assert not unit.mock_calls


def test_failed_stop_is_visible(thermostat):
    now, config, sensor, state, unit = thermostat
    sensor['online'] = False
    state.update(running=True, mode=4)
    unit.command.side_effect = RuntimeError('unreachable')
    assert scheduler.run_smart_control(now)['stop_failed'] is True


def test_future_or_malformed_fetch_fails_closed(thermostat):
    now, _, sensor, _, _ = thermostat
    for stamp in ('not-a-date', (now + timedelta(minutes=1)).isoformat()):
        sensor['fetched_at'] = stamp
        assert sensor_control_issue(sensor, now) == 'sensor_stale'


def test_run_endpoint_requires_login_and_same_origin(thermostat, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(main, 'settings', replace(main.settings, api_token='test-api-key'))
    client = TestClient(main.app)
    assert client.post('/api/smart-control/run').status_code == 401
    headers = {'Authorization':'Bearer test-api-key', 'Origin':'https://evil.example'}
    assert client.post('/api/smart-control/run', headers=headers).status_code == 403
    monkeypatch.setattr(main, 'run_smart_control', lambda:{'enabled':False,'action':'none'})
    headers['Origin'] = 'http://testserver'
    assert client.post('/api/smart-control/run', headers=headers).json()['action'] == 'none'


def test_controller_outage_does_not_claim_ready(thermostat, monkeypatch):
    now, config, sensor, state, unit = thermostat
    from app.sensor_health import evaluation_issue
    assert evaluation_issue(config, now) == 'controller_not_checked'
    config['last_evaluation'] = {'checked_at': (now-timedelta(minutes=3)).isoformat()}
    assert evaluation_issue(config, now) == 'controller_overdue'
    config['last_evaluation'] = {'checked_at': now.isoformat(), 'stop_failed':True}
    assert evaluation_issue(config, now) == 'stop_failed'


def test_heat_to_cool_waits_after_stop_before_restarting(thermostat):
    now, config, sensor, state, unit = thermostat
    sensor['temperature_f'] = 87.2
    state.update(running=True, mode=4)
    assert scheduler.run_smart_control(now)['action'] == 'off'
    state['running'] = False
    sensor['fetched_at'] = (now+timedelta(minutes=1)).isoformat()
    result = scheduler.run_smart_control(now+timedelta(minutes=1))
    assert result['reason'] == 'minimum_interval'
    unit.command.assert_called_once()
    sensor['fetched_at'] = (now+timedelta(minutes=6)).isoformat()
    assert scheduler.run_smart_control(now+timedelta(minutes=6))['midea_mode'] == 'cool'


def test_sensor_loss_also_stops_units_left_in_auto_mode(thermostat):
    now, config, sensor, state, unit = thermostat
    sensor['online'] = False
    state.update(running=True, mode=1)
    assert scheduler.run_smart_control(now)['action'] == 'off'
    unit.command.assert_called_once_with({'action':'off'})


def test_failed_command_is_saved_as_failure_not_ready(thermostat):
    now, config, sensor, state, unit = thermostat
    unit.command.side_effect = RuntimeError('unreachable')
    assert scheduler.run_smart_control(now)['reason'] == 'ac_unavailable'
    assert config['last_evaluation']['action'] == 'error'


@pytest.mark.parametrize('delay', [6, 12, 24])
def test_slow_successful_sensor_fetch_is_not_mistaken_for_future_data(thermostat, monkeypatch, delay):
    now, config, sensor, state, unit = thermostat
    sensor['fetched_at'] = (now + timedelta(seconds=delay)).isoformat()
    sensor['temperature_observed_at'] = sensor['fetched_at']
    ticks = iter([100.0, 100.0, 100.0, 100.0 + delay])
    monkeypatch.setattr(scheduler, 'monotonic', lambda: next(ticks))
    result = scheduler.run_smart_control(now)
    assert result['action'] == 'adjusted'
    assert result['midea_mode'] == 'heat'
    assert result['checked_at'] == sensor['fetched_at']


def test_fetch_delay_does_not_extend_the_stuck_temperature_deadline(thermostat, monkeypatch):
    now, config, sensor, state, unit = thermostat
    sensor['temperature_observed_at'] = (now - timedelta(seconds=895)).isoformat()
    sensor['fetched_at'] = (now + timedelta(seconds=12)).isoformat()
    state.update(running=True, mode=4)
    ticks = iter([100.0, 100.0, 100.0, 112.0])
    monkeypatch.setattr(scheduler, 'monotonic', lambda: next(ticks))
    assert scheduler.run_smart_control(now)['reason'] == 'sensor_not_changing'
    unit.command.assert_called_once_with({'action':'off'})


def test_fresh_fetch_does_not_hide_the_controllers_last_sensor_stop(thermostat):
    now, config, sensor, state, unit = thermostat
    from app.sensor_health import evaluation_issue
    config['last_evaluation'] = {'checked_at':now.isoformat(), 'action':'off', 'reason':'sensor_stale'}
    assert sensor_control_issue(sensor, now) is None
    assert evaluation_issue(config, now) == 'sensor_stale'
    config['last_evaluation'] = {'checked_at':now.isoformat(), 'action':'hold', 'reason':'minimum_interval'}
    assert evaluation_issue(config, now) is None


def test_lock_and_config_delay_is_counted_before_sensor_io(thermostat, monkeypatch):
    now, config, sensor, state, unit = thermostat
    sensor['fetched_at'] = (now + timedelta(seconds=20)).isoformat()
    sensor['temperature_observed_at'] = sensor['fetched_at']
    # 8 seconds before the inner decision, then 12 seconds reading the sensor.
    ticks = iter([100.0, 108.0, 108.0, 120.0])
    monkeypatch.setattr(scheduler, 'monotonic', lambda: next(ticks))
    result = scheduler.run_smart_control(now)
    assert result['action'] == 'adjusted'
    assert result['checked_at'] == sensor['fetched_at']


@pytest.mark.parametrize('mode,room', [(4,71.2),(2,72.8)])
def test_entering_comfort_band_uses_low_fan_without_waiting(thermostat,mode,room):
    now, config, sensor, state, unit = thermostat
    state.update(running=True,mode=mode,fan_speed=60)
    sensor['temperature_f']=room
    config['last_command_at']=(now-timedelta(seconds=10)).isoformat()
    assert scheduler.run_smart_control(now)['action']=='circulate'
    unit.command.assert_called_once_with({'action':'set','mode':'fan','fan':'low','running':True})


def test_small_heat_overshoot_coasts_without_cooling(thermostat):
    now, config, sensor, state, unit = thermostat
    state.update(running=False,mode=4)
    sensor['temperature_f']=74.7
    result=scheduler.run_smart_control(now)
    assert result['reason']=='settling_before_reversal'
    assert result['midea_mode']=='fan'


def test_opposite_demand_must_persist_ten_minutes(thermostat):
    now, config, sensor, state, unit = thermostat
    sensor['temperature_f']=76
    state.update(running=False,mode=4)
    assert scheduler.run_smart_control(now)['reason']=='confirming_opposite_demand'
    state.update(running=True,mode=5,fan_speed=40)
    for minute in (1,6):
        sensor['fetched_at']=(now+timedelta(minutes=minute)).isoformat()
        assert scheduler.run_smart_control(now+timedelta(minutes=minute))['reason']=='confirming_opposite_demand'
    sensor['fetched_at']=(now+timedelta(minutes=10)).isoformat()
    assert scheduler.run_smart_control(now+timedelta(minutes=10))['midea_mode']=='cool'


def test_brief_overshoot_resets_reversal_confirmation(thermostat):
    now, config, sensor, state, unit = thermostat
    config.update(last_conditioning_mode='heat',opposite_demand_mode='cool',
                  opposite_demand_since=(now-timedelta(minutes=8)).isoformat())
    state.update(running=True,mode=5,fan_speed=40)
    sensor['temperature_f']=74
    assert scheduler.run_smart_control(now)['reason']=='settling_before_reversal'
    assert config['opposite_demand_since'] is None
    unit.command.assert_not_called()


def test_fan_circulation_rests_after_seven_minutes(thermostat):
    now, config, sensor, state, unit = thermostat
    sensor['temperature_f']=72
    state.update(running=True,mode=5,fan_speed=40)
    config['circulation_since']=(now-timedelta(minutes=7)).isoformat()
    assert scheduler.run_smart_control(now)['action']=='off'
    unit.command.assert_called_once_with({'action':'off'})
    state['running']=False
    sensor['fetched_at']=(now+timedelta(minutes=1)).isoformat()
    assert scheduler.run_smart_control(now+timedelta(minutes=1))['action']=='hold'
    unit.command.assert_called_once()
