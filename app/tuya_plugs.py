"""Explicit on/off control of three user-assigned Tuya outlets; no MQTT or scans on polls."""
from __future__ import annotations

import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from . import tuya_sharing_client as sharing
from .db import plug_operation_lock

ROLES = ('roof', 'walls', 'utility')
LABELS = ('Roof lights', 'Wall lights', 'Waterfall / Jumping Balloon')
TTL = 60


def fail(message, code=409):
    raise HTTPException(code, message)


def config():
    return sharing._load('plug-config') or {'revision': 0, 'bindings': {}}


def manager():
    session = sharing._load('session')
    if not session:
        fail('Connect your Tuya account using Indoor sensor setup first.')
    return sharing._manager(session)


def candidate(device):
    # Only a single relay outlet is eligible. Never treat the AC/sensor or a
    # multi-relay controller as a plug just because it exposes a switch.
    functions = getattr(device, 'function', {})
    codes = [code for code, spec in functions.items()
             if code.startswith('switch') and getattr(spec, 'type', '').lower() == 'boolean']
    if device.category not in ('cz', 'pc') or len(codes) != 1 or codes[0] not in ('switch', 'switch_1'):
        return None
    return {'id': str(device.id), 'name': str(device.name)[:80], 'code': codes[0],
            'online': device.online is True}


def discover():
    with plug_operation_lock():
        old = sharing._load('plug-discovery') or {}
        if time.time() - old.get('at', 0) < 30:
            return {'devices': old['devices'], 'cached': True}
        try:
            client = manager()
            client.update_device_cache()
            devices = [row for device in client.device_map.values() if (row := candidate(device))]
        except HTTPException:
            raise
        except Exception:
            fail('Tuya scan failed. Check your Tuya account connection and try again.', 503)
        devices = sorted(devices, key=lambda d: (d['name'], d['id']))[:50]
        sharing._save('plug-discovery', {'at': time.time(), 'devices': devices})
        return {'devices': devices, 'cached': False}


def assign(revision, selected):
    with plug_operation_lock():
        old = config()
        if old['revision'] != revision:
            fail('Plug assignments changed. Refresh before saving.')
        ids = [v for v in selected.values() if v]
        if len(ids) != len(set(ids)):
            fail('Choose a different physical plug for each role.', 422)
        bindings = {}
        if ids:
            # Query only selected IDs, revalidating capabilities without switching power.
            try:
                devices = manager().device_repository.query_devices_by_ids(ids)
                found = {str(d.id): candidate(d) for d in devices}
            except HTTPException:
                raise
            except Exception:
                fail('Could not verify those plugs with Tuya. Nothing was assigned.', 503)
            for role, device_id in selected.items():
                if not device_id:
                    continue
                row = found.get(device_id)
                if not row:
                    fail('A selected device is not a supported single-switch outlet.', 422)
                bindings[role] = row
        sharing._save('plug-config', {'revision': revision + 1, 'bindings': bindings})
        sharing._save('plug-cache', {})
        return _snapshot(config(), {}, False)


def readings(client, bindings):
    ids = [row['id'] for row in bindings.values()]
    result = client.customer_api.get('/v1.0/m/life/ha/devices/detail', {'devIds': ','.join(ids)})
    if not result.get('success') or not isinstance(result.get('result'), list):
        raise ValueError('Invalid Tuya status')
    found = {str(d.get('id')): d for d in result['result']}
    states = {}
    for role, row in bindings.items():
        d = found.get(row['id'], {})
        values = {item.get('code'): item.get('value') for item in d.get('status', [])}
        value = values.get(row['code'])
        states[role] = {'online': d.get('online') is True,
                        'power': value if type(value) is bool and d.get('online') is True else None}
    return states


