from __future__ import annotations

import json
from datetime import datetime
from urllib.request import Request as UrlRequest, urlopen
from zoneinfo import ZoneInfo

from .config import settings


def preview_schedule_prompt(prompt: str) -> dict:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    now = datetime.now(ZoneInfo("America/New_York"))
    schema = {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "schedules": {
                "type": "array",
                "maxItems": 30,
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "schedule_type": {"type": "string", "enum": ["one_time", "weekdays", "daily", "weekly"]},
                        "days": {"type": "string"},
                        "start_date": {"type": ["string", "null"]},
                        "end_date": {"type": ["string", "null"]},
                        "time_local": {"type": "string"},
                        "action": {"type": "string", "enum": ["set", "on", "off"]},
                        "mode": {"type": ["string", "null"], "enum": ["cool", "heat", "auto", "fan", "dry", None]},
                        "temperature": {"type": ["number", "null"]},
                        "fan": {"type": ["string", "null"], "enum": ["Auto", "Low", "Medium", "High", None]},
                        "enabled": {"type": "boolean"},
                    },
                    "required": [
                        "name", "schedule_type", "days", "start_date", "end_date",
                        "time_local", "action", "mode", "temperature", "fan", "enabled"
                    ],
                    "additionalProperties": False,
                },
            },
            "questions": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["summary", "schedules", "questions"],
        "additionalProperties": False,
    }

    instructions = f"""You convert a user's plain-English HVAC schedule request into a PREVIEW only.
Current local datetime: {now.isoformat()}
Timezone: America/New_York.

Do not invent a missing time, date, temperature, or mode. If a required detail is
missing, put a short question in questions and omit that ambiguous schedule.
Use 24-hour HH:MM for time_local. Use YYYY-MM-DD for dates.
For weekly days use comma-separated Mon,Tue,Wed,Thu,Fri,Sat,Sun tokens.
For weekdays use schedule_type=weekdays and days=Mon,Tue,Wed,Thu,Fri.
For daily use days=Mon,Tue,Wed,Thu,Fri,Sat,Sun.
For one_time set start_date to the exact date.
If the user says a desired room temperature, put it in temperature. In the new
Smart Room Control system that number is treated as the desired ROOM temperature.
Never save or execute anything. Return only the structured preview.
"""
    payload = {
        "model": settings.openai_model,
        "instructions": instructions,
        "input": prompt,
        "max_output_tokens": 1400,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "nethome_schedule_preview",
                "strict": True,
                "schema": schema,
            }
        },
    }
    req = UrlRequest(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.openai_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(req, timeout=25) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    output_text = str(data.get("output_text") or "").strip()
    if not output_text:
        for item in data.get("output") or []:
            for part in item.get("content") or []:
                if part.get("type") in {"output_text", "text"} and part.get("text"):
                    output_text = str(part["text"]).strip()
                    break
            if output_text:
                break
    if not output_text:
        raise RuntimeError("AI schedule preview returned no structured output")
    result = json.loads(output_text)
    if not isinstance(result, dict):
        raise RuntimeError("AI schedule preview returned invalid output")
    return result
