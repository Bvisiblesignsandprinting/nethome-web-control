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

        from midea_beautiful.lan import LanDevice
        appliance = LanDevice(
            appliance_id=settings.device_id,
            appliance_type="0xac",
        )

        # The browser sends its last known state with each command so we can
        # preserve the other AC settings without doing a slow cloud read first.
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

        appliance.apply(cloud=cloud)
        return {
            "device_id": settings.device_id,
            "device_name": settings.device_name,
            "action": action,
            "running": bool(appliance.state.running),
            "mode": appliance.state.mode,
            "target_temperature_c": appliance.state.target_temperature,
            "fan_speed": appliance.state.fan_speed,
        }


midea = MideaClient()
