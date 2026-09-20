from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
import json
from urllib.parse import urlencode
from urllib.request import urlopen

from .config import settings


def _fetch(params: dict[str, Any]) -> dict[str, Any]:
    url = "https://api.open-meteo.com/v1/forecast?" + urlencode(params)
    with urlopen(url, timeout=12) as response:
        return json.loads(response.read().decode("utf-8"))


def forecast_summary(day: str = "today") -> dict[str, Any]:
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
        "forecast_days": 3,
    }
    data = _fetch(params)
    daily = data["daily"]
    idx = 1 if day == "tomorrow" else 0
    return {
        "date": daily["time"][idx],
        "high_f": round(daily["temperature_2m_max"][idx]),
        "low_f": round(daily["temperature_2m_min"][idx]),
        "rain_chance": round(daily["precipitation_probability_max"][idx]),
        "weather_code": daily["weather_code"][idx],
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
