"""Separate cloud contact time from physical measurement evidence."""
from datetime import datetime, timezone
import math

MAX_FETCH_AGE = 90
MAX_UNCHANGED_AGE = 15 * 60


def sensor_control_issue(sensor, now=None):
    now = now or datetime.now(timezone.utc)
    if not sensor or not sensor.get('ok'):
        return 'sensor_unavailable'
    if sensor.get('online') is False:
        return 'sensor_offline'
    value = sensor.get('temperature_f')
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return 'sensor_invalid'
    def age(field):
        try:
            stamp = datetime.fromisoformat(str(sensor[field]).replace('Z', '+00:00'))
            return (now - stamp.astimezone(timezone.utc)).total_seconds()
        except (KeyError, TypeError, ValueError):
            return None
    if sensor.get('source') == 'tuya-sharing':
        if sensor.get('online') is not True:
            return 'sensor_offline'
        # A cloud fetch is NOT a new physical sample. Bound both separately.
        fetched = age('fetched_at')
        changed = age('temperature_observed_at')
        if fetched is None or not -5 <= fetched <= MAX_FETCH_AGE:
            return 'sensor_stale'
        if changed is None or not -5 <= changed < MAX_UNCHANGED_AGE:
            return 'sensor_not_changing'
        return None
    if sensor.get('measurement_time_verified') is False:
        return 'sensor_measurement_time_unverified'
    if sensor.get('updated_at'):
        measured = age('updated_at')
        if measured is None or not -5 <= measured <= 600:
            return 'sensor_stale'
    return None


def control_message(issue):
    return {
        'sensor_unavailable': 'Sensor unavailable; automatic control paused',
        'sensor_offline': 'Sensor offline; automatic control paused',
        'sensor_invalid': 'Invalid temperature; automatic control paused',
        'sensor_stale': 'Sensor connection is old; automatic control paused',
        'sensor_not_changing': 'Waiting for a temperature change; automatic control paused',
        'sensor_measurement_time_unverified': 'Sensor update time unverified; automatic control paused',
        'controller_not_checked': 'Waiting for the automatic controller to check',
        'controller_overdue': 'Automatic controller has not checked recently',
        'ac_unavailable': 'Cannot reach the AC; automatic control paused',
        'stop_failed': 'Could not confirm the AC stopped; check the unit',
        'writes_locked': 'Automatic commands are locked',
    }.get(issue, 'Automatic room control ready')


def evaluation_issue(config, now=None):
    last = config.get('last_evaluation') or {}
    if last.get('stop_failed'):
        return 'stop_failed'
    if last.get('reason') in {'ac_unavailable', 'writes_locked'}:
        return last['reason']
    # A newer sensor fetch alone does not prove the controller has recovered.
    # Keep the previous pause visible until a successful control evaluation.
    if str(last.get('reason') or '').startswith('sensor_'):
        return last['reason']
    try:
        stamp = datetime.fromisoformat(last['checked_at']).astimezone(timezone.utc)
        age = ((now or datetime.now(timezone.utc)) - stamp).total_seconds()
    except (KeyError, TypeError, ValueError):
        return 'controller_not_checked'
    if not -5 <= age <= 150:
        return 'controller_overdue'
    return None
