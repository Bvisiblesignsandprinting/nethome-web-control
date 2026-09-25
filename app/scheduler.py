from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
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
    if abs_error_f >= 6.0:
        return "high"
    if abs_error_f >= 3.0:
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
        sensor = tuya.indoor_sensor(max_cache_age_seconds=20)
    except TuyaCloudError as exc:
        add_activity("smart-control", "sensor", "error", str(exc)[:500])
        result["action"] = "skipped"
        result["reason"] = "sensor_unavailable"
        return result

    if sensor.get("online") is False:
        result["action"] = "skipped"
        result["reason"] = "sensor_offline"
        return result

    updated_at = sensor.get("updated_at")
    if updated_at:
        try:
            sensor_time = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00")).astimezone(timezone.utc)
            if now_utc - sensor_time > timedelta(minutes=10):
                result["action"] = "skipped"
                result["reason"] = "sensor_stale"
                return result
        except Exception:
            pass

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
    last_command_at = config.get("last_command_at")
    min_minutes = max(1, int(config.get("min_command_interval_minutes") or 5))
    last_dt = None
    if last_command_at:
        try:
            last_dt = datetime.fromisoformat(str(last_command_at).replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            last_dt = None

    # Comfort zone: wait for a stable reading before stepping down from
    # compressor operation to circulation or fully off.
    if abs_error_f <= deadband_f:
        comfort_since = config.get("comfort_since")
        if not comfort_since:
            saved = save_smart_control_config({"comfort_since": now_utc.isoformat()})
            result["action"] = "hold"
            result["reason"] = "comfort_stabilizing"
            result["comfort_since"] = saved.get("comfort_since")
            return result

        try:
            comfort_dt = datetime.fromisoformat(str(comfort_since).replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            comfort_dt = now_utc

        stable_minutes = (now_utc - comfort_dt).total_seconds() / 60.0
        result["comfort_stable_minutes"] = round(stable_minutes, 1)
        mild_outside = _outside_is_mild(outside_f, target_f)

        if stable_minutes < 3.0:
            result["action"] = "hold"
            result["reason"] = "comfort_stabilizing"
            return result

        # If conditions are mild, use a short low-fan circulation stage before
        # shutting down. Fan mode recirculates room air; it does not introduce
        # outside air.
        if mild_outside and current_mode != 5:
            command = {"action": "set", "mode": "fan", "fan": "low", "running": True}
            command_result = midea.command(command)
            save_device_state(command_result, True, source="smart-control")
            saved = save_smart_control_config({"last_command_at": now_utc.isoformat()})
            add_activity("smart-control", "circulate", "success", f"room={room_f:.1f} target={target_f:.1f} outside={outside_f}")
            result.update({
                "action": "circulate",
                "midea_mode": "fan",
                "fan": "low",
                "verified": bool(command_result.get("verified")),
                "last_command_at": saved.get("last_command_at"),
            })
            return result

        if mild_outside and current_mode == 5 and running:
            if last_dt and now_utc - last_dt < timedelta(minutes=7):
                result["action"] = "hold"
                result["reason"] = "circulation_period"
                return result

        if running:
            command_result = midea.command({"action": "off"})
            save_device_state(command_result, True, source="smart-control")
            saved = save_smart_control_config({"last_command_at": now_utc.isoformat()})
            add_activity("smart-control", "comfort_off", "success", f"room={room_f:.1f} target={target_f:.1f}")
            result.update({
                "action": "off",
                "reason": "comfort_stable",
                "verified": bool(command_result.get("verified")),
                "last_command_at": saved.get("last_command_at"),
            })
            return result

        result["action"] = "hold"
        result["reason"] = "comfort_stable"
        return result

    if config.get("comfort_since") is not None:
        config = save_smart_control_config({"comfort_since": None})

    if last_dt and now_utc - last_dt < timedelta(minutes=min_minutes):
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
    fan = _adaptive_fan(abs_error_f)
    correction_limit = 2.0 if abs_error_f >= 6.0 else 1.5 if abs_error_f >= 3.0 else 1.0
    correction = max(-correction_limit, min(correction_limit, error_f))
    next_set_f = max(60.0, min(86.0, round((current_set_f + correction) * 2) / 2))

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
    saved = save_smart_control_config({"last_command_at": now_utc.isoformat()})
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
                    sensor = tuya.indoor_sensor(max_cache_age_seconds=20) if tuya.configured else None
                    if sensor and sensor.get("ok") and sensor.get("online") is not False:
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
                    save_smart_control_config(
                        {
                            "target_temperature_f": float(schedule["temperature"]),
                            "last_command_at": None,
                        }
                    )
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

    summary["smart_control"] = run_smart_control(now_utc)
    return summary
