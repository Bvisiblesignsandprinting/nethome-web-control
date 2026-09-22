from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo
import json
from urllib.parse import urlencode
from urllib.request import urlopen

from .config import settings


def _fetch(params: dict[str, Any]) -> dict[str, Any]:
    url = "https://api.open-meteo.com/v1/forecast?" + urlencode(params)
    with urlopen(url, timeout=12) as response:
        return json.loads(response.read().decode("utf-8"))


def _daily_data(days: int = 8) -> dict[str, Any]:
    params = {
        "latitude": settings.weather_latitude,
        "longitude": settings.weather_longitude,
        "timezone": settings.weather_timezone,
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
        "daily": ",".join([
            "temperature_2m_max",
            "temperature_2m_min",
            "precipitation_probability_max",
            "weather_code",
        ]),
        "forecast_days": max(1, min(days, 8)),
    }
    return _fetch(params)


def _summary_at(daily: dict[str, Any], idx: int) -> dict[str, Any]:
    return {
        "date": daily["time"][idx],
        "high_f": round(daily["temperature_2m_max"][idx]),
        "low_f": round(daily["temperature_2m_min"][idx]),
        "rain_chance": round(daily["precipitation_probability_max"][idx]),
        "weather_code": daily["weather_code"][idx],
    }


def _resolve_day_index(day: str, dates: list[str]) -> int:
    key = (day or "today").strip().lower()
    tz = ZoneInfo(settings.weather_timezone)
    today = datetime.now(tz).date()

    if key in {"today", "now"}:
        target = today
    elif key == "tomorrow":
        target = today + timedelta(days=1)
    else:
        weekday_names = {
            "mon": 0, "monday": 0,
            "tue": 1, "tues": 1, "tuesday": 1,
            "wed": 2, "wednesday": 2,
            "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
            "fri": 4, "friday": 4,
            "sat": 5, "saturday": 5,
            "sun": 6, "sunday": 6,
        }
        if key in weekday_names:
            delta = (weekday_names[key] - today.weekday()) % 7
            target = today + timedelta(days=delta)
        else:
            try:
                target = datetime.fromisoformat(key).date()
            except ValueError as exc:
                raise ValueError(
                    "Use TODAY, TOMORROW, a weekday, or a date like 2026-09-25."
                ) from exc

    target_s = target.isoformat()
    if target_s not in dates:
        raise ValueError("That date is outside the available 8-day forecast.")
    return dates.index(target_s)


def forecast_summary(day: str = "today") -> dict[str, Any]:
    data = _daily_data(8)
    daily = data["daily"]
    idx = _resolve_day_index(day, daily["time"])
    return _summary_at(daily, idx)


def forecast_range(days: int = 3) -> list[dict[str, Any]]:
    count = max(1, min(int(days), 8))
    data = _daily_data(count)
    daily = data["daily"]
    return [_summary_at(daily, i) for i in range(len(daily["time"]))]


def current_weather() -> dict[str, Any]:
    params = {
        "latitude": settings.weather_latitude,
        "longitude": settings.weather_longitude,
        "timezone": settings.weather_timezone,
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
        "current": ",".join([
            "temperature_2m",
            "apparent_temperature",
            "precipitation",
            "weather_code",
            "wind_speed_10m",
        ]),
        "forecast_days": 1,
    }
    data = _fetch(params)
    cur = data.get("current") or {}
    return {
        "time": cur.get("time"),
        "temperature_f": round(float(cur["temperature_2m"])) if cur.get("temperature_2m") is not None else None,
        "feels_like_f": round(float(cur["apparent_temperature"])) if cur.get("apparent_temperature") is not None else None,
        "precipitation_in": cur.get("precipitation"),
        "weather_code": cur.get("weather_code"),
        "wind_mph": round(float(cur["wind_speed_10m"])) if cur.get("wind_speed_10m") is not None else None,
    }


def comfort_recommendation(day: str = "today") -> dict[str, Any]:
    f = forecast_summary(day)
    high = f["high_f"]
    low = f["low_f"]
    if high >= 80:
        mode, temp = "cool", 77
    elif high >= 70:
        mode, temp = "cool", 75
    elif high <= 55:
        mode, temp = "heat", 70
    else:
        mode, temp = "auto", 72
    return {
        "forecast": f,
        "recommended_mode": mode,
        "recommended_temperature": temp,
    }
