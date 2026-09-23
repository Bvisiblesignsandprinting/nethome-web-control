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
    twilio_auth_token: str | None = os.getenv("TWILIO_AUTH_TOKEN") or None
    openai_api_key: str | None = os.getenv("OPENAI_API_KEY") or None
    openai_model: str = os.getenv("NETHOME_OPENAI_MODEL", "gpt-5.6-luna")
    panel_firmware_version: str = os.getenv("NETHOME_PANEL_FIRMWARE_VERSION", "1.0.0")
    panel_firmware_url: str | None = os.getenv("NETHOME_PANEL_FIRMWARE_URL") or None
    panel_firmware_sha256: str | None = os.getenv("NETHOME_PANEL_FIRMWARE_SHA256") or None
    panel_firmware_notes: str = os.getenv("NETHOME_PANEL_FIRMWARE_NOTES", "")
    panel_config_json: str = os.getenv("NETHOME_PANEL_CONFIG_JSON", "{}")


settings = Settings()
