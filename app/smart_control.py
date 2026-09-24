from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .config import settings
from .db import (
    add_activity,
    get_latest_device_state,
    load_smart_control_config,
    save_device_state,
    save_smart_control_config,
)
from .midea_client import midea
from .tuya_client import TuyaCloudError, tuya


DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "target_temperature": 72.0,
    "calibration_f": 0.0,
    "deadband_f": 1.0,
    "min_command_interval_minutes": 5,
    "preferred_mode": "auto",
    "preferred_fan": "auto",
    "last_command_at": None,
    "last_action": None,
    "last_status": "disabled",
}

SENSOR_STALE_SECONDS = 5 * 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def get_smart_config() -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    stored = load_smart_control_config()
    if isinstance(stored, dict):
        config.update(stored)
    config["target_temperature"] = max(60.0, min(86.0, float(config.get("target_temperature") or 72.0)))
    config["calibration_f"] = max(-5.0, min(5.0, float(config.get("calibration_f") or 0.0)))
    config["deadband_f"] = max(0.5, min(3.0, float(config.get("deadband_f") or 1.0)))
    config["min_command_interval_minutes"] = max(
        3, min(30, int(config.get("min_command_interval_minutes") or 5))
    )
    mode = str(config.get("preferred_mode") or "auto").lower()
    config["preferred_mode"] = mode if mode in {"auto", "cool", "heat"} else "auto"
    fan = str(config.get("preferred_fan") or "auto").lower()
    config["preferred_fan"] = fan if fan in {"auto", "low", "medium", "high"} else "auto"
    config["enabled"] = bool(config.get("enabled"))
    return config


def update_smart_config(changes: dict[str, Any]) -> dict[str, Any]:
    config = get_smart_config()
    for key in (
        "enabled",
        "target_temperature",
        "calibration_f",
        "deadband_f",
        "min_command_interval_minutes",
        "preferred_mode",
        "preferred_fan",
        "last_command_at",
        "last_action",
        "last_status",
    ):
        if key in changes and changes[key] is not None:
            config[key] = changes[key]

    config["enabled"] = bool(config.get("enabled"))
    config["target_temperature"] = max(60.0, min(86.0, float(config.get("target_temperature") or 72.0)))
    config["calibration_f"] = max(-5.0, min(5.0, float(config.get("calibration_f") or 0.0)))
    config["deadband_f"] = max(0.5, min(3.0, float(config.get("deadband_f") or 1.0)))
    config["min_command_interval_minutes"] = max(
        3, min(30, int(config.get("min_command_interval_minutes") or 5))
    )
    mode = str(config.get("preferred_mode") or "auto").lower()
    config["preferred_mode"] = mode if mode in {"auto", "cool", "heat"} else "auto"
    fan = str(config.get("preferred_fan") or "auto").lower()
    config["preferred_fan"] = fan if fan in {"auto", "low", "medium", "high"} else "auto"
    save_smart_control_config(config)
    return config


def _sensor_snapshot(config: dict[str, Any]) -> tuple[dict[str, Any], float | None, str]:
    try:
        sensor = tuya.indoor_sensor(max_cache_age_seconds=30)
    except TuyaCloudError as exc:
        return (
            {
                "ok": False,
                "online": False,
                "error": str(exc)[:300],
                "source": "tuya-cloud",
            },
            None,
            "sensor_offline",
        )

    raw_temp = sensor.get("temperature_f")
    if raw_temp is None:
        return sensor, None, "sensor_offline"

    room_f = round(float(raw_temp) + float(config.get("calibration_f") or 0.0), 1)
    updated_at = _parse_dt(sensor.get("updated_at"))
    stale = bool(updated_at and (_now() - updated_at).total_seconds() > SENSOR_STALE_SECONDS)
    explicitly_offline = sensor.get("online") is False
    if explicitly_offline:
        status = "sensor_offline"
    elif stale:
        status = "sensor_stale"
    else:
        status = "active" if config.get("enabled") else "disabled"
    return sensor, room_f, status


