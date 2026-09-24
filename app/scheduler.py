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
    save_device_state,
    update_schedule,
)
from .midea_client import midea
from .smart_control import apply_schedule, run_smart_control

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
            smart_result = apply_schedule(schedule)
            if smart_result is not None:
                result = {"smart_control": smart_result}
                add_activity(
                    "scheduler",
                    "schedule_execute",
                    "success",
                    f"schedule_id={schedule['id']} smart_room_control=true",
                )
            else:
                result = midea.command(command)
                save_device_state(result, True, source="cloud")
                add_activity(
                    "scheduler",
                    "schedule_execute",
                    "success",
                    f"schedule_id={schedule['id']} verified={result.get('verified', False)}",
                )

            finish_schedule_execution(execution_id, "success", result=result)

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

    try:\n        summary["smart_control"] = run_smart_control()\n    except Exception as exc:\n        add_activity("smart-control", "runner", "error", str(exc)[:500])\n        summary["smart_control"] = {"ok": False, "error": str(exc)[:300]}\n\n    return summary
