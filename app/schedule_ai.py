from __future__ import annotations

import json
from datetime import datetime
from urllib.request import Request as UrlRequest, urlopen
from zoneinfo import ZoneInfo

from .config import settings
from .db import add_activity


def interpret_schedule_prompt(prompt: str) -> dict:
    """Interpret a natural-language schedule request into a preview only.

    This function never writes schedules. The website must explicitly confirm
    the preview and then use the existing validated schedule-create endpoint.
    """
    if not settings.openai_api_key:
        return {
            "ok": False,
            "needs_clarification": True,
            "message": "Schedule assistant is not configured.",
            "schedules": [],
        }

    now = datetime.now(ZoneInfo(settings.weather_timezone or "America/New_York"))
    schema = {
        "type": "object",
        "properties": {
            "needs_clarification": {"type": "boolean"},
            "message": {"type": "string"},
            "schedules": {
                "type": "array",
                "maxItems": 30,
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "schedule_type": {
                            "type": "string",
                            "enum": ["one_time", "weekdays", "daily", "weekly"],
                        },
                        "days": {"type": "string"},
                        "start_date": {"type": ["string", "null"]},
                        "end_date": {"type": ["string", "null"]},
                        "time_local": {"type": "string"},
                        "action": {"type": "string", "enum": ["set", "on", "off"]},
                        "mode": {
                            "type": ["string", "null"],
                            "enum": ["auto", "cool", "heat", "dry", "fan", None],
                        },
                        "temperature": {"type": ["number", "null"]},
                        "fan": {
                            "type": ["string", "null"],
                            "enum": ["Auto", "Low", "Medium", "High", None],
                        },
                        "enabled": {"type": "boolean"},
                    },
                    "required": [
                        "name",
                        "schedule_type",
                        "days",
                        "start_date",
                        "end_date",
                        "time_local",
                        "action",
                        "mode",
                        "temperature",
                        "fan",
                        "enabled",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["needs_clarification", "message", "schedules"],
        "additionalProperties": False,
    }

    instructions = f"""You convert plain-English HVAC schedule requests into a PREVIEW for NetHome AC Control.
Current local date/time: {now.strftime('%A, %Y-%m-%d %I:%M %p %Z')}.
Timezone: {settings.weather_timezone}.

Important behavior:
- Never execute anything. Return only structured schedule previews.
- A temperature in a SET schedule means desired ROOM temperature for Smart Room Control.
- If mode is not specified for a temperature schedule, use mode "auto".
- If fan is not specified for SET, use "Auto".
- Use 24-hour HH:MM for time_local.
- one_time requires start_date YYYY-MM-DD and end_date equal to start_date.
- weekdays means Monday-Friday and days must be "Mon,Tue,Wed,Thu,Fri".
- daily days must be "Mon,Tue,Wed,Thu,Fri,Sat,Sun".
- weekly days must use comma-separated Mon,Tue,Wed,Thu,Fri,Sat,Sun tokens.
- For ON or OFF, mode, temperature, and fan must be null.
- Keep generated names concise and customer-readable.
- If the user's intended date/day/time or requested temperature is genuinely ambiguous, return
  needs_clarification=true, explain the missing detail in message, and return schedules=[].
- Do not invent extra schedule events that were not requested.
- Interpret relative dates such as today/tomorrow using the current local date above.
"""

    payload = {
        "model": settings.openai_model,
        "instructions": instructions,
        "input": prompt,
        "max_output_tokens": 1800,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "nethome_schedule_preview",
                "strict": True,
                "schema": schema,
            }
        },
    }

    try:
        req = UrlRequest(
            "https://api.openai.com/v1/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {settings.openai_api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urlopen(req, timeout=20) as resp:
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

        parsed = json.loads(output_text)
        if not isinstance(parsed, dict):
            raise ValueError("Schedule assistant returned an invalid object")
        parsed["ok"] = not bool(parsed.get("needs_clarification"))
        schedules = parsed.get("schedules")
        if not isinstance(schedules, list):
            parsed["schedules"] = []
        add_activity(
            "schedule-ai",
            "preview",
            "clarify" if parsed.get("needs_clarification") else "success",
            f"prompt={prompt[:180]} count={len(parsed.get('schedules') or [])}",
        )
        return parsed
    except Exception as exc:
        add_activity("schedule-ai", "preview", "error", str(exc)[:500])
        return {
            "ok": False,
            "needs_clarification": True,
            "message": "I could not interpret that schedule yet. Please try a clearer date, time, and temperature.",
            "schedules": [],
            "error": str(exc)[:300],
        }
