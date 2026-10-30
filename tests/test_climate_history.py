from datetime import datetime, timedelta, timezone
from app.climate_history import observe_climate


def sample(now, minute, value, phase='heat', calibration=0):
    return dict(at=(now-timedelta(minutes=minute)).isoformat(),room_f=value,
                phase=phase,calibration_f=calibration)


def test_history_predicts_warming_and_keeps_measurement_distinction():
    now=datetime.now(timezone.utc)
    config={'target_temperature_f':72,'temperature_history':[sample(now,3,68),sample(now,2,69),sample(now,1,70)]}
    row=observe_climate(config,71,50,{'mode':4,'running':True},now)
    assert row['room_trend_f_per_minute']==1
    assert row['projected_room_f']==74
    assert row['moving_toward_target']
    assert 'measurement_time_verified' not in row


def test_outside_only_extends_coasting_when_fresh_and_helpful():
    now=datetime.now(timezone.utc)
    config={'target_temperature_f':72,'outside_checked_at':now.isoformat(),
            'temperature_history':[sample(now,3,75,'fan'),sample(now,2,74.5,'fan'),sample(now,1,74,'fan')]}
    row=observe_climate(config,73.5,55,{'mode':5,'running':True},now)
    assert row['outside_helpful'] and row['projection_minutes']==4
    assert row['moving_toward_target'] and row['projected_room_f']==71.5
    config['outside_checked_at']=(now-timedelta(minutes=31)).isoformat()
    row=observe_climate(config,73.5,55,{'mode':5,'running':True},now)
    assert not row['outside_helpful'] and not row['weather_fresh']
    assert row['projection_minutes']==3


def test_changed_mode_does_not_reuse_heating_trend_as_passive_warming():
    now=datetime.now(timezone.utc)
    config={'temperature_history':[sample(now,3,68),sample(now,2,69),sample(now,1,70)]}
    row=observe_climate(config,71,55,{'mode':5,'running':True},now)
    assert row['room_trend_f_per_minute'] is None


def test_calibration_change_gaps_and_jumps_reset_history():
    now=datetime.now(timezone.utc)
    for config,room in [({'sensor_calibration_f':-1,'temperature_history':[sample(now,1,68)]},69),
                        ({'temperature_history':[sample(now,4,68)]},69),
                        ({'temperature_history':[sample(now,1,68)]},80)]:
        row=observe_climate(config,room,None,{'mode':4,'running':True},now)
        assert len(row['history'])==1
        assert row['room_trend_f_per_minute'] is None


def test_bounded_history_and_insufficient_span_do_not_invent_trend():
    now=datetime.now(timezone.utc)
    config={'temperature_history':[sample(now,100,60),sample(now,.5,71)]}
    row=observe_climate(config,71,None,{'mode':4,'running':True},now)
    assert len(row['history'])==1 and row['projected_room_f'] is None


def test_wrong_direction_trend_does_not_claim_passive_recovery():
    now=datetime.now(timezone.utc)
    config={'target_temperature_f':72,'outside_checked_at':now.isoformat(),
            'temperature_history':[sample(now,3,73,'rest'),sample(now,2,73.5,'rest'),sample(now,1,74,'rest')]}
    row=observe_climate(config,74.5,55,{'running':False,'mode':4},now)
    assert row['outside_helpful'] and not row['moving_toward_target']