def _snapshot(saved, cache, cached):
    fresh = time.time() - cache.get('at', 0) < TTL
    return {'revision': saved['revision'], 'account_connected': sharing.configured(),
            'cache_seconds': TTL, 'cached': cached, 'fresh': fresh,
            'checked_at': cache.get('at'),
            'roles': [{'role': role, 'label': label,
                       'device_id': saved['bindings'].get(role, {}).get('id', ''),
                       'name': saved['bindings'].get(role, {}).get('name', ''),
                       'assigned': role in saved['bindings'],
                       'online': bool(fresh and cache.get('states', {}).get(role, {}).get('online')),
                       'power': cache.get('states', {}).get(role, {}).get('power') if fresh else None}
                      for role, label in zip(ROLES, LABELS)]}


def status():
    with plug_operation_lock():
        saved = config()
        cache = sharing._load('plug-cache') or {}
        if not saved['bindings'] or time.time() - cache.get('attempt_at', 0) < TTL:
            return _snapshot(saved, cache, True)
        now = time.time()
        try:
            states = readings(manager(), saved['bindings'])
            cache = {'at': now, 'attempt_at': now, 'states': states}
        except Exception:
            # Failure backs off globally and never presents old state as current.
            cache = {'attempt_at': now}
        sharing._save('plug-cache', cache)
        return _snapshot(saved, cache, False)


def command(target, power, revision):
    with plug_operation_lock():
        saved = config()
        if saved['revision'] != revision:
            fail('Plug assignments changed. Refresh before controlling power.')
        roles = ('roof', 'walls') if target == 'lights' else (target,)
        if any(role not in saved['bindings'] for role in roles):
            fail('Assign the required plugs in Plug connection first.')
        selected = {role: saved['bindings'][role] for role in roles}
        try:
            client = manager()
            states = readings(client, selected)
        except HTTPException:
            raise
        except Exception:
            fail('Could not check the plugs. No power command was sent.', 503)
        if any(not s['online'] or s['power'] is None for s in states.values()):
            fail('A required plug is offline or unavailable. No power command was sent.')
        results = []
        # Never retry uncertain writes or silently undo a successful first plug.
        for role, row in selected.items():
            try:
                reply = client.customer_api.post('/v1.1/m/thing/' + row['id'] + '/commands', None,
                                                  {'commands': [{'code': row['code'], 'value': power}]})
                accepted = reply.get('success') is True
            except Exception:
                accepted = False
            results.append({'role': role, 'accepted': accepted})
            if not accepted:
                break
        try:
            after = readings(client, saved['bindings'])
            cache = {'at': time.time(), 'attempt_at': time.time(), 'states': after}
        except Exception:
            after, cache = {}, {}
        sharing._save('plug-cache', cache)
        confirmed = len(results) == len(roles) and all(r['accepted'] and
                    after.get(r['role'], {}).get('power') is power for r in results)
        return {'ok': confirmed, 'results': results, 'state': _snapshot(saved, cache, False),
                'message': 'Power confirmed' if confirmed else
                'Power not fully confirmed. Check each plug before trying again.'}


class Bindings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: StrictInt = Field(ge=0)
    roof: str = Field(default='', max_length=64, pattern=r'^[a-zA-Z0-9_-]*$')
    walls: str = Field(default='', max_length=64, pattern=r'^[a-zA-Z0-9_-]*$')
    utility: str = Field(default='', max_length=64, pattern=r'^[a-zA-Z0-9_-]*$')


class Power(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: StrictInt = Field(ge=0)
    target: Literal['roof', 'walls', 'utility', 'lights']
    power: StrictBool


def router(auth):
    api = APIRouter(prefix='/api/plugs', dependencies=[Depends(auth)])

    def same_origin(request):
        origin = request.headers.get('origin')
        if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
            fail('Open Plug connection on NetHome.', 403)

    @api.get('')
    def get_status(response: Response):
        response.headers['Cache-Control'] = 'no-store'
        return status()

    @api.post('/discover')
    def scan(request: Request):
        same_origin(request)
        return discover()

    @api.put('/bindings')
    def bind(body: Bindings, request: Request):
        same_origin(request)
        return assign(body.revision, body.model_dump(exclude={'revision'}))

    @api.post('/command')
    def power(body: Power, request: Request):
        same_origin(request)
        return command(body.target, body.power, body.revision)

    return api
