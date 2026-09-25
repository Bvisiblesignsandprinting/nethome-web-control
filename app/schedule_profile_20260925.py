from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .db import (
    add_activity,
    create_schedule,
    delete_schedule,
    list_schedules,
    load_smart_control_config,
    save_smart_control_config,
)

PROFILE_VERSION = "smart-room-preset-schedule-2026-09-25-v1"


def apply_requested_smart_room_schedule() -> None:
    """Apply the user-requested Smart Room daily preset schedule exactly once."""
    config = load_smart_control_config()
    if config.get("schedule_profile_version") == PROFILE_VERSION:
        return

    for row in list_schedules():
        delete_schedule(int(row["id"]))

    schedules = [
        {
            "name": "Smart Room - All Day",
            "schedule_type": "daily",
            "days": "Mon,Tue,Wed,Thu,Fri,Sat,Sun",
            "time_local": "08:00",
            "action": "set",
            "temperature": 73.0,
            "mode": None,
            "fan": None,
            "enabled": True,
        },
        {
            "name": "Smart Room - Sudah",
            "schedule_type": "daily",
            "days": "Mon,Tue,Wed,Thu,Fri,Sat,Sun",
            "time_local": "20:00",
            "action": "set",
            "temperature": 74.0,
            "mode": None,
            "fan": None,
            "enabled": True,
        },
        {
            "name": "Smart Room - Sleeping",
            "schedule_type": "daily",
            "days": "Mon,Tue,Wed,Thu,Fri,Sat,Sun",
            "time_local": "23:00",
            "action": "set",
            "temperature": 72.0,
            "mode": None,
            "fan": None,
            "enabled": True,
        },
    ]

    for schedule in schedules:
        create_schedule(schedule)

    now_local = datetime.now(ZoneInfo("America/New_York"))
    current_minutes = now_local.hour * 60 + now_local.minute
    if 20 * 60 <= current_minutes < 23 * 60:
        active_preset = "sudah"
        target = 74.0
    elif current_minutes >= 23 * 60 or current_minutes < 8 * 60:
        active_preset = "sleeping"
        target = 72.0
    else:
        active_preset = "all_day"
        target = 73.0

    save_smart_control_config(
        {
            "active_preset": active_preset,
            "target_temperature_f": target,
            "comfort_since": None,
            "last_command_at": None,
            "schedule_profile_version": PROFILE_VERSION,
        }
    )
    add_activity(
        "system",
        "schedule_profile",
        "success",
        f"version={PROFILE_VERSION} active_preset={active_preset} target={target}",
    )
