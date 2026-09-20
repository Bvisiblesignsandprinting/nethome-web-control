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
        if action not in {"on", "off"}:
            raise NotImplementedError("Only ON and OFF commands are implemented right now.")

        cloud = self._cloud()
        cloud.max_retries = 2
        cloud.request_timeout = 9

        # For power-only commands we do not need to perform a full status /
        # capability discovery first. That extra identify round-trip is slower
        # and can fail even when the appliance itself is online.
        from midea_beautiful.lan import LanDevice
        appliance = LanDevice(
            appliance_id=settings.device_id,
            appliance_type="0xac",
        )
        appliance.state.running = action == "on"
        appliance.apply(cloud=cloud)
        return {
            "device_id": settings.device_id,
            "device_name": settings.device_name,
            "action": action,
            "running": bool(appliance.state.running),
        }


midea = MideaClient()
