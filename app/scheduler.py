from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from time import monotonic
import json
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import settings
from .db import (
    add_activity,
    claim_due_queued_execution,
    claim_schedule_execution,
    enqueue_device_command_at,
    finish_schedule_execution,
    list_schedules,
    load_smart_control_config,
    save_smart_control_config,
    save_device_state,
    update_schedule,
)
from .midea_client import midea
from .tuya_client import TuyaCloudError, tuya
from .weather import current_weather
from .sensor_health import sensor_control_issue
from .climate_history import observe_climate
from .db import smart_operation_lock

DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _as_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return date.fromisoformat(str(value)[:10])


def _as_time(value: Any) -> time:
    if isinstance(value, time):
        return value.replace(tzinfo=None)
    return time.fromisoformat(str(value))


def _schedule_due_at(schedule: dict[str, Any], now_utc: datetime) -> datetime | None:
    if not bool(schedule.get("enabled")):
        return None

    tz_name = schedule.get("timezone") or "America/New_York"
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("America/New_York")

    local_now = now_utc.astimezone(tz)
    local_date = local_now.date()
    run_time = _as_time(schedule["time_local"])

    # The Vercel/Supabase runner calls this once per minute.
    if (local_now.hour, local_now.minute) != (run_time.hour, run_time.minute):
        return None

    start_date = _as_date(schedule.get("start_date"))
    end_date = _as_date(schedule.get("end_date"))
    if start_date and local_date < start_date:
        return None
    if end_date and local_date > end_date:
        return None

    schedule_type = schedule.get("schedule_type") or "weekly"
    weekday = DAY_NAMES[local_now.weekday()]

    if schedule_type == "one_time":
        if start_date != local_date:
            return None
    elif schedule_type == "weekdays":
        if local_now.weekday() > 4:
            return None
    elif schedule_type == "daily":
        pass
    elif schedule_type == "weekly":
        selected = {
            day.strip()
            for day in str(schedule.get("days") or "").split(",")
            if day.strip()
        }
        if weekday not in selected:
            return None
    else:
        return None

    local_scheduled = datetime.combine(local_date, run_time, tzinfo=tz)
    return local_scheduled.astimezone(timezone.utc).replace(second=0, microsecond=0)


def _command_from_schedule(schedule: dict[str, Any]) -> dict[str, Any]:
    action = str(schedule.get("action") or "set").lower()
    command: dict[str, Any] = {"action": action}
    if schedule.get("mode"):
        command["mode"] = str(schedule["mode"])
    if schedule.get("temperature") is not None:
        command["temperature"] = float(schedule["temperature"])
    if schedule.get("fan"):
        command["fan"] = str(schedule["fan"])
    return command


def _adaptive_fan(abs_error_f: float) -> str:
    if abs_error_f >= 12.0:
        return "high"
    if abs_error_f >= 6.0:
        return "medium"
    return "low"


def _outside_is_mild(outside_f: float | None, target_f: float) -> bool:
    return outside_f is not None and abs(float(outside_f) - target_f) <= 6.0


def _outside_temperature(config: dict[str, Any], now_utc: datetime) -> tuple[float | None, dict[str, Any]]:
    cached = config.get("outside_temperature_f")
    checked_at = config.get("outside_checked_at")
    if cached is not None and checked_at:
        try:
            checked = datetime.fromisoformat(str(checked_at).replace("Z", "+00:00")).astimezone(timezone.utc)
            if now_utc - checked < timedelta(minutes=15):
                return float(cached), config
        except Exception:
            pass

    try:
        weather = current_weather()
        outside_f = weather.get("temperature_f")
        if outside_f is not None:
            saved = save_smart_control_config({
                "outside_temperature_f": float(outside_f),
                "outside_checked_at": now_utc.isoformat(),
            })
            return float(outside_f), saved
    except Exception as exc:
        add_activity("smart-control", "outside_weather", "warning", str(exc)[:300])

    return (float(cached) if cached is not None else None), config


