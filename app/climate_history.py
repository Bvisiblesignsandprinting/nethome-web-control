"""Small, bounded observation history for anticipatory comfort control."""
from datetime import datetime
import math


def observe_climate(config, room_f, outside_f, state, now):
    phase = {2:'cool',4:'heat',5:'fan'}.get(int(state.get('mode') or 0), 'other') if state.get('running') else 'rest'
    calibration = float(config.get('sensor_calibration_f') or 0)
    rows = []
    for row in config.get('temperature_history') or []:
        try:
            age = (now-datetime.fromisoformat(row['at'])).total_seconds()
            value = float(row['room_f'])
            if 0 <= age <= 900 and math.isfinite(value) and row.get('calibration_f') == calibration:
                rows.append(dict(row))
        except (ValueError, TypeError, KeyError):
            continue
    rows.sort(key=lambda row: row['at'])
    if rows and (now-datetime.fromisoformat(rows[-1]['at'])).total_seconds() > 180:
        rows = []
    if rows and abs(room_f-float(rows[-1]['room_f'])) > 5:
        rows = []  # Do not extrapolate across a jump or a changed sensor.
    sample = {'at':now.isoformat(), 'room_f':room_f, 'phase':phase, 'calibration_f':calibration}
    if rows and (now-datetime.fromisoformat(rows[-1]['at'])).total_seconds() < 45:
        rows[-1] = sample
    else:
        rows.append(sample)
    rows = rows[-20:]
    # Fit only the recent uninterrupted phase, so old heating cannot be used
    # as evidence of warming after the compressor has stopped.
    recent = []
    for row in reversed(rows):
        if row['phase'] != phase or (now-datetime.fromisoformat(row['at'])).total_seconds() > 360:
            break
        recent.append(row)
    trend = None
    if len(recent) >= 3:
        xs = [(datetime.fromisoformat(r['at'])-now).total_seconds()/60 for r in recent]
        ys = [r['room_f'] for r in recent]
        if max(xs)-min(xs) >= 2:
            xm, ym = sum(xs)/len(xs), sum(ys)/len(ys)
            denominator = sum((x-xm)**2 for x in xs)
            if denominator:
                slope = sum((x-xm)*(y-ym) for x,y in zip(xs,ys))/denominator
                if abs(slope) <= 1:
                    trend = round(slope, 3)
    target = float(config.get('target_temperature_f') or 73)
    weather_fresh = False
    try:
        age = (now-datetime.fromisoformat(config['outside_checked_at'])).total_seconds()
        weather_fresh = -5 <= age <= 1800 and outside_f is not None and math.isfinite(outside_f) and -80 <= outside_f <= 140
    except (KeyError, TypeError, ValueError):
        pass
    helpful = bool(weather_fresh and ((room_f>target and outside_f<target-2) or (room_f<target and outside_f>target+2)))
    moving_toward = trend is not None and trend*(target-room_f) > 0 and abs(trend) >= .05
    horizon = 4 if helpful and phase in {'rest','fan'} else 3
    projected = round(room_f+trend*horizon, 2) if trend is not None else None
    return {'history':rows, 'room_trend_f_per_minute':trend, 'projected_room_f':projected,
            'projection_minutes':horizon, 'outside_helpful':helpful,
            'weather_fresh':weather_fresh, 'moving_toward_target':moving_toward}
