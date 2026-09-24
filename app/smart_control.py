from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .db import add_activity, load_smart_control_config, save_device_state, save_smart_control_config
from .midea_client import midea
from .tuya_client import TuyaCloudError, tuya

DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "target_temperature": 72.0,
    "calibration_f": 0.0,
    "deadband_f": 1.0,
    "min_command_interval_minutes": 5,
    "preferred_mode": "auto",
    "stale_after_minutes": 10,
    "last_command_at": None,
    "last_command": None,
}

ALLOWED_MODES = {"auto", "cool", "heat"}


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def get_config() -> dict[str, Any]:
    config = {**DEFAULTS, **load_smart_control_config()}
    config["enabled"] = bool(config.get("enabled", False))
    config["target_temperature"] = max(50.0, min(90.0, float(config.get("target_temperature", 72.0))))
    config["calibration_f"] = max(-10.0, min(10.0, float(config.get("calibration_f", 0.0))))
    config["deadband_f"] = max(0.5, min(4.0, float(config.get("deadband_f", 1.0))))
    config["min_command_interval_minutes"] = max(3, min(30, int(config.get("min_command_interval_minutes", 5))))
    config["stale_after_minutes"] = max(3, min(60, int(config.get("stale_after_minutes", 10))))
    preferred = str(config.get("preferred_mode") or "auto").lower()
    config["preferred_mode"] = preferred if preferred in ALLOWED_MODES else "auto"
    return config


def update_config(changes: dict[str, Any]) -> dict[str, Any]:
    config = get_config()
    allowed = {
        "enabled", "target_temperature", "calibration_f", "deadband_f",
        "min_command_interval_minutes", "preferred_mode", "stale_after_minutes",
        "last_command_at", "last_command",
    }
    for key, value in changes.items():
        if key in allowed and value is not None:
            config[key] = value
    # Normalize before persistence.
    config = {**config, **{
        "enabled": bool(config.get("enabled")),
        "target_temperature": max(50.0, min(90.0, float(config.get("target_temperature", 72.0)))),
        "calibration_f": max(-10.0, min(10.0, float(config.get("calibration_f", 0.0)))),
        "deadband_f": max(0.5, min(4.0, float(config.get("deadband_f", 1.0)))),
        "min_command_interval_minutes": max(3, min(30, int(config.get("min_command_interval_minutes", 5)))),
        "stale_after_minutes": max(3, min(60, int(config.get("stale_after_minutes", 10)))),
    }}
    preferred = str(config.get("preferred_mode") or "auto").lower()
    config["preferred_mode"] = preferred if preferred in ALLOWED_MODES else "auto"
    save_smart_control_config(config)
    return config


