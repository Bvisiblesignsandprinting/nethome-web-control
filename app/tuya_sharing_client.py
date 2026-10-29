"""Read-only Tuya account sharing, without an IoT developer project or MQTT."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import math
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import qrcode
import qrcode.image.svg
from cryptography.fernet import Fernet
from tuya_sharing import LoginControl, Manager
from tuya_sharing.customerapi import SharingTokenListener

from .config import settings
from .db import (load_sharing_record, save_sharing_record, sensor_operation_lock,
                 load_sensor_cache, save_sensor_cache, load_smart_control_config)

CLIENT_ID = 'HA_3y9q4ak7g4ephrvke'
INTERVAL = 15
ERROR_BACKOFF = 60
# SDK errors and debugging output may contain tokens/URLs. Never publish them.
logging.getLogger('tuya_sharing').disabled = True


class SharingError(RuntimeError):
    pass


def _cipher():
    if not settings.api_token or settings.api_token == 'change-me-before-remote-access':
        raise SharingError('Secure server configuration is required')
    key = hashlib.sha256(('nethome-tuya-sharing-v1:' + settings.api_token).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def _load(action):
    value = load_sharing_record(action)
    return json.loads(_cipher().decrypt(value.encode())) if value else None


def _save(action, value):
    save_sharing_record(action, _cipher().encrypt(json.dumps(value).encode()).decode())


def configured():
    # Corrupt encrypted state fails closed; it must not silently use the trial.
    return bool(load_sharing_record('session'))


def connection_status():
    session = _load('session')
    return {'connected': bool(session), 'source': 'tuya-sharing',
            'sensor_name': session.get('sensor_name') if session else None,
            'interval_seconds': INTERVAL, 'computer_required': False}


def _login():
    login = LoginControl()
    # Avoid the SDK's 60-second default blocking the shared operation lock.
    original = login.session.request
    def request(*args, **kwargs):
        kwargs['timeout'] = 12
        return original(*args, **kwargs)
    login.session.request = request
    return login


def begin(code):
    with sensor_operation_lock():
        previous = _load('pending')
        now = time.time()
        if previous and now - previous.get('created_at', 0) < 10:
            raise SharingError('Please wait a few seconds before preparing another code')
        response = _login().qr_code(CLIENT_ID, 'haauthorize', code)
        if not response.get('success') or not response.get('result', {}).get('qrcode'):
            raise SharingError('Tuya could not prepare the connection. Check the User Code.')
        token = response['result']['qrcode']
        _save('pending', {'code': code, 'token': token, 'created_at': now, 'last_check': 0})
        buffer = io.BytesIO()
        qrcode.make('tuyaSmart--qrLogin?token=' + token,
                    image_factory=qrcode.image.svg.SvgPathImage).save(buffer)
        return {'qr': 'data:image/svg+xml;base64,' + base64.b64encode(buffer.getvalue()).decode(),
                'expires_in': 180}


class TokenListener(SharingTokenListener):
    def __init__(self, session):
        self.session = session

    def update_token(self, token_info):
        self.session['token_info'] = token_info
        _save('session', self.session)


def _manager(session, *, persist=True):
    endpoint = urlsplit(session['endpoint'])
    host = endpoint.hostname or ''
    if endpoint.scheme != 'https' or endpoint.username or endpoint.password or not any(
        host == domain or host.endswith('.' + domain)
        for domain in ('iotbing.com', 'tuyaus.com', 'tuyaeu.com', 'tuyacn.com', 'tuyain.com', 'tuya.com')
    ):
        raise SharingError('Tuya returned an unsupported service address')
    manager = Manager(CLIENT_ID, session['code'], session['terminal_id'], session['endpoint'],
                      session['token_info'], TokenListener(session) if persist else None)
    original = manager.customer_api.session.request
    def request(*args, **kwargs):
        kwargs['timeout'] = 12
        return original(*args, **kwargs)
    manager.customer_api.session.request = request
    return manager


def _definition(device, code):
    definition = device.status_range.get(code)
    values = getattr(definition, 'values', {})
    return json.loads(values) if isinstance(values, str) else values


def confirm():
    with sensor_operation_lock():
        pending = _load('pending')
        if not pending or time.time() - pending['created_at'] > 180:
            raise SharingError('Connection code expired. Prepare another code.')
        if pending.get('used'):
            return connection_status()
        if time.time() - pending.get('last_check', 0) < 5:
            return {'connected': False, 'pending': True}
        pending['last_check'] = time.time()
        _save('pending', pending)
        success, info = _login().login_result(pending['token'], CLIENT_ID, pending['code'])
        if not success:
            return {'connected': False, 'pending': True}
        session = {'code': pending['code'], 'terminal_id': info['terminal_id'],
                   'endpoint': info['endpoint'], 'token_info': info}
        manager = _manager(session, persist=False)
        manager.update_device_cache()
        candidates = [d for d in manager.device_map.values() if d.category == 'wsdcg'
                      and 'va_temperature' in d.status and 'va_humidity' in d.status]
        if settings.tuya_device_id:
            candidates = [d for d in candidates if d.id == settings.tuya_device_id]
        if len(candidates) != 1:
            raise SharingError('The existing room sensor was not uniquely identified. No device was selected.')
        device = candidates[0]
        session.update(device_id=device.id, sensor_name=device.name,
                       temperature_definition=_definition(device, 'va_temperature'),
                       humidity_definition=_definition(device, 'va_humidity'))
        reading = normalize(device.status, device.online, session)
        _save('session', session)
        _save('pending', {'used': True, 'created_at': pending['created_at']})
        save_sensor_cache({'identity': ['sharing', device.id], 'sensor': reading,
                           'fetched_at': time.time(), 'next_fetch_at': time.time() + INTERVAL})
        return {**connection_status(), 'sensor': reading}


def normalize(status, online, session):
    def scaled(code, definition):
        value = status.get(code)
        scale = definition.get('scale')
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not isinstance(scale, int):
            raise SharingError('Sensor units or values could not be verified')
        result = value / (10 ** scale)
        if not math.isfinite(result):
            raise SharingError('Sensor returned an invalid reading')
        return result
    temp = session['temperature_definition']
    if temp.get('unit') not in ('C', '°C', '℃'):
        raise SharingError('Sensor temperature unit is not Celsius')
    c = scaled('va_temperature', temp)
    humidity = scaled('va_humidity', session['humidity_definition'])
    if not -40 <= c <= 85 or not 0 <= humidity <= 100:
        raise SharingError('Sensor reading is outside its valid range')
    return {'ok': True, 'temperature_c': round(c, 1), 'temperature_f': round(c * 1.8 + 32, 1),
            'humidity': round(humidity), 'battery': status.get('battery_state'),
            'online': online, 'source': 'tuya-sharing', 'updated_at': None,
            'measurement_time_verified': False, 'cached': False,
            'fetched_at': datetime.now(timezone.utc).isoformat(), 'cache_ttl_seconds': INTERVAL}


def indoor_sensor(max_cache_age_seconds=INTERVAL):
    # Shared by browser, panel, schedules and SMS. Off mode makes zero Tuya calls.
    if not load_smart_control_config().get('enabled'):
        raise SharingError('Sensor reads paused while Smart Room is off')
    with sensor_operation_lock():
        session = _load('session')
        if not session:
            raise SharingError('Connect the sensor in Settings')
        identity = ['sharing', session['device_id']]
        cache = load_sensor_cache()
        if cache.get('identity') != identity:
            cache = {'identity': identity}
        now = time.time()
        if cache.get('sensor'):
            # Migrate successful 40-second reservations without clearing error backoff.
            cache['next_fetch_at'] = min(cache.get('next_fetch_at', now),
                                        cache.get('fetched_at', 0) + INTERVAL)
        if cache.get('sensor') and 0 <= now - cache.get('fetched_at', 0) < max(INTERVAL, max_cache_age_seconds):
            return {**cache['sensor'], 'cached': True,
                    'cache_age_seconds': int(now - cache['fetched_at'])}
        if now < cache.get('next_fetch_at', 0):
            raise SharingError('Sensor refresh is temporarily unavailable; retry shortly')
        cache.update(next_fetch_at=now + INTERVAL)
        save_sensor_cache(cache)
        try:
            manager = _manager(session)
            response = manager.customer_api.get('/v1.0/m/life/ha/devices/detail', {'devIds': session['device_id']})
            if not response.get('success'):
                raise SharingError('Tuya could not refresh the sensor; reconnect in Settings if this continues')
            devices = [d for d in response.get('result', []) if d.get('id') == session['device_id']]
            if len(devices) != 1:
                raise SharingError('Room sensor was not returned by Tuya')
            device = devices[0]
            status = {v['code']: v['value'] for v in device.get('status', []) if 'code' in v and 'value' in v}
            reading = normalize(status, device.get('online'), session)
            cache.update(sensor=reading, fetched_at=time.time(), next_fetch_at=time.time() + INTERVAL)
            save_sensor_cache(cache)
            return reading
        except SharingError:
            cache.pop('sensor', None)
            cache['next_fetch_at'] = time.time() + ERROR_BACKOFF
            save_sensor_cache(cache)
            raise
        except Exception:
            cache.pop('sensor', None)
            cache['next_fetch_at'] = time.time() + ERROR_BACKOFF
            save_sensor_cache(cache)
            raise SharingError('Sensor refresh failed; no stale reading was used') from None
