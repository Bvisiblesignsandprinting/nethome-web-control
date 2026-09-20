from __future__ import annotations

from typing import Any

from .config import settings
from .credentials import load_credentials


class MideaClient:
    def _cloud(self):
        account, password = load_credentials()
        if not account or not password:
            raise RuntimeError("NetHome Plus credentials are not configured")
        from midea_beautiful import connect_to_cloud
        return connect_to_cloud(account=account, password=password)

    def account_devices(self) -> list[dict[str, Any]]:
        cloud = self._cloud()
        result = cloud.api_request("/v1/appliance/user/list/get", {})
        return result.get("list", [])

    def status(self) -> dict[str, Any]:
        cloud = self._cloud()
        from midea_beautiful.lan import appliance_state
        appliance = appliance_state(
            cloud=cloud,
            use_cloud=True,
            appliance_id=settings.device_id,
            appliance_type="0xac",
            retries=2,
            cloud_timeout=9,
        )
        state = getattr(appliance, "state", appliance)
        return {
            "device_id": settings.device_id,
            "device_name": settings.device_name,
            "raw": str(state),
            "running": bool(getattr(state, "running", False)),
            "mode": getattr(state, "mode", None),
            "fan_speed": getattr(state, "fan_speed", None),
            "target_temperature_c": getattr(state, "target_temperature", None),
            "indoor_temperature_c": getattr(state, "indoor_temperature", None),
            "outdoor_temperature_c": getattr(state, "outdoor_temperature", None),
            "vertical_swing": getattr(state, "vertical_swing", None),
            "horizontal_swing": getattr(state, "horizontal_swing", None),
            "eco_mode": getattr(state, "eco_mode", None),
            "comfort_sleep": getattr(state, "comfort_sleep", None),
            "turbo": getattr(state, "turbo", None),
        }

    def command(self, command: dict[str, Any]) -> dict[str, Any]:
        if not settings.allow_writes:
            raise PermissionError("AC write commands are locked. Set NETHOME_ALLOW_WRITES=true only after read-only status is proven.")

        action = str(command.get("action") or "").strip().lower()
        if action not in {"on", "off", "set"}:
            raise NotImplementedError("Supported actions are ON, OFF, and SET.")

        cloud = self._cloud()
        cloud.max_retries = 1
        cloud.request_timeout = 6

        from midea_beautiful.lan import LanDevice, appliance_state

        if action == "set":
            # Mode / temperature / fan changes must start from the unit's real
            # current state. Building a blank AC state can produce a valid
            # power command while other settings are ignored by some models.
            appliance = appliance_state(
                cloud=cloud,
                use_cloud=True,
                appliance_id=settings.device_id,
                appliance_type="0xac",
                retries=1,
                cloud_timeout=6,
            )
        else:
            # Power-only commands can stay fast and do not need a pre-read.
            appliance = LanDevice(
                appliance_id=settings.device_id,
                appliance_type="0xac",
            )

        # Preserve Fahrenheit on every command.
        appliance.state.fahrenheit = True

        # Apply only the requested values to the current state.
        mode = command.get("mode")
        if mode not in (None, ""):
            appliance.state.mode = int(mode)

        temperature_f = command.get("temperature")
        if temperature_f is not None:
            appliance.state.target_temperature = (float(temperature_f) - 32.0) * 5.0 / 9.0

        fan = command.get("fan")
        if fan not in (None, ""):
            appliance.state.fan_speed = int(float(fan))

        if action == "on":
            appliance.state.running = True
        elif action == "off":
            appliance.state.running = False
        else:
            appliance.state.running = bool(command.get("running", True))

        for field in ("horizontal_swing", "vertical_swing", "eco_mode", "comfort_sleep", "turbo"):
            value = command.get(field)
            if value is not None:
                setattr(appliance.state, field, bool(value))

        # Send the SET packet directly. LanDevice.apply() performs a second
        # status refresh after every write; skipping that extra round-trip
        # makes the remote feel much faster.
        cmd = appliance.state.apply_command()
        data = appliance._lan_packet(cmd, False)
        try:
            cloud.appliance_transparent_send(settings.device_id, data)
        except Exception as exc:
            message = str(exc).lower()
            if any(token in message for token in ("3144", "3106", "loginid is empty", "invalidsession")):
                # Midea sessions can be invalidated when several commands arrive
                # close together. Start one completely fresh login and retry once.
                cloud = self._cloud()
                cloud.max_retries = 1
                cloud.request_timeout = 6
                cloud.appliance_transparent_send(settings.device_id, data)
            else:
                raise

        return {
            "device_id": settings.device_id,
            "device_name": settings.device_name,
            "action": action,
            "running": bool(appliance.state.running),
            "mode": appliance.state.mode,
            "target_temperature_c": appliance.state.target_temperature,
            "fan_speed": appliance.state.fan_speed,
            "horizontal_swing": appliance.state.horizontal_swing,
            "vertical_swing": appliance.state.vertical_swing,
            "eco_mode": appliance.state.eco_mode,
            "comfort_sleep": appliance.state.comfort_sleep,
            "turbo": appliance.state.turbo,
        }


midea = MideaClient()