def read_sensor(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or get_config()
    try:
        sensor = tuya.indoor_sensor(max_cache_age_seconds=30)
    except TuyaCloudError as exc:
        return {"ok": False, "online": False, "stale": True, "error": str(exc), "source": "tuya-cloud"}

    updated = _parse_dt(sensor.get("updated_at"))
    age = (datetime.now(timezone.utc) - updated).total_seconds() if updated else None
    stale = age is None or age > int(config["stale_after_minutes"]) * 60
    raw_f = sensor.get("temperature_f")
    calibrated_f = None if raw_f is None else round(float(raw_f) + float(config["calibration_f"]), 1)
    online_flag = sensor.get("online")
    online = online_flag is not False and not stale
    return {
        **sensor,
        "ok": bool(sensor.get("ok", True)),
        "online": online,
        "stale": stale,
        "age_seconds": None if age is None else int(max(0, age)),
        "temperature_f_raw": raw_f,
        "temperature_f": calibrated_f,
    }


def state_document(*, run_controller: bool = False) -> dict[str, Any]:
    if run_controller:
        return run_smart_control()
    config = get_config()
    sensor = read_sensor(config)
    room_f = sensor.get("temperature_f") if sensor.get("ok") else None
    status = "disabled"
    if config["enabled"]:
        status = "monitoring" if sensor.get("online") else "sensor_offline"
    return {
        "ok": True,
        "config": config,
        "sensor": sensor,
        "room_temperature_f": room_f,
        "status": status,
    }


def _minimum_interval_open(config: dict[str, Any], now: datetime) -> bool:
    last = _parse_dt(config.get("last_command_at"))
    if not last:
        return True
    return now - last >= timedelta(minutes=int(config["min_command_interval_minutes"]))


def _record_command(config: dict[str, Any], command: dict[str, Any], result: dict[str, Any]) -> None:
    config["last_command_at"] = datetime.now(timezone.utc).isoformat()
    config["last_command"] = command
    save_smart_control_config(config)
    save_device_state(result, True, source="cloud")
    add_activity("smart-control", "device_command", "success", f"command={command}")


def _send(config: dict[str, Any], command: dict[str, Any]) -> dict[str, Any]:
    try:
        result = midea.command(command)
        _record_command(config, command, result)
        return {"sent": True, "command": command, "result": result}
    except Exception as exc:
        add_activity("smart-control", "device_command", "error", f"command={command} error={exc}")
        return {"sent": False, "command": command, "error": str(exc)}


def run_smart_control() -> dict[str, Any]:
    config = get_config()
    sensor = read_sensor(config)
    room_f = sensor.get("temperature_f") if sensor.get("ok") else None
    response: dict[str, Any] = {
        "ok": True,
        "config": config,
        "sensor": sensor,
        "room_temperature_f": room_f,
        "status": "disabled",
        "action": None,
    }
    if not config["enabled"]:
        return response
    if not sensor.get("online") or room_f is None:
        response["status"] = "sensor_offline"
        return response

    now = datetime.now(timezone.utc)
    target = float(config["target_temperature"])
    band = float(config["deadband_f"])
    high = target + band
    low = target - band
    preferred = str(config["preferred_mode"])
    room = float(room_f)

    if room > high:
        if preferred == "heat":
            desired = {"action": "off"}
            label = "too_warm_heat_only"
        else:
            desired = {
                "action": "set", "mode": "cool",
                "temperature": max(60.0, target - 2.0),
                "fan": "Auto", "running": True,
            }
            label = "cooling"
    elif room < low:
        if preferred == "cool":
            desired = {"action": "off"}
            label = "too_cold_cool_only"
        else:
            desired = {
                "action": "set", "mode": "heat",
                "temperature": min(86.0, target + 2.0),
                "fan": "Auto", "running": True,
            }
            label = "heating"
    else:
        desired = {"action": "off"}
        label = "comfort_band"

    response["status"] = label
    response["desired_command"] = desired

    # Avoid repeated cloud writes. Inside the comfort band we still use the
    # external sensor as authority, but only send OFF when the minimum interval opens.
    last_command = config.get("last_command")
    if last_command == desired:
        response["action"] = "already_set"
        return response
    if not _minimum_interval_open(config, now):
        response["action"] = "waiting_min_interval"
        return response

    outcome = _send(config, desired)
    response["action"] = outcome
    response["config"] = get_config()
    return response


def apply_schedule(schedule: dict[str, Any]) -> dict[str, Any] | None:
    """Map a normal schedule into Smart Room Control when possible.

    SET schedules with temperature + Cool/Heat/Auto become desired room targets.
    OFF disables Smart Control so the next minute cannot immediately turn the AC
    back on. ON re-enables Smart Control using the saved target.
    """
    action = str(schedule.get("action") or "set").lower()
    if action == "off":
        update_config({"enabled": False})
        return None
    if action == "on":
        update_config({"enabled": True})
        return run_smart_control()
    if action != "set" or schedule.get("temperature") is None:
        return None

    mode = str(schedule.get("mode") or "auto").lower()
    if mode not in ALLOWED_MODES:
        return None
    update_config({
        "enabled": True,
        "target_temperature": float(schedule["temperature"]),
        "preferred_mode": mode,
    })
    return run_smart_control()