def smart_control_snapshot(
    *,
    config: dict[str, Any] | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    config = config or get_smart_config()
    sensor, room_f, sensor_status = _sensor_snapshot(config)
    current_status = status or str(config.get("last_status") or sensor_status)
    if not config.get("enabled"):
        current_status = "disabled"
    elif sensor_status in {"sensor_offline", "sensor_stale"}:
        current_status = sensor_status

    requested_enabled = bool(config.get("enabled"))
    effective_enabled = requested_enabled and sensor_status not in {"sensor_offline", "sensor_stale"}
    public_config = {
        "enabled": effective_enabled,
        "requested_enabled": requested_enabled,
        "target_temperature": float(config.get("target_temperature") or 72.0),
        "calibration_f": float(config.get("calibration_f") or 0.0),
        "deadband_f": float(config.get("deadband_f") or 1.0),
        "min_command_interval_minutes": int(config.get("min_command_interval_minutes") or 5),
        "preferred_mode": str(config.get("preferred_mode") or "auto"),
        "preferred_fan": str(config.get("preferred_fan") or "auto"),
        "last_command_at": config.get("last_command_at"),
        "last_action": config.get("last_action"),
    }
    return {
        "ok": True,
        "config": public_config,
        "sensor": sensor,
        "room_temperature_f": room_f,
        "status": current_status,
    }


def _desired_action(config: dict[str, Any], room_f: float) -> str | None:
    target = float(config["target_temperature"])
    deadband = float(config["deadband_f"])
    upper = target + deadband
    lower = target - deadband
    mode = str(config.get("preferred_mode") or "auto")

    if mode == "cool":
        if room_f >= upper:
            return "cool"
        if room_f <= lower:
            return "off"
        return None
    if mode == "heat":
        if room_f <= lower:
            return "heat"
        if room_f >= upper:
            return "off"
        return None

    if room_f >= upper:
        return "cool"
    if room_f <= lower:
        return "heat"
    return "off"


def _cached_state_matches(action: str) -> bool:
    cached = get_latest_device_state()
    state = (cached or {}).get("state") or {}
    if not state:
        return False
    running = bool(state.get("running"))
    mode = state.get("mode")
    if action == "off":
        return not running
    expected_mode = 2 if action == "cool" else 4
    try:
        return running and int(mode) == expected_mode
    except Exception:
        return False


def run_smart_control(
    now_utc: datetime | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    now_utc = (now_utc or _now()).astimezone(timezone.utc)
    config = get_smart_config()
    if not config.get("enabled"):
        config["last_status"] = "disabled"
        save_smart_control_config(config)
        return smart_control_snapshot(config=config, status="disabled")

    sensor, room_f, sensor_status = _sensor_snapshot(config)
    if room_f is None or sensor_status in {"sensor_offline", "sensor_stale"}:
        config["last_status"] = sensor_status
        save_smart_control_config(config)
        result = smart_control_snapshot(config=config, status=sensor_status)
        result["sensor"] = sensor
        result["room_temperature_f"] = room_f
        return result

    desired = _desired_action(config, room_f)
    if desired is None:
        config["last_status"] = "holding"
        save_smart_control_config(config)
        result = smart_control_snapshot(config=config, status="holding")
        result["sensor"] = sensor
        result["room_temperature_f"] = room_f
        return result

    last_at = _parse_dt(config.get("last_command_at"))
    minimum_seconds = int(config["min_command_interval_minutes"]) * 60
    if not force and last_at and (now_utc - last_at).total_seconds() < minimum_seconds:
        config["last_status"] = "waiting_interval"
        save_smart_control_config(config)
        result = smart_control_snapshot(config=config, status="waiting_interval")
        result["sensor"] = sensor
        result["room_temperature_f"] = room_f
        return result

    if not force and str(config.get("last_action") or "") == desired and _cached_state_matches(desired):
        config["last_status"] = "holding"
        save_smart_control_config(config)
        result = smart_control_snapshot(config=config, status="holding")
        result["sensor"] = sensor
        result["room_temperature_f"] = room_f
        return result

    if not settings.allow_writes:
        config["last_status"] = "writes_locked"
        save_smart_control_config(config)
        result = smart_control_snapshot(config=config, status="writes_locked")
        result["sensor"] = sensor
        result["room_temperature_f"] = room_f
        return result

    if desired == "off":
        command = {"action": "off"}
    else:
        target = float(config["target_temperature"])
        # Push the internal AC target slightly beyond the room target. The
        # external Tuya sensor remains the thermostat; this avoids the Midea
        # head-unit sensor stopping too early.
        raw_setpoint = target - 2.0 if desired == "cool" else target + 2.0
        command = {
            "action": "set",
            "mode": desired,
            "temperature": max(60.0, min(86.0, raw_setpoint)),
            "fan": str(config.get("preferred_fan") or "auto"),
            "running": True,
        }

    try:
        result_state = midea.command(command)
        save_device_state(result_state, True, source="cloud")
        config["last_command_at"] = now_utc.isoformat()
        config["last_action"] = desired
        config["last_status"] = "command_sent"
        save_smart_control_config(config)
        add_activity(
            "smart-control",
            "thermostat_adjust",
            "success",
            (
                f"room={room_f:.1f} target={float(config['target_temperature']):.1f} "
                f"deadband={float(config['deadband_f']):.1f} action={desired}"
            ),
        )
        response = smart_control_snapshot(config=config, status="command_sent")
        response["sensor"] = sensor
        response["room_temperature_f"] = room_f
        response["command"] = command
        response["result"] = result_state
        return response
    except Exception as exc:
        config["last_status"] = "command_error"
        save_smart_control_config(config)
        add_activity("smart-control", "thermostat_adjust", "error", str(exc)[:500])
        response = smart_control_snapshot(config=config, status="command_error")
        response["sensor"] = sensor
        response["room_temperature_f"] = room_f
        response["error"] = str(exc)[:500]
        return response
