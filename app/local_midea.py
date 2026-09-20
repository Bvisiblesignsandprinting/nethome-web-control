from __future__ import annotations

import asyncio
from typing import Any

from .config import settings


class LocalMideaController:
    """Local-LAN controller using msmart-ng.

    The worker and the AC are expected to be on the same LAN. Discovery
    authenticates V3 devices once; normal status/control then stays local.
    """

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._device = None

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    async def _discover(self):
        from msmart.discover import Discover

        devices = await Discover.discover()
        target_id = int(settings.device_id)
        for device in devices:
            if int(getattr(device, "id", 0) or 0) == target_id:
                self._device = device
                return device

        # Some networks suppress broadcast discovery. If an already-discovered
        # device is not found by ID, do not silently control another AC.
        found = [str(getattr(d, "id", "?")) for d in devices]
        raise RuntimeError(
            f"Local AC {settings.device_id} not found. Discovered IDs: {', '.join(found) or 'none'}"
        )

    async def _get_device(self):
        if self._device is None:
            await self._discover()
        return self._device

    async def _refresh(self):
        device = await self._get_device()
        await device.refresh()
        if not bool(getattr(device, "online", False)):
            # Drop the cached device so the next call re-discovers it.
            self._device = None
            raise RuntimeError("Local AC is offline")
        return device

    @staticmethod
    def _enum_int(value):
        try:
            return int(value)
        except Exception:
            try:
                return int(value.value)
            except Exception:
                return value

    @staticmethod
    def _status_dict(device) -> dict[str, Any]:
        swing = LocalMideaController._enum_int(getattr(device, "swing_mode", 0))
        return {
            "device_id": settings.device_id,
            "device_name": settings.device_name,
            "raw": str(device),
            "running": bool(getattr(device, "power_state", False)),
            "mode": LocalMideaController._enum_int(getattr(device, "operational_mode", None)),
            "fan_speed": LocalMideaController._enum_int(getattr(device, "fan_speed", None)),
            "target_temperature_c": getattr(device, "target_temperature", None),
            "indoor_temperature_c": getattr(device, "indoor_temperature", None),
            "outdoor_temperature_c": getattr(device, "outdoor_temperature", None),
            "vertical_swing": swing in (0xC, 0xF),
            "horizontal_swing": swing in (0x3, 0xF),
            "eco_mode": bool(getattr(device, "eco", False)),
            "comfort_sleep": bool(getattr(device, "sleep", False)),
            "turbo": bool(getattr(device, "turbo", False)),
            "source": "local-lan",
        }

    def status(self) -> dict[str, Any]:
        device = self._run(self._refresh())
        return self._status_dict(device)

    async def _apply_command(self, command: dict[str, Any]):
        from msmart.device import AirConditioner as AC

        device = await self._refresh()
        action = str(command.get("action") or "").strip().lower()
        if action not in {"on", "off", "set"}:
            raise ValueError("Unsupported AC action")

        if action == "on":
            device.power_state = True
        elif action == "off":
            device.power_state = False
        else:
            device.power_state = bool(command.get("running", True))

        mode = command.get("mode")
        if mode not in (None, ""):
            mode_map = {
                "auto": AC.OperationalMode.AUTO,
                "cool": AC.OperationalMode.COOL,
                "dry": AC.OperationalMode.DRY,
                "heat": AC.OperationalMode.HEAT,
                "fan": AC.OperationalMode.FAN_ONLY,
                "fan_only": AC.OperationalMode.FAN_ONLY,
                "1": AC.OperationalMode.AUTO,
                "2": AC.OperationalMode.COOL,
                "3": AC.OperationalMode.DRY,
                "4": AC.OperationalMode.HEAT,
                "5": AC.OperationalMode.FAN_ONLY,
            }
            key = str(mode).strip().lower()
            device.operational_mode = mode_map.get(key, AC.OperationalMode(int(float(mode))))

        temperature_f = command.get("temperature")
        if temperature_f is not None:
            # Midea controls in Celsius, normally in 0.5 C increments.
            celsius = (float(temperature_f) - 32.0) * 5.0 / 9.0
            device.target_temperature = round(celsius * 2.0) / 2.0

        fan = command.get("fan")
        if fan not in (None, ""):
            fan_map = {
                "auto": AC.FanSpeed.AUTO,
                "silent": AC.FanSpeed.SILENT,
                "low": AC.FanSpeed.LOW,
                "medium": AC.FanSpeed.MEDIUM,
                "high": AC.FanSpeed.MAX,
            }
            key = str(fan).strip().lower()
            if key in fan_map:
                device.fan_speed = fan_map[key]
            else:
                device.fan_speed = AC.FanSpeed(int(float(fan)))

        horizontal = command.get("horizontal_swing")
        vertical = command.get("vertical_swing")
        if horizontal is not None or vertical is not None:
            current = self._enum_int(getattr(device, "swing_mode", 0))
            h = bool(horizontal) if horizontal is not None else current in (0x3, 0xF)
            v = bool(vertical) if vertical is not None else current in (0xC, 0xF)
            if h and v:
                device.swing_mode = AC.SwingMode.BOTH
            elif h:
                device.swing_mode = AC.SwingMode.HORIZONTAL
            elif v:
                device.swing_mode = AC.SwingMode.VERTICAL
            else:
                device.swing_mode = AC.SwingMode.OFF

        if command.get("eco_mode") is not None:
            device.eco = bool(command["eco_mode"])
        if command.get("comfort_sleep") is not None:
            device.sleep = bool(command["comfort_sleep"])
        if command.get("turbo") is not None:
            device.turbo = bool(command["turbo"])

        # Preserve Fahrenheit display on the physical unit.
        device.fahrenheit = True

        await device.apply()
        await asyncio.sleep(0.35)
        await device.refresh()
        if not bool(getattr(device, "online", False)):
            raise RuntimeError("AC did not answer after command")
        return device

    def command(self, command: dict[str, Any]) -> dict[str, Any]:
        device = self._run(self._apply_command(command))
        result = self._status_dict(device)
        result["action"] = str(command.get("action") or "").lower()
        result["verified"] = True
        return result


local_midea = LocalMideaController()
