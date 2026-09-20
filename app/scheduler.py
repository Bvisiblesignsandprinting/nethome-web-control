from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Any

from .config import settings
from .db import (
    add_activity,
    claim_schedule_execution,
    finish_schedule_execution,
    list_schedules,
)

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
    text = str(value)
    return time.fromisoformat(text)


def _schedule_due_at(
    schedule: dict[str, Any],
    now_utc: datetime,
) -> datetime | None:
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

    # The runner is invoked once per minute. Only match the current local minute.
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
    command: dict[str, Any] = {
        "action": schedule.get("action") or "set",
    }
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

        # Leave the claimed execution as pending so the always-on local worker
        # can execute it with the persistent NetHome Plus session.
        if _use_worker_queue := True:
            # claim_schedule_execution creates the row as pending. It is now
            # the worker's job to claim and complete it.
            item["status"] = "queued"
            add_activity(
                "scheduler",
                "schedule_execute",
                "queued",
                f"schedule_id={schedule['id']} execution_id={execution_id}",
            )
            summary["executions"].append(item)
            continue


            continue

        try:
            result = midea.command(command)
            finish_schedule_execution(
                execution_id,
                "success",
                result=result,
            )
            add_activity(
                "scheduler",
                "schedule_execute",
                "success",
                f"schedule_id={schedule['id']}",
            )
            item["status"] = "success"
            item["result"] = result
            summary["success"] += 1
        except Exception as exc:
            finish_schedule_execution(
                execution_id,
                "failed",
                error=str(exc),
            )
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

    return summary
