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
    target_f = float(config.get("target_temperature_f") or 72)
    deadband_f = max(0.5, float(config.get("deadband_f") or 1.0))
    preferred_mode = str(config.get("preferred_mode") or "auto").lower()
    if preferred_mode not in {"auto", "cool", "heat"}:
        preferred_mode = "auto"
    result.update({
        "room_temperature_f": round(room_f, 1),
        "target_temperature_f": round(target_f, 1),
        "preferred_mode": preferred_mode,
    })

    error_f = target_f - room_f
    if abs(error_f) <= deadband_f:
        result["action"] = "hold"
        return result

    last_command_at = config.get("last_command_at")
    min_minutes = max(1, int(config.get("min_command_interval_minutes") or 5))
    if last_command_at:
        try:
            last_dt = datetime.fromisoformat(str(last_command_at).replace("Z", "+00:00")).astimezone(timezone.utc)
            if now_utc - last_dt < timedelta(minutes=min_minutes):
                result["action"] = "hold"
                result["reason"] = "minimum_interval"
                return result
        except Exception:
            pass

    try:
        state = midea.status()
    except Exception as exc:
        add_activity("smart-control", "status", "error", str(exc)[:500])
        result["action"] = "skipped"
        result["reason"] = "ac_unavailable"
        return result

    current_c = state.get("target_temperature_c")
    current_set_f = (float(current_c) * 9.0 / 5.0 + 32.0) if current_c is not None else target_f

    # Decide which HVAC direction is allowed. In Auto, the external room
    # sensor may choose Heat or Cool. In a fixed preference, do not wake the
    # AC in the opposite direction just because the room crossed the target.
    desired_mode = preferred_mode
    if preferred_mode == "auto":
        desired_mode = "heat" if error_f > 0 else "cool"
    elif preferred_mode == "cool" and error_f > 0 and not bool(state.get("running")):
        result["action"] = "hold"
        result["reason"] = "cool_not_needed"
        return result
    elif preferred_mode == "heat" and error_f < 0 and not bool(state.get("running")):
        result["action"] = "hold"
        result["reason"] = "heat_not_needed"
        return result

    # External-sensor feedback correction. Limit each adjustment to 2F so the
    # room converges smoothly without hunting or hammering the Midea cloud.
    correction = max(-2.0, min(2.0, error_f))
    next_set_f = max(60.0, min(86.0, round(current_set_f + correction)))
    current_mode = int(state.get("mode") or 0)
    desired_mode_code = {"cool": 2, "heat": 4}.get(desired_mode, current_mode)
    if abs(next_set_f - current_set_f) < 0.5 and current_mode == desired_mode_code:
        result["action"] = "hold"
        return result

    command = {
        "action": "set",
        "temperature": next_set_f,
        "mode": desired_mode,
        "running": True,
    }
    command_result = midea.command(command)
    save_device_state(command_result, True, source="smart-control")
    saved = save_smart_control_config({"last_command_at": now_utc.isoformat()})
    add_activity(
        "smart-control",
        "adjust",
        "success",
        f"room={room_f:.1f} target={target_f:.1f} ac_set={current_set_f:.1f}->{next_set_f:.1f}",
    )
    result.update(
        {
            "action": "adjusted",
            "midea_setpoint_f": next_set_f,
            "midea_mode": desired_mode,
            "verified": bool(command_result.get("verified")),
            "last_command_at": saved.get("last_command_at"),
        }
    )
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
