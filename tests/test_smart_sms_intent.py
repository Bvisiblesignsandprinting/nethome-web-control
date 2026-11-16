from unittest.mock import Mock
from datetime import datetime, timezone
import pytest
from app import db, messaging, scheduler
from app.message_intent import smart_power_intent


@pytest.fixture
def sms(monkeypatch, tmp_path):
    monkeypatch.setattr(db, 'DATABASE_URL', None)
    monkeypatch.setattr(db, 'DB_PATH', tmp_path/'sms.db')
    db.init_db()
    db.save_smart_control_config({'enabled': True, 'target_temperature_f': 65})
    unit = Mock()
    monkeypatch.setattr(messaging, 'midea', unit)
    monkeypatch.setattr(messaging, '_smart_status_reply', lambda *a, **kw: 'Smart status')
    return unit


@pytest.mark.parametrize('text', [
    'SMART OFF', 'Turn off Smart Room', 'Turn off smart control',
    'Turn off smart control settings', 'Please turn the Smart Room off.',
    'Disable Smart Room', 'Stop smart control', 'Pause smart auto',
    'Switch off only the smart room', 'smart control off', 'Turn off only the smart',
    'Turn off SmartRoom', 'Turn off SmartControl',
    'Do not turn off the machine turn off only the smart room smart',
    "Don't turn off the AC, just turn Smart Room off", 'Turn off Smart Room, not the machine',
])
def test_smart_off_disables_automation_and_never_commands_ac(sms, text):
    before = db.load_smart_control_config()
    result = messaging.process_text_command(text)
    assert result['ok'] and 'AC power and settings left unchanged' in result['reply']
    after = db.load_smart_control_config()
    assert after['enabled'] is False
    assert after['target_temperature_f'] == before['target_temperature_f']
    sms.command.assert_not_called()
    sms.status.assert_not_called()
    activities = db.list_activity()
    assert not any(a['action']=='device_command' for a in activities)


@pytest.mark.parametrize('text', [
    "Don't turn off Smart Room", 'Do not disable Smart Room',
    "I don't want you to turn off Smart Room", 'Turn off the AC and turn off Smart Room',
    'Turn Smart Room off and then turn Smart Room on', 'Turn off Smart Room at 10 PM',
    'Turn off Smart Room tomorrow', 'Should I turn off Smart Room?',
    'Turn off Smart Room in five minutes',
    'Why did you turn off Smart Room?',
    'Shut down Smart Room',
])
def test_ambiguous_negated_or_future_requests_cannot_fall_through_to_ac(sms, text):
    result = messaging.process_text_command(text)
    assert result['ok'] is False and 'No changes made' in result['reply']
    assert db.load_smart_control_config()['enabled'] is True
    sms.command.assert_not_called()


def test_disabled_controller_does_not_read_sensor_or_issue_commands(sms, monkeypatch):
    messaging.process_text_command('Turn off smart control settings')
    sensor = Mock(side_effect=AssertionError('Disabled controller read sensor'))
    monkeypatch.setattr(scheduler.tuya, 'indoor_sensor', sensor)
    monkeypatch.setattr(scheduler, 'midea', sms)
    result = scheduler.run_smart_control(datetime.now(timezone.utc))
    assert result['enabled'] is False and result['action']=='none'
    sensor.assert_not_called()
    sms.command.assert_not_called()


@pytest.mark.parametrize('text', ['Turn on Smart Room', 'Enable smart control', 'Resume Smart Auto'])
def test_smart_on_changes_only_enablement(sms,text):
    db.save_smart_control_config({'enabled':False})
    assert messaging.process_text_command(text)['ok']
    assert db.load_smart_control_config()['enabled'] is True
    sms.command.assert_not_called()


def test_plain_ac_off_keeps_its_meaning(sms,monkeypatch):
    execute=Mock(return_value={'ok':True,'reply':'AC off'})
    monkeypatch.setattr(messaging,'_execute',execute)
    assert messaging.process_text_command('Turn off the AC')['ok']
    assert execute.call_args.args[0]['action']=='off'
    assert db.load_smart_control_config()['enabled'] is True


@pytest.mark.parametrize('text', ["Don't turn off the machine", 'Do not turn off the AC'])
def test_negated_ac_power_never_becomes_an_off_command(sms,text):
    assert messaging.process_text_command(text)['ok'] is False
    sms.command.assert_not_called()
