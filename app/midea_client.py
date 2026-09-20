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
            retries=2,
            cloud_timeout=9,
        )
        state = getattr(appliance, "state", appliance)
        return {
            "device_id": settings.device_id,
            "device_name": settings.device_name,
            "raw": str(state),
        }

    def command(self, command: dict[str, Any]) -> dict[str, Any]:
        if not settings.allow_writes:
            raise PermissionError("AC write commands are locked. Set NETHOME_ALLOW_WRITES=true only after read-only status is proven.")
        raise NotImplementedError("Write commands stay intentionally locked until the live device is back online and read-only status is verified.")


midea = MideaClient()
