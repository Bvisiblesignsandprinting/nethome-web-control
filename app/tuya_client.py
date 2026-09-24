from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import settings


class TuyaCloudError(RuntimeError):
    pass


class TuyaCloudClient:
    def __init__(self) -> None:
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = Lock()
        self._cached_sensor: dict | None = None
        self._cached_sensor_at = 0.0

    @property
    def configured(self) -> bool:
        return bool(
            settings.tuya_client_id
            and settings.tuya_client_secret
            and settings.tuya_device_id
            and settings.tuya_base_url
        )

    @staticmethod
    def _content_sha256(body: bytes) -> str:
        return hashlib.sha256(body).hexdigest()

    def _signature(
        self,
        method: str,
        path: str,
        timestamp_ms: str,
        body: bytes = b"",
        access_token: str | None = None,
    ) -> str:
        string_to_sign = (
            f"{method.upper()}\n"
            f"{self._content_sha256(body)}\n"
            f"\n"
            f"{path}"
        )
        prefix = settings.tuya_client_id
        if access_token:
            prefix += access_token
        raw = f"{prefix}{timestamp_ms}{string_to_sign}"
        return hmac.new(
            settings.tuya_client_secret.encode(),
            raw.encode(),
            hashlib.sha256,
        ).hexdigest().upper()

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        access_token: str | None = None,
    ) -> dict:
        if not self.configured:
            raise TuyaCloudError("Tuya cloud settings are not configured")

        body = b""
        timestamp_ms = str(int(time.time() * 1000))
        headers = {
            "client_id": settings.tuya_client_id,
            "t": timestamp_ms,
            "sign_method": "HMAC-SHA256",
            "sign": self._signature(
                method,
                path,
                timestamp_ms,
                body=body,
                access_token=access_token,
            ),
            "Content-Type": "application/json",
        }
        if access_token:
            headers["access_token"] = access_token

        request = Request(
            settings.tuya_base_url.rstrip("/") + path,
            data=None,
            headers=headers,
            method=method.upper(),
        )
        try:
            with urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise TuyaCloudError(f"Tuya HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise TuyaCloudError(f"Tuya request failed: {exc}") from exc

        if not payload.get("success"):
            raise TuyaCloudError(
                f"Tuya API error {payload.get('code', 'unknown')}: "
                f"{payload.get('msg', 'request failed')}"
            )
        return payload

    def _get_access_token(self) -> str:
        now = time.time()
        if self._token and now < self._token_expires_at:
            return self._token

        with self._token_lock:
            now = time.time()
            if self._token and now < self._token_expires_at:
                return self._token

            payload = self._request_json("GET", "/v1.0/token?grant_type=1")
            result = payload.get("result") or {}
            token = str(result.get("access_token") or "")
            if not token:
                raise TuyaCloudError("Tuya token response did not include access_token")

            expires_in = int(result.get("expire_time") or 7200)
            self._token = token
            self._token_expires_at = now + max(60, expires_in - 60)
            return token

    def _get(self, path: str) -> dict:
        return self._request_json(
            "GET",
            path,
            access_token=self._get_access_token(),
        )

    def indoor_sensor(self, *, max_cache_age_seconds: int = 30) -> dict:
        now = time.time()
        if (
            self._cached_sensor
            and max_cache_age_seconds > 0
            and now - self._cached_sensor_at < max_cache_age_seconds
        ):
            cached = dict(self._cached_sensor)
            cached["cached"] = True
            return cached

        device_id = settings.tuya_device_id
        shadow = self._get(
            f"/v2.0/cloud/thing/{device_id}/shadow/properties"
        )
        properties = (shadow.get("result") or {}).get("properties") or []
        by_code = {
            str(item.get("code")): item
            for item in properties
            if isinstance(item, dict) and item.get("code")
        }

        temp_item = by_code.get("temp_current") or {}
        humidity_item = by_code.get("humidity_value") or {}
        battery_item = by_code.get("battery_state") or {}

        if "value" not in temp_item or "value" not in humidity_item:
            raise TuyaCloudError(
                "Tuya sensor response is missing temp_current or humidity_value"
            )

        temp_c = float(temp_item["value"]) / 10.0
        temp_f = temp_c * 9.0 / 5.0 + 32.0

        online: bool | None = None
        try:
            detail = self._get(f"/v1.0/iot-03/devices/{device_id}")
            detail_result = detail.get("result") or {}
            if "online" in detail_result:
                online = bool(detail_result.get("online"))
        except TuyaCloudError:
            pass

        updated_ms = max(
            int(temp_item.get("time") or 0),
            int(humidity_item.get("time") or 0),
            int(battery_item.get("time") or 0),
        )
        if updated_ms:
            updated_at = datetime.fromtimestamp(
                updated_ms / 1000.0,
                tz=timezone.utc,
            ).isoformat()
        else:
            updated_at = datetime.now(timezone.utc).isoformat()

        result = {
            "ok": True,
            "temperature_f": round(temp_f, 1),
            "temperature_c": round(temp_c, 1),
            "humidity": int(humidity_item["value"]),
            "battery": battery_item.get("value"),
            "online": online,
            "updated_at": updated_at,
            "source": "tuya-cloud",
            "cached": False,
        }
        self._cached_sensor = dict(result)
        self._cached_sensor_at = now
        return result


tuya = TuyaCloudClient()