def run_smart_control(now_utc: datetime | None = None) -> dict[str, Any]:
    started = monotonic()
    now = now_utc or datetime.now(timezone.utc)
    with smart_operation_lock():
        config = load_smart_control_config()
        previous = config.get('last_evaluation') or {}
        try:
            age = (now - datetime.fromisoformat(previous['checked_at'])).total_seconds()
        except (KeyError, ValueError, TypeError):
            age = 999
        # Cron and the panel may wake together; make just one decision per cycle.
        if config.get('enabled') and 0 <= age < 25:
            return previous
        try:
            result = _run_smart_control(now + timedelta(seconds=monotonic() - started))
        except Exception:
            result = {'enabled': bool(config.get('enabled')), 'checked_at': now.isoformat(),
                      'action': 'error', 'reason': 'ac_unavailable'}
            add_activity('smart-control', 'controller', 'error', 'Automatic adjustment could not be confirmed')
        if result.get('enabled'):
            save_smart_control_config({'last_evaluation': result})
        return result


def _sensor_pause(result, reason, now):
    """Do not leave a previously running heater/cooler unattended on sensor loss."""
    result.update(action='skipped', reason=reason)
    try:
        state = midea.status()
        if state.get('running') and int(state.get('mode') or 0) in {1, 2, 3, 4}:
            stopped = midea.command({'action': 'off'})
            save_device_state(stopped, True, source='smart-control')
            save_smart_control_config({'last_command_at': now.isoformat(), 'comfort_since': None})
            add_activity('smart-control', 'sensor_pause_off', 'success', reason)
            result.update(action='off', verified=bool(stopped.get('verified')))
    except Exception:
        result['stop_failed'] = True
        add_activity('smart-control', 'sensor_pause_off', 'error', 'Could not confirm AC stopped: ' + reason)
    return result


def _coast(config, state, result, now, reason):
    """Mix room air on low fan for at most seven minutes, then rest."""
    since = config.get('circulation_since')
    try:
        expired = since and (now - datetime.fromisoformat(since)).total_seconds() >= 420
    except (TypeError, ValueError):
        expired = False
    mode = int(state.get('mode') or 0)
    running = bool(state.get('running'))
    if expired and (not running or mode == 5):
        if running:
            stopped = midea.command({'action':'off'})
            save_device_state(stopped, True, source='smart-control')
            save_smart_control_config({'last_command_at':now.isoformat()})
            add_activity('smart-control', 'coast_off', 'success', reason)
            result.update(action='off', reason=reason, verified=bool(stopped.get('verified')))
        else:
            result.update(action='hold', reason=reason)
        return result
    if running and mode == 5 and int(state.get('fan_speed') or 0) <= 40:
        if not since:
            save_smart_control_config({'circulation_since':now.isoformat()})
        result.update(action='hold', reason=reason)
        return result
    mixed = midea.command({'action':'set', 'mode':'fan', 'fan':'low', 'running':True})
    save_device_state(mixed, True, source='smart-control')
    patch = {'last_command_at':now.isoformat(), 'circulation_since':now.isoformat(), 'last_coast_reason':reason}
    if mode in {2,4}:
        patch['last_conditioning_mode'] = {2:'cool',4:'heat'}[mode]
    save_smart_control_config(patch)
    add_activity('smart-control', 'circulate', 'success', reason)
    result.update(action='circulate', reason=reason, midea_mode='fan', fan='low', verified=bool(mixed.get('verified')))
    return result


