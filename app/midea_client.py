from __future__ import annotations

import time
from typing import Any, Callable, TypeVar

from .config import settings
from .credentials import load_credentials
from .db import (
    clear_cloud_session,
    cloud_operation_lock,
    load_cloud_session,
    save_cloud_session,
)

T = TypeVar("T")

# This AC uses Midea's BB/sub-protocol mode numbering even when it accepts
# the legacy X40 control frame. The physical unit confirmed this mapping:
# Device calibration from the physical unit:
# wire 0=Cool, 2=Auto, 3=Heat, 4=Dry, 5=Fan.
# (wire 1 is not used for the five UI modes on this unit.)
# Keep the website/SMS/schedule API on the normal logical numbering
# 1=Auto, 2=Cool, 3=Dry, 4=Heat, 5=Fan.
LOGICAL_TO_WIRE_MODE = {1: 2, 2: 0, 3: 4, 4: 3, 5: 5}
WIRE_TO_LOGICAL_MODE = {0: 2, 2: 1, 3: 4, 4: 3, 5: 5}


class MideaClient:
    """Serverless-safe NetHome Plus cloud controller.

    Vercel invocations are serialized with a Postgres advisory lock and reuse
    one shared Midea auth session stored in Supabase. This avoids creating
    competing NetHome sessions from separate serverless instances.
    """

    def __init__(self):
        self._cloud_client = None

    @staticmethod
    def _credentials() -> tuple[str, str]:
        account, password = load_credentials()
        if not account or not password:
            raise RuntimeError("NetHome Plus credentials are not configured")
        return account, password

    @staticmethod
    def _new_cloud():
        from midea_beautiful.cloud import MideaCloud
        from midea_beautiful.midea import (
            DEFAULT_API_SERVER_URL,
            DEFAULT_APP_ID,
            DEFAULT_APPKEY,
            DEFAULT_HMACKEY,
            DEFAULT_IOTKEY,
            DEFAULT_PROXIED,
            DEFAULT_SIGNKEY,
        )

        account, password = MideaClient._credentials()
        cloud = MideaCloud(
            appkey=DEFAULT_APPKEY,
            account=account,
            password=password,
            appid=DEFAULT_APP_ID,
            api_url=DEFAULT_API_SERVER_URL,
            sign_key=DEFAULT_SIGNKEY,
            iot_key=DEFAULT_IOTKEY,
            hmac_key=DEFAULT_HMACKEY,
            proxied=DEFAULT_PROXIED,
        )
        cloud.max_retries = 1
        cloud.request_timeout = 12
        return cloud

    def _persist_cloud(self, cloud) -> None:
        session = dict(getattr(cloud, "_session", {}) or {})
        login_id = str(getattr(cloud, "_login_id", "") or "")
        if session and login_id:
            save_cloud_session({"login_id": login_id, "session": session})

    def _restore_cloud(self):
        saved = load_cloud_session()
        if not saved:
            return None
        session = saved.get("session")
        login_id = saved.get("login_id")
        if not isinstance(session, dict) or not login_id or not session.get("sessionId"):
            return None

        cloud = self._new_cloud()
        cloud._login_id = str(login_id)
        cloud._session = dict(session)
        access_token = session.get("accessToken")
        if access_token:
            cloud._security.access_token = str(access_token)
        return cloud

    def _fresh_cloud(self):
        clear_cloud_session()
        cloud = self._new_cloud()
        cloud.authenticate()
        self._persist_cloud(cloud)
        self._cloud_client = cloud
        return cloud

    def _cloud(self):
        # Reconcile each warm Vercel instance with the shared Supabase session.
        # This prevents a warm function from reviving an older session after
        # another instance has already refreshed authentication.
        saved = load_cloud_session()
        if self._cloud_client is not None:
            current_session = dict(getattr(self._cloud_client, "_session", {}) or {})
            current_id = current_session.get("sessionId")
            saved_session = saved.get("session") if isinstance(saved, dict) else None
            saved_id = saved_session.get("sessionId") if isinstance(saved_session, dict) else None
            if not saved_id or current_id == saved_id:
                return self._cloud_client
            self._cloud_client = None

        cloud = self._restore_cloud()
        if cloud is None:
            cloud = self._fresh_cloud()
        self._cloud_client = cloud
        return cloud

    @staticmethod
    def _auth_error(exc: Exception) -> bool:
        text = str(exc).lower()
        return any(
            token in text
            for token in (
                "3106",
                "3144",
                "invalidsession",
                "invalid session",
                "loginid is empty",
                "session-restart",
                "full-restart",
            )
        )

    @staticmethod
    def _retryable_transport_error(exc: Exception) -> bool:
        text = str(exc).lower()
        return any(
            token in text
            for token in (
                "timed out",
                "timeout",
                "connection reset",
                "connection aborted",
                "temporarily unavailable",
                "remote end closed",
                "3176",
                "reply does not exist",
                "no reply",
            )
        )

    def _run_cloud(self, operation: Callable[[Any], T]) -> T:
        """Run one serialized cloud operation with bounded recovery."""
        with cloud_operation_lock():
            last_exc: Exception | None = None
            for attempt in range(3):
                cloud = self._cloud()
                cloud.max_retries = 1
                cloud.request_timeout = 12
                try:
                    result = operation(cloud)
                    self._persist_cloud(cloud)
                    return result
                except Exception as exc:
                    last_exc = exc
                    if self._auth_error(exc):
                        clear_cloud_session()
                        self._cloud_client = None
                        if attempt < 2:
                            time.sleep(0.6 * (attempt + 1))
                            continue
                    elif self._retryable_transport_error(exc) and attempt < 2:
                        time.sleep(0.8 * (attempt + 1))
                        continue
                    raise

            assert last_exc is not None
            raise last_exc

    @staticmethod
    def _read_device(cloud):
        """Read state with one cloud request, skipping noisy capability probes."""
        from midea_beautiful.lan import LanDevice

        device = LanDevice(
            appliance_id=settings.device_id,
            appliance_type="0xac",
        )
        device.max_retries = 1
        cloud.max_retries = 1
        cloud.request_timeout = 12
        device.refresh(cloud)
        return device

    @staticmethod
    def _state_dict(device) -> dict[str, Any]:
        state = device.state
        wire_mode = getattr(state, "mode", None)
        try:
            logical_mode = WIRE_TO_LOGICAL_MODE.get(int(wire_mode), int(wire_mode))
        except (TypeError, ValueError):
            logical_mode = wire_mode
        return {
            "device_id": settings.device_id,
            "device_name": settings.device_name,
            "raw": str(state),
            "running": bool(getattr(state, "running", False)),
            "mode": logical_mode,
            "wire_mode": wire_mode,
            "fan_speed": getattr(state, "fan_speed", None),
            "target_temperature_c": getattr(state, "target_temperature", None),
            "indoor_temperature_c": getattr(state, "indoor_temperature", None),
            "outdoor_temperature_c": getattr(state, "outdoor_temperature", None),
            "vertical_swing": getattr(state, "vertical_swing", None),
            "horizontal_swing": getattr(state, "horizontal_swing", None),
            "eco_mode": getattr(state, "eco_mode", None),
            "comfort_sleep": getattr(state, "comfort_sleep", None),
            "turbo": getattr(state, "turbo", None),
            "source": "midea-cloud",
        }

    def account_devices(self) -> list[dict[str, Any]]:
        def operation(cloud):
            result = cloud.api_request("/v1/appliance/user/list/get", {})
            return result.get("list", [])

        return self._run_cloud(operation)

    def status(self) -> dict[str, Any]:
        return self._run_cloud(
            lambda cloud: self._state_dict(self._read_device(cloud))
        )

    @staticmethod
    def _apply_values(device, command: dict[str, Any]) -> dict[str, Any]:
        state = device.state
        action = str(command.get("action") or "").strip().lower()
        if action not in {"on", "off", "set"}:
            raise NotImplementedError("Supported actions are ON, OFF, and SET.")

        state.fahrenheit = True

        mode = command.get("mode")
        requested_logical_mode = None
        if mode not in (None, ""):
            mode_map = {
                "auto": 1,
                "cool": 2,
                "dry": 3,
                "heat": 4,
                "fan": 5,
                "fan_only": 5,
            }
            mode_value = mode_map.get(str(mode).strip().lower(), mode)
            requested_logical_mode = int(float(mode_value))
            if requested_logical_mode not in LOGICAL_TO_WIRE_MODE:
                raise ValueError(f"Unsupported AC mode: {mode}")
            state.mode = LOGICAL_TO_WIRE_MODE[requested_logical_mode]

        temperature_f = command.get("temperature")
        requested_c = None
        if temperature_f is not None:
            requested_c = round((((float(temperature_f) - 32.0) * 5.0 / 9.0) * 2.0)) / 2.0
            requested_c = max(16.0, min(31.0, requested_c))
            state.target_temperature = requested_c

        fan = command.get("fan")
        if fan not in (None, ""):
            fan_map = {"auto": 102, "silent": 20, "low": 40, "medium": 60, "high": 100}
            fan_value = fan_map.get(str(fan).strip().lower(), fan)
            state.fan_speed = int(float(fan_value))

        if action == "on":
            state.running = True
        elif action == "off":
            state.running = False
        elif command.get("running") is not None:
            state.running = bool(command["running"])

        for field in ("horizontal_swing", "vertical_swing", "eco_mode", "comfort_sleep", "turbo"):
            if command.get(field) is not None:
                setattr(state, field, bool(command[field]))

        return {
            "action": action,
            "requested_mode": requested_logical_mode,
            "requested_temperature_c": requested_c,
            "requested_running": bool(state.running) if action in {"on", "off"} or command.get("running") is not None else None,
        }

    @staticmethod
    def _send_state(cloud, device) -> None:
        cmd = device.state.apply_command()
        data = device._lan_packet(cmd, False)
        cloud.appliance_transparent_send(settings.device_id, data)

    @staticmethod
    def _matches_request(state: dict[str, Any], expected: dict[str, Any]) -> bool:
        if expected.get("requested_running") is not None:
            if bool(state.get("running")) != bool(expected["requested_running"]):
                return False
        if expected.get("requested_mode") is not None:
            try:
                if int(state.get("mode")) != int(expected["requested_mode"]):
                    return False
            except Exception:
                return False
        if expected.get("requested_temperature_c") is not None:
            try:
                if abs(float(state.get("target_temperature_c")) - float(expected["requested_temperature_c"])) > 0.6:
                    return False
            except Exception:
                return False
        return True

    def command(self, command: dict[str, Any]) -> dict[str, Any]:
        if not settings.allow_writes:
            raise PermissionError(
                "AC write commands are locked. Set NETHOME_ALLOW_WRITES=true."
            )

        def operation(cloud):
            device = self._read_device(cloud)
            expected = self._apply_values(device, command)

            verified = None
            last_write_error: Exception | None = None

            for write_attempt in range(3):
                try:
                    self._send_state(cloud, device)
                    last_write_error = None
                except Exception as exc:
                    last_write_error = exc
                    # Midea code 3176 means the cloud did not receive a reply.
                    # The AC may still have applied the command, so verify state
                    # before deciding that the write failed or sending it again.
                    if not self._retryable_transport_error(exc):
                        raise

                time.sleep(0.9 if last_write_error else 0.65)

                try:
                    verified_device = self._read_device(cloud)
                    verified = self._state_dict(verified_device)
                    if self._matches_request(verified, expected):
                        verified["action"] = expected["action"]
                        verified["verified"] = True
                        if last_write_error:
                            verified["verification_note"] = (
                                "Command confirmed after Midea cloud returned no reply."
                            )
                        return verified

                    device = verified_device
                    expected = self._apply_values(device, command)
                except Exception as verify_exc:
                    if not self._retryable_transport_error(verify_exc):
                        raise
                    if write_attempt == 2:
                        if last_write_error is not None:
                            raise last_write_error
                        raise verify_exc

                if write_attempt < 2:
                    time.sleep(0.6 * (write_attempt + 1))

            if verified is None:
                if last_write_error is not None:
                    raise last_write_error
                raise RuntimeError("AC state could not be verified after the command.")

            actual_f = None
            target_c = verified.get("target_temperature_c")
            if target_c is not None:
                actual_f = round((float(target_c) * 9.0 / 5.0) + 32.0)
            raise RuntimeError(
                "AC did not confirm the requested setting "
                f"(mode={verified.get('mode')}, temp={actual_f}F, running={verified.get('running')})."
            )

        return self._run_cloud(operation)


midea = MideaClient()
