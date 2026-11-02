import json
from unittest.mock import Mock
import pytest
from app import db, messaging


def test_legacy_saved_heat_pin_loads_auto_without_changing_target_or_enablement(monkeypatch,tmp_path):
    monkeypatch.setattr(db,'DATABASE_URL',None)
    monkeypatch.setattr(db,'DB_PATH',tmp_path/'legacy.db')
    db.init_db()
    with db._sqlite_conn() as conn:
        conn.execute("insert into activity(created_at,source,action,result,details) values (?,?,?,?,?)",
                     ('2026-11-02T12:00:00+00:00','smart-control-config','settings','active',
                      json.dumps(dict(enabled=False,target_temperature_f=75,preferred_mode='heat',sensor_calibration_f=-1))))
    config=db.load_smart_control_config()
    assert config['preferred_mode']=='auto'
    assert config['target_temperature_f']==75 and config['enabled'] is False
    assert config['sensor_calibration_f']==-1
    assert db.save_smart_control_config({'preferred_mode':'cool'})['preferred_mode']=='auto'


@pytest.mark.parametrize('mode',['HEAT','COOL','AUTO'])
def test_sms_legacy_mode_commands_cannot_restrict_smart_auto(monkeypatch,mode):
    save=Mock(return_value={'ok':True})
    monkeypatch.setattr(messaging,'_set_smart_config',save)
    command='SMART MODE '+mode
    assert messaging._handle_smart_command(command,command)['ok']
    patch,label=save.call_args.args
    assert patch['preferred_mode']=='auto' and 'enabled' not in patch
    assert 'chooses heat, cool or fan' in label
