from __future__ import annotations

import os
from dataclasses import dataclass


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    host: str = os.getenv("NETHOME_HOST", "127.0.0.1")
    port: int = int(os.getenv("NETHOME_PORT", "8765"))
    device_id: str = os.getenv("NETHOME_DEVICE_ID", "151732606554174")
    device_name: str = os.getenv("NETHOME_DEVICE_NAME", "Sukkah/Play Room AC")
    allow_writes: bool = _as_bool(os.getenv("NETHOME_ALLOW_WRITES"), False)
    api_token: str = os.getenv("NETHOME_API_TOKEN", "change-me-before-remote-access")
    automation_secret: str | None = (
        os.getenv("NETHOME_AUTOMATION_SECRET")
        or os.getenv("CRON_SECRET")
        or os.getenv("NETHOME_API_TOKEN")
        or None
    )
    message_secret: str | None = (
        os.getenv("NETHOME_MESSAGE_SECRET")
        or os.getenv("NETHOME_API_TOKEN")
        or None
    )
    worker_secret: str | None = (
        os.getenv("NETHOME_WORKER_SECRET")
        or os.getenv("NETHOME_AUTOMATION_SECRET")
        or os.getenv("NETHOME_API_TOKEN")
        or None
    )
    weather_latitude: float = float(os.getenv("NETHOME_WEATHER_LATITUDE", "41.3318"))
    weather_longitude: float = float(os.getenv("NETHOME_WEATHER_LONGITUDE", "-74.1868"))
    weather_timezone: str = os.getenv("NETHOME_WEATHER_TIMEZONE", "America/New_York")
    login_password: str | None = os.getenv("NETHOME_LOGIN_PASSWORD") or None
    account: str | None = os.getenv("NETHOME_ACCOUNT") or None
    password: str | None = os.getenv("NETHOME_PASSWORD") or None


settings = Settings()
