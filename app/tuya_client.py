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
from .db import sensor_operation_lock, load_sensor_cache, save_sensor_cache

SENSOR_REFRESH_SECONDS = 40


class TuyaCloudError(RuntimeError):
    def __init__(self, message: str, *, code: str | None = None, retry_after: int = 0):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


class TuyaCloudClient:
    def __init__(self) -> None:
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = Lock()

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
                f"{payload.get('msg', 'request failed')}",
                code=str(payload.get("code", "unknown")),
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

    def indoor_sensor(self, *, max_cache_age_seconds: int = SENSOR_REFRESH_SECONDS) -> dict:
        # Share one recent reading across callers without a five-minute control lag.
        if not self.configured:
            raise TuyaCloudError("Tuya cloud settings are not configured")
        cache_age = max(SENSOR_REFRESH_SECONDS, max_cache_age_seconds)
        identity = [settings.tuya_base_url, settings.tuya_client_id, settings.tuya_device_id]
        try:
            with sensor_operation_lock():
                state = load_sensor_cache()
                if state.get("identity") != identity:
                    state = {"identity": identity}
                now = time.time()
                sensor = state.get("sensor")
                if sensor and 0 <= now - state.get("fetched_at", 0) < cache_age:
                    result = dict(sensor)
                    result.update(cached=True, cache_age_seconds=int(now - state["fetched_at"]), cache_ttl_seconds=SENSOR_REFRESH_SECONDS)
                    return result
                next_fetch_at = state.get("next_fetch_at", 0)
                if sensor and state.get("error") is None:
                    # Old successful cache rows may still carry a 300-second deadline.
                    # Shorten that cooldown without resetting real error backoffs.
                    next_fetch_at = min(next_fetch_at, state.get("fetched_at", 0) + SENSOR_REFRESH_SECONDS)
                if now < next_fetch_at:
                    raise TuyaCloudError(state.get("error") or "Sensor refresh is pending", code=state.get("error_code"), retry_after=int(next_fetch_at - now) + 1)
                # Persist a reservation before contacting Tuya. A killed worker must
                # not let another cold instance immediately repeat its cloud calls.
                state.update(next_fetch_at=now + SENSOR_REFRESH_SECONDS, error="Sensor refresh is pending", error_code=None)
                save_sensor_cache(state)
                detail_ttl = 600 if state.get("online") is False else 3600
                check_online = now - state.get("detail_at", 0) >= detail_ttl
                try:
                    result = self._fetch_sensor(check_online=check_online, cached_online=state.get("online"))
                except TuyaCloudError as exc:
                    delay = 21600 if exc.code in {"28841004", "28841104"} else SENSOR_REFRESH_SECONDS
                    state.update(error=str(exc), error_code=exc.code, next_fetch_at=time.time() + delay)
                    save_sensor_cache(state)
                    raise TuyaCloudError(str(exc), code=exc.code, retry_after=delay) from exc
                fetched_at = time.time()
                result.update(fetched_at=datetime.fromtimestamp(fetched_at, tz=timezone.utc).isoformat(), cache_age_seconds=0, cache_ttl_seconds=SENSOR_REFRESH_SECONDS)
                state.update(sensor=result, fetched_at=fetched_at, next_fetch_at=fetched_at + SENSOR_REFRESH_SECONDS, error=None, error_code=None, online=result.get("online"))
                if check_online:
                    state["detail_at"] = fetched_at
                save_sensor_cache(state)
                return dict(result)
        except TuyaCloudError:
            raise
        except Exception as exc:
            # Fail closed if the shared limiter cannot be read/written. Do not
            # fall back to unlimited per-instance polling during a DB outage.
            raise TuyaCloudError("Sensor refresh unavailable; cloud requests paused to protect quota", retry_after=300) from exc

    def _fetch_sensor(self, *, check_online: bool, cached_online: bool | None) -> dict:
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

        online: bool | None = cached_online
        if check_online:
            try:
                detail = self._get(f"/v1.0/iot-03/devices/{device_id}")
                detail_result = detail.get("result") or {}
                if "online" in detail_result:
                    online = bool(detail_result.get("online"))
            except TuyaCloudError as exc:
                if exc.code in {"28841004", "28841104"}:
                    raise

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
        return result


tuya = TuyaCloudClient()