def _run_smart_control(now_utc: datetime | None = None) -> dict[str, Any]:
    started = monotonic()
    now_utc = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    config = load_smart_control_config()
    result: dict[str, Any] = {
        "enabled": bool(config.get("enabled")),
        "checked_at": now_utc.isoformat(),
        "action": "none",
    }
    if not result["enabled"]:
        return result
    if not settings.allow_writes:
        result["action"] = "skipped"
        result["reason"] = "writes_locked"
        return result

    try:
        sensor = tuya.indoor_sensor(max_cache_age_seconds=30)
    except TuyaCloudError as exc:
        add_activity("smart-control", "sensor", "error", str(exc)[:500])
        return _sensor_pause(result, 'sensor_unavailable', now_utc)

    # The network response may arrive seconds AFTER the cycle began. Compare
    # its timestamps to the time after I/O, not to the earlier cycle-start time.
    now_utc += timedelta(seconds=monotonic() - started)
    result['checked_at'] = now_utc.isoformat()
    issue = sensor_control_issue(sensor, now_utc, config=config)
    stable_snapshot = issue is None and sensor_control_issue(sensor, now_utc) == 'sensor_not_changing'
    if issue:
        return _sensor_pause(result, issue, now_utc)

    room_f = float(sensor["temperature_f"]) + float(config.get("sensor_calibration_f") or 0)
    target_f = float(config.get("target_temperature_f") or 73)
    deadband_f = max(0.5, float(config.get("deadband_f") or 1.0))
    preferred_mode = str(config.get("preferred_mode") or "auto").lower()
    if preferred_mode not in {"auto", "cool", "heat"}:
        preferred_mode = "auto"

    outside_f, config = _outside_temperature(config, now_utc)
    error_f = target_f - room_f
    abs_error_f = abs(error_f)
    result.update({
        "room_temperature_f": round(room_f, 1),
        "target_temperature_f": round(target_f, 1),
        "preferred_mode": preferred_mode,
        "outside_temperature_f": outside_f,
    })

    try:
        state = midea.status()
    except Exception as exc:
        add_activity("smart-control", "status", "error", str(exc)[:500])
        result["action"] = "skipped"
        result["reason"] = "ac_unavailable"
        return result

    current_mode = int(state.get("mode") or 0)
    running = bool(state.get("running"))
    climate = observe_climate(config, room_f, outside_f, state, now_utc)
    save_smart_control_config({'temperature_history':climate['history']})
    result.update({k:v for k,v in climate.items() if k != 'history'})
    projected = climate['projected_room_f']
    # The last live cycle gained another 1.8F after heating stopped. Reserve
    # a bounded 1.5F for residual warmth, only with a measured warming trend
    # and within 2F of target. This is an allowance, not a physical forecast.
    residual_allowance = 1.5 if current_mode == 4 and climate['moving_toward_target'] and abs_error_f <= 2 else 0
    result['residual_heat_allowance_f'] = residual_allowance
    if stable_snapshot:
        result['stable_snapshot'] = True
        if running:
            return _coast(config, state, result, now_utc, 'stable_room')
        result.update(action='hold', reason='stable_room')
        return result
    effective_error = abs_error_f
    if climate['moving_toward_target'] and projected is not None:
        effective_error = min(abs_error_f, abs(target_f-projected))
    last_command_at = config.get("last_command_at")
    min_minutes = max(1, int(config.get("min_command_interval_minutes") or 5))
    last_dt = None
    if last_command_at:
        try:
            last_dt = datetime.fromisoformat(str(last_command_at).replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            last_dt = None

    # Stop heat on overshoot (or cooling on undershoot) immediately. The
    # adjustment interval protects restarts/reversals, never delays stopping.
    wrong_direction = running and ((current_mode == 4 and error_f <= 0) or (current_mode == 2 and error_f >= 0))
    if wrong_direction and abs_error_f >= 3.0:
        stopped = midea.command({'action': 'off'})
        save_device_state(stopped, True, source='smart-control')
        save_smart_control_config({'last_command_at': now_utc.isoformat(), 'comfort_since': None, 'last_conditioning_mode': {2:'cool',4:'heat'}.get(current_mode)})
        add_activity('smart-control', 'target_stop', 'success', f'room={room_f:.1f} target={target_f:.1f}')
        result.update(action='off', reason='target_reached', verified=bool(stopped.get('verified')))
        return result

    # Cut conditioning before overshoot when several observations show that
    # residual heat/cold is already carrying the room to its target.
    approaching = running and ((current_mode == 4 and error_f > 0 and projected is not None and projected + residual_allowance >= target_f)
                              or (current_mode == 2 and error_f < 0 and projected is not None and projected <= target_f))
    if approaching and climate['moving_toward_target'] and abs_error_f <= 3:
        return _coast(config, state, result, now_utc, 'anticipating_target')
    # Give an early cutoff its full mixing period before deciding to reheat.
    # Otherwise a rounded reading just below the band can restart the heater
    # before the residual warmth has reached the room sensor.
    if running and current_mode == 5 and config.get('last_coast_reason') == 'anticipating_target' and abs_error_f <= 3:
        return _coast(config, state, result, now_utc, 'anticipating_target')
    # Weather alone never proves that a room is recovering. Require measured
    # progress as well, and only coast near target rather than defer large errors.
    if not (running and current_mode in {2,4}) and climate['outside_helpful'] and climate['moving_toward_target'] and abs_error_f <= 3:
        return _coast(config, state, result, now_utc, 'weather_assisted_settling')

    # Stop active conditioning on entering the comfort band, then circulate
    # quietly. Do not keep adding heat/cold for a three-minute settling wait.
    if abs_error_f <= deadband_f or (wrong_direction and abs_error_f < 3.0):
        save_smart_control_config({'opposite_demand_since': None, 'opposite_demand_mode': None})
        return _coast(config, state, result, now_utc, 'comfort_circulation')

    candidate = 'heat' if error_f > 0 else 'cool'
    previous_mode = config.get('last_conditioning_mode') or {2:'cool',4:'heat'}.get(current_mode)
    if preferred_mode == 'auto' and previous_mode and candidate != previous_mode and abs_error_f < 6.0:
        # Small overshoots get air mixing and time to settle, not an immediate
        # heat/cool reversal. A 3-6F opposite demand must persist ten minutes.
        since = config.get('opposite_demand_since')
        if abs_error_f < 3.0:
            save_smart_control_config({'opposite_demand_since': None, 'opposite_demand_mode': None})
            return _coast(config, state, result, now_utc, 'settling_before_reversal')
        if config.get('opposite_demand_mode') != candidate or not since:
            save_smart_control_config({'opposite_demand_since': now_utc.isoformat(), 'opposite_demand_mode': candidate})
            return _coast(config, state, result, now_utc, 'confirming_opposite_demand')
        try:
            confirmed = (now_utc - datetime.fromisoformat(since)).total_seconds() >= 600
        except (ValueError, TypeError):
            confirmed = False
        if not confirmed:
            return _coast(config, state, result, now_utc, 'confirming_opposite_demand')
    elif config.get('opposite_demand_since'):
        save_smart_control_config({'opposite_demand_since': None, 'opposite_demand_mode': None})

    if config.get("comfort_since") is not None:
        config = save_smart_control_config({"comfort_since": None})

    if last_dt and now_utc - last_dt < timedelta(minutes=min_minutes):
        # Taper airflow promptly even while a setpoint/restart change is held.
        # This only lowers the fan; it does not restart or reverse conditioning.
        softer_fan = _adaptive_fan(effective_error)
        softer_speed = {'low':40, 'medium':60, 'high':100}[softer_fan]
        if running and current_mode in {2,4} and int(state.get('fan_speed') or 0) > softer_speed:
            tapered = midea.command({'action':'set', 'fan':softer_fan})
            save_device_state(tapered, True, source='smart-control')
            add_activity('smart-control', 'fan_taper', 'success', f'room={room_f:.1f} target={target_f:.1f} fan={softer_fan}')
            result.update(action='fan_taper', fan=softer_fan, verified=bool(tapered.get('verified')))
            return result
        result["action"] = "hold"
        result["reason"] = "minimum_interval"
        return result

    desired_mode = preferred_mode
    if preferred_mode == "auto":
        desired_mode = "heat" if error_f > 0 else "cool"
    elif preferred_mode == "cool" and error_f > 0:
        if running:
            command_result = midea.command({"action": "off"})
            save_device_state(command_result, True, source="smart-control")
            saved = save_smart_control_config({"last_command_at": now_utc.isoformat()})
            result.update({"action": "off", "reason": "cool_not_needed", "last_command_at": saved.get("last_command_at")})
        else:
            result.update({"action": "hold", "reason": "cool_not_needed"})
        return result
    elif preferred_mode == "heat" and error_f < 0:
        if running:
            command_result = midea.command({"action": "off"})
            save_device_state(command_result, True, source="smart-control")
            saved = save_smart_control_config({"last_command_at": now_utc.isoformat()})
            result.update({"action": "off", "reason": "heat_not_needed", "last_command_at": saved.get("last_command_at")})
        else:
            result.update({"action": "hold", "reason": "heat_not_needed"})
        return result

    desired_mode_code = {"cool": 2, "heat": 4}.get(desired_mode, current_mode)

    # Avoid reversing an actively running compressor for a tiny overshoot.
    if running and current_mode in {2, 4} and current_mode != desired_mode_code and abs_error_f < 2.0:
        result["action"] = "hold"
        result["reason"] = "mode_switch_guard"
        return result

    current_c = state.get("target_temperature_c")
    current_set_f = (float(current_c) * 9.0 / 5.0 + 32.0) if current_c is not None else target_f

    # Strong unit: use the fan for most of the response shaping and taper both
    # airflow and setpoint correction as the room approaches target.
    fan = _adaptive_fan(effective_error)
    correction_limit = 2.0 if effective_error >= 12.0 else 1.5 if effective_error >= 6.0 else 1.0
    correction = max(-correction_limit, min(correction_limit, error_f))
    # Anchor to the room target, never integrate a correction into yesterday's
    # setpoint. Otherwise heat can ratchet to 86F and carry that into cooling.
    next_set_f = max(60.0, min(86.0, round((target_f + correction) * 2) / 2))

    command = {
        "action": "set",
        "temperature": next_set_f,
        "mode": desired_mode,
        "fan": fan,
        "running": True,
    }

    current_fan = int(state.get("fan_speed") or 0)
    fan_code = {"low": 40, "medium": 60, "high": 100}[fan]
    if (
        abs(next_set_f - current_set_f) < 0.5
        and current_mode == desired_mode_code
        and abs(current_fan - fan_code) <= 5
        and running
    ):
        result["action"] = "hold"
        return result

    command_result = midea.command(command)
    save_device_state(command_result, True, source="smart-control")
    saved = save_smart_control_config({"last_command_at": now_utc.isoformat(), "last_conditioning_mode": desired_mode, "circulation_since": None, "opposite_demand_since": None, "opposite_demand_mode": None})
    add_activity(
        "smart-control",
        "adjust",
        "success",
        f"room={room_f:.1f} target={target_f:.1f} outside={outside_f} ac_set={current_set_f:.1f}->{next_set_f:.1f} fan={fan}",
    )
    result.update({
        "action": "adjusted",
        "midea_setpoint_f": next_set_f,
        "midea_mode": desired_mode,
        "fan": fan,
        "verified": bool(command_result.get("verified")),
        "last_command_at": saved.get("last_command_at"),
    })
    return result

def run_due_schedules(now_utc: datetime | None = None) -> dict[str, Any]:
    started = monotonic()
    now_utc = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    schedules = list_schedules()

    summary = {
        "checked_at": now_utc.isoformat(),
        "checked": len(schedules),
        "due": 0,
        "claimed": 0,
        "success": 0,
        "failed": 0,
        "skipped": 0,
        "duplicate": 0,
        "executions": [],
    }

    for schedule in schedules:
        scheduled_for = _schedule_due_at(schedule, now_utc)
        if scheduled_for is None:
            continue

        summary["due"] += 1
        command = _command_from_schedule(schedule)
        execution = claim_schedule_execution(
            int(schedule["id"]),
            scheduled_for,
            command,
        )
        if execution is None:
            summary["duplicate"] += 1
            continue

        summary["claimed"] += 1
        execution_id = int(execution["id"])
        item = {
            "execution_id": execution_id,
            "schedule_id": int(schedule["id"]),
            "name": schedule.get("name"),
            "scheduled_for": scheduled_for.isoformat(),
            "command": command,
        }

        if not settings.allow_writes:
            reason = "AC writes are locked"
            finish_schedule_execution(
                execution_id,
                "skipped",
                result={"reason": "writes_locked"},
                error=reason,
            )
            add_activity(
                "scheduler",
                "schedule_execute",
                "skipped",
                f"schedule_id={schedule['id']} reason=writes_locked",
            )
            item["status"] = "skipped"
            item["reason"] = "writes_locked"
            summary["skipped"] += 1
            summary["executions"].append(item)
            continue

        try:
            smart_config = load_smart_control_config()
            smart_target_updated = False
            cloud_command = dict(command)

            # With Smart Room Control enabled, a schedule temperature is a ROOM
            # target, not a raw Midea setpoint. Mode/fan still go to the AC.
            if (
                bool(smart_config.get("enabled"))
                and str(schedule.get("action") or "set").lower() == "set"
                and schedule.get("temperature") is not None
            ):
                # Only convert the schedule temperature into a room target when
                # the external sensor is currently trustworthy. If Tuya is
                # offline/stale, fall back to the normal Midea schedule
                # temperature instead of acting on old room data.
                sensor_ready = False
                try:
                    sensor = tuya.indoor_sensor(max_cache_age_seconds=30) if tuya.configured else None
                    check_time = now_utc + timedelta(seconds=monotonic() - started)
                    if sensor and sensor_control_issue(sensor, check_time) is None:
                        updated_at = sensor.get("updated_at")
                        if updated_at:
                            sensor_time = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
                            if sensor_time.tzinfo is None:
                                sensor_time = sensor_time.replace(tzinfo=timezone.utc)
                            sensor_ready = (now_utc - sensor_time.astimezone(timezone.utc)) <= timedelta(minutes=10)
                        else:
                            sensor_ready = True
                except Exception:
                    sensor_ready = False

                if sensor_ready:
                    target_temperature = float(schedule["temperature"])
                    smart_patch = {
                        "target_temperature_f": target_temperature,
                        "last_command_at": None,
                        "comfort_since": None,
                    }
                    preset_candidates = (
                        ("sudah", "preset_sudah_f", 74.0),
                        ("all_day", "preset_all_day_f", 73.0),
                        ("sleeping", "preset_sleeping_f", 72.0),
                    )
                    for preset_name, preset_key, default_value in preset_candidates:
                        preset_value = float(smart_config.get(preset_key) or default_value)
                        if abs(target_temperature - preset_value) < 0.01:
                            smart_patch["active_preset"] = preset_name
                            break
                    save_smart_control_config(smart_patch)
                    smart_target_updated = True
                    cloud_command.pop("temperature", None)

            # Do not send an empty SET just to persist a smart target.
            actual_fields = {k: v for k, v in cloud_command.items() if k != "action" and v is not None}
            if cloud_command.get("action") == "set" and not actual_fields:
                result = {
                    "verified": True,
                    "smart_target_updated": smart_target_updated,
                    "target_temperature_f": float(schedule["temperature"]),
                }
            else:
                result = midea.command(cloud_command)
                save_device_state(result, True, source="cloud")
                if smart_target_updated:
                    result = dict(result)
                    result["smart_target_updated"] = True
                    result["target_temperature_f"] = float(schedule["temperature"])

            finish_schedule_execution(execution_id, "success", result=result)
            add_activity(
                "scheduler",
                "schedule_execute",
                "success",
                f"schedule_id={schedule['id']} verified={result.get('verified', False)} smart_target={smart_target_updated}",
            )

            if (schedule.get("schedule_type") or "weekly") == "one_time":
                update_schedule(int(schedule["id"]), {"enabled": False})

            item["status"] = "success"
            item["result"] = result
            summary["success"] += 1
        except Exception as exc:
            finish_schedule_execution(execution_id, "failed", error=str(exc))
            add_activity(
                "scheduler",
                "schedule_execute",
                "error",
                f"schedule_id={schedule['id']} error={exc}",
            )
            item["status"] = "failed"
            item["error"] = str(exc)
            summary["failed"] += 1

        summary["executions"].append(item)

    # Run ad-hoc SMS retry commands that became due. These use the existing
    # schedule_executions table but are not visible as user schedules.
    for _ in range(20):
        queued = claim_due_queued_execution(now_utc)
        if queued is None:
            break

        summary["due"] += 1
        summary["claimed"] += 1
        execution_id = int(queued["id"])
        raw_command = queued.get("command") or {}
        if isinstance(raw_command, str):
            try:
                raw_command = json.loads(raw_command)
            except Exception:
                raw_command = {}
        command = dict(raw_command) if isinstance(raw_command, dict) else {}
        retry_count = int(command.pop("_retry_count", 0) or 0)
        retry_max = int(command.pop("_retry_max", 12) or 12)
        retry_label = str(command.pop("_retry_label", "SMS command"))
        item = {
            "execution_id": execution_id,
            "schedule_id": None,
            "name": retry_label,
            "scheduled_for": str(queued.get("scheduled_for") or ""),
            "command": command,
        }

        if not settings.allow_writes:
            finish_schedule_execution(
                execution_id,
                "skipped",
                result={"reason": "writes_locked"},
                error="AC writes are locked",
            )
            item["status"] = "skipped"
            item["reason"] = "writes_locked"
            summary["skipped"] += 1
            summary["executions"].append(item)
            continue

        try:
            result = midea.command(command)
            save_device_state(result, True, source="cloud")
            finish_schedule_execution(execution_id, "success", result=result)
            add_activity(
                "scheduler",
                "sms_retry_execute",
                "success",
                f"execution_id={execution_id} retry={retry_count} verified={result.get('verified', False)}",
            )
            item["status"] = "success"
            item["result"] = result
            summary["success"] += 1
        except Exception as exc:
            text = str(exc).lower()
            offline = "3123" in text or "offline" in text
            finish_schedule_execution(execution_id, "failed", error=str(exc))
            item["status"] = "failed"
            item["error"] = str(exc)
            summary["failed"] += 1

            if offline and retry_count < retry_max:
                next_command = {
                    **command,
                    "_retry_count": retry_count + 1,
                    "_retry_max": retry_max,
                    "_retry_label": retry_label,
                }
                next_at = now_utc + timedelta(minutes=5)
                enqueue_device_command_at("sms-retry", next_command, next_at)
                item["retry_scheduled_for"] = next_at.isoformat()
                add_activity(
                    "scheduler",
                    "sms_retry_execute",
                    "retry",
                    f"execution_id={execution_id} next={next_at.isoformat()} retry={retry_count + 1}/{retry_max}",
                )
            else:
                add_activity(
                    "scheduler",
                    "sms_retry_execute",
                    "error",
                    f"execution_id={execution_id} retry={retry_count}/{retry_max} error={exc}",
                )

        summary["executions"].append(item)

    # The existing always-on runner keeps the sensor available even with no open browser.
    from . import tuya_sharing_client as sharing
    if not load_smart_control_config().get('enabled') and sharing.configured():
        try:
            summary['sensor_monitor'] = tuya.indoor_sensor()
        except TuyaCloudError:
            summary['sensor_monitor'] = {'ok': False, 'error': 'Sensor refresh unavailable'}
    summary["smart_control"] = run_smart_control(now_utc + timedelta(seconds=monotonic() - started))
    return summary
