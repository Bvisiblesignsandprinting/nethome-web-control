from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any
from urllib.request import Request as UrlRequest, urlopen
from zoneinfo import ZoneInfo

from .config import settings
from .db import (
    add_activity,
    create_schedule,
    delete_schedule,
    get_latest_device_state,
    list_schedules,
    save_device_state,
    update_schedule,
)
from .midea_client import midea
from .weather import comfort_recommendation, current_weather, forecast_range, forecast_summary

HELP_TEXT = """NetHome AC Control:
Just text what you want in normal English.

Examples:
What's the AC status?
Turn it on.
Make it 72 degrees.
Put it on cool.
Make it a little colder.
Set the fan to high.
What's the weather tomorrow?
What temperature do you recommend tonight?
Turn the AC off at 11 PM.
Set up my AC schedule for tomorrow.
What's my schedule?

Short commands also work: STATUS, ON, OFF, COOL 72, HEAT 70, FAN AUTO.

If I'm not sure what you mean, I'll ask before changing the AC."""


def _short_error(exc: Exception) -> str:
    text = str(exc).strip()
    low = text.lower()
    if "3123" in text or "offline" in low:
        return "AC is offline."
    if "locked" in low:
        return "AC control is locked."
    if "timeout" in low or "timed out" in low:
        return "AC cloud is slow right now. Please try again."
    return f"AC error: {text[:120]}"


def _status_reply(result: dict[str, Any]) -> str:
    mode_names = {1: "Auto", 2: "Cool", 3: "Dry", 4: "Heat", 5: "Fan"}
    mode_code = result.get("mode")

    def _f(c):
        return None if c is None else round((float(c) * 9 / 5) + 32)

    power = "ON" if result.get("running") else "OFF"
    mode = mode_names.get(mode_code, f"Mode {mode_code}")
    target = _f(result.get("target_temperature_c"))
    indoor = _f(result.get("indoor_temperature_c"))
    fan = result.get("fan_speed")
    parts = [power, mode]
    if target is not None:
        parts.append(f"{target}F")
    if indoor is not None:
        parts.append(f"Room {indoor}F")
    if fan is not None:
        parts.append(f"Fan {fan}%")
    return " | ".join(parts)


def _execute(payload: dict[str, Any], label: str) -> dict[str, Any]:
    try:
        result = midea.command(payload)
        save_device_state(result, True, source="cloud")
        add_activity("sms", "device_command", "success", f"{label} verified={result.get('verified', False)}")
        return {"ok": True, "reply": f"Done: {label}.", "result": result}
    except Exception as exc:
        add_activity("sms", "device_command", "error", f"{payload}: {exc}")
        return {"ok": False, "reply": _short_error(exc)}



def _schedule_next_occurrence(schedule: dict[str, Any]) -> datetime | None:
    tz_name = str(schedule.get("timezone") or "America/New_York")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("America/New_York")
    now = datetime.now(tz)
    time_text = str(schedule.get("time_local") or "00:00")
    try:
        hour, minute = [int(x) for x in time_text[:5].split(":")]
    except Exception:
        return None

    start_s = str(schedule.get("start_date") or "")
    end_s = str(schedule.get("end_date") or "")
    start_date = datetime.fromisoformat(start_s).date() if start_s else None
    end_date = datetime.fromisoformat(end_s).date() if end_s else None
    schedule_type = str(schedule.get("schedule_type") or "weekly")
    selected = {d.strip() for d in str(schedule.get("days") or "").split(",") if d.strip()}
    day_names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

    for offset in range(0, 370):
        day = (now + timedelta(days=offset)).date()
        if start_date and day < start_date:
            continue
        if end_date and day > end_date:
            break

        if schedule_type == "one_time":
            if not start_date or day != start_date:
                continue
        elif schedule_type == "weekdays":
            if day.weekday() > 4:
                continue
        elif schedule_type == "daily":
            pass
        elif schedule_type == "weekly":
            if day_names[day.weekday()] not in selected:
                continue
        else:
            continue

        candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
        if candidate >= now:
            return candidate
    return None


def _schedule_entries() -> list[tuple[dict[str, Any], datetime | None]]:
    entries = []
    for row in list_schedules():
        nxt = _schedule_next_occurrence(row)
        entries.append((row, nxt))
    entries.sort(
        key=lambda item: (
            0 if item[0].get("enabled") else 1,
            item[1] is None,
            item[1] or datetime.max.replace(tzinfo=ZoneInfo("UTC")),
            int(item[0].get("id") or 0),
        )
    )
    return entries


def _schedule_command_text(schedule: dict[str, Any]) -> str:
    action = str(schedule.get("action") or "set").lower()
    if action == "off":
        return "OFF"
    if action == "on":
        return "ON"
    pieces = []
    mode = schedule.get("mode")
    temp = schedule.get("temperature")
    fan = schedule.get("fan")
    if mode:
        pieces.append(str(mode).upper())
    if temp is not None:
        pieces.append(f"{float(temp):g}F")
    if fan:
        pieces.append(f"Fan {str(fan).title()}")
    return " ".join(pieces) if pieces else "SET"


def _schedule_list_reply() -> str:
    entries = _schedule_entries()
    if not entries:
        return (
            "No schedules. Add one like: "
            "ADD SCHEDULE DAILY 8:00 AM HEAT 68"
        )
    lines = ["Upcoming schedules:"]
    for i, (row, nxt) in enumerate(entries[:8], 1):
        state = "" if row.get("enabled") else " [DISABLED]"
        when = nxt.strftime("%a %b %-d, %-I:%M %p") if nxt else "No upcoming run"
        lines.append(f"{i}) {when} - {_schedule_command_text(row)}{state}")
    lines.append("Change one with: SCHEDULE 1 HEAT 68, SCHEDULE 1 TIME 8:30 PM, or SCHEDULE 1 OFF.")
    return "\n".join(lines)


def _schedule_by_number(number: int) -> tuple[dict[str, Any], datetime | None] | None:
    entries = _schedule_entries()
    if number < 1 or number > len(entries):
        return None
    return entries[number - 1]


def _parse_sms_time(text: str) -> str | None:
    match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(AM|PM)", text.strip(), re.I)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if hour < 1 or hour > 12 or minute > 59:
        return None
    if match.group(3).upper() == "PM" and hour != 12:
        hour += 12
    if match.group(3).upper() == "AM" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}"


def _parse_schedule_action(instruction: str) -> dict[str, Any] | None:
    text = instruction.strip().upper()
    if text in {"OFF", "ON"}:
        return {
            "action": text.lower(),
            "mode": None,
            "temperature": None,
            "fan": None,
        }

    m = re.fullmatch(r"(COOL|HEAT)\s+(\d{2}(?:\.\d)?)", text)
    if m:
        temp = float(m.group(2))
        if temp < 50 or temp > 90:
            return None
        return {
            "action": "set",
            "mode": m.group(1).lower(),
            "temperature": temp,
            "fan": None,
        }

    temp_m = re.fullmatch(r"(?:TEMP|SET)\s+(\d{2}(?:\.\d)?)", text)
    if temp_m:
        temp = float(temp_m.group(1))
        if temp < 50 or temp > 90:
            return None
        return {
            "action": "set",
            "mode": None,
            "temperature": temp,
            "fan": None,
        }

    fan_m = re.fullmatch(r"FAN\s+(AUTO|LOW|MEDIUM|HIGH)", text)
    if fan_m:
        return {
            "action": "set",
            "mode": None,
            "temperature": None,
            "fan": fan_m.group(1).lower(),
        }

    return None


def _resolve_week_date(day_text: str, tz: ZoneInfo) -> datetime.date | None:
    key = day_text.strip().upper()
    today = datetime.now(tz).date()

    if key == "TODAY":
        return today
    if key == "TOMORROW":
        return today + timedelta(days=1)

    try:
        return datetime.fromisoformat(key).date()
    except ValueError:
        pass

    day_map = {
        "MON": 0, "MONDAY": 0,
        "TUE": 1, "TUES": 1, "TUESDAY": 1,
        "WED": 2, "WEDNESDAY": 2,
        "THU": 3, "THUR": 3, "THURS": 3, "THURSDAY": 3,
        "FRI": 4, "FRIDAY": 4,
        "SAT": 5, "SATURDAY": 5,
        "SUN": 6, "SUNDAY": 6,
    }
    if key not in day_map:
        return None

    delta = (day_map[key] - today.weekday()) % 7
    return today + timedelta(days=delta)


def _handle_week_batch(raw_command: str) -> dict[str, Any] | None:
    normalized = raw_command.strip()
    m = re.match(r"^(?:WEEK|WEEK SCHEDULE|SCHEDULE WEEK)\s*:\s*(.+)$", normalized, re.I | re.S)
    if not m:
        return None

    body = m.group(1).strip()
    parts = [p.strip() for p in re.split(r"[;\n]+", body) if p.strip()]
    if not parts:
        return {
            "ok": False,
            "reply": "No entries found. Example: WEEK: TUE 11:00 AM HEAT 69; WED 8:00 AM HEAT 68",
        }
    if len(parts) > 40:
        return {"ok": False, "reply": "Too many entries in one text. Maximum is 40."}

    tz = ZoneInfo("America/New_York")
    now = datetime.now(tz)
    parsed: list[dict[str, Any]] = []
    errors: list[str] = []
    skipped_past: list[str] = []

    day_aliases = {
        "TODAY", "TOMORROW",
        "MON", "MONDAY", "TUE", "TUES", "TUESDAY", "WED", "WEDNESDAY",
        "THU", "THUR", "THURS", "THURSDAY", "FRI", "FRIDAY",
        "SAT", "SATURDAY", "SUN", "SUNDAY",
    }

    last_day_text: str | None = None
    for idx, raw_part in enumerate(parts, 1):
        part = raw_part.strip()

        # Strip common list markers without depending on the schedule parser.
        while part and part[0] in "-*•":
            part = part[1:].lstrip()
        first_token, sep, remainder = part.partition(" ")
        if first_token.rstrip(".)").isdigit() and sep:
            part = remainder.strip()

        tokens = part.replace("@", " ").split()
        if not tokens:
            errors.append(f"{idx}: empty entry")
            continue

        first = tokens[0].strip(":,-").upper()
        looks_like_iso_date = len(first) == 10 and first[4:5] == "-" and first[7:8] == "-"
        has_day = first in day_aliases or looks_like_iso_date

        if has_day:
            if len(tokens) < 4:
                errors.append(f"{idx}: couldn't read '{part[:45]}'")
                continue
            day_text = first
            time_text = tokens[1].strip(":,-") + " " + tokens[2].strip(":,-")
            action_tokens = tokens[3:]
            last_day_text = day_text
        else:
            if last_day_text is None or len(tokens) < 3:
                errors.append(f"{idx}: couldn't read '{part[:45]}'")
                continue
            day_text = last_day_text
            time_text = tokens[0].strip(":,-") + " " + tokens[1].strip(":,-")
            action_tokens = tokens[2:]

        while action_tokens and action_tokens[0] in {"-", ":", "@"}:
            action_tokens = action_tokens[1:]
        action_text = " ".join(action_tokens).strip()
        if not action_text:
            errors.append(f"{idx}: missing action in '{part[:45]}'")
            continue

        run_date = _resolve_week_date(day_text, tz)
        time_local = _parse_sms_time(time_text)
        action = _parse_schedule_action(action_text)
        if not run_date or not time_local or not action:
            errors.append(f"{idx}: invalid date/time/action in '{part[:45]}'")
            continue

        hh, mm = [int(x) for x in time_local.split(":")]
        run_at = datetime(run_date.year, run_date.month, run_date.day, hh, mm, tzinfo=tz)

        if run_date < now.date() or run_date > now.date() + timedelta(days=6):
            errors.append(f"{idx}: {run_date.isoformat()} is outside the next 7 days")
            continue
        if run_at <= now:
            skipped_past.append(f"{day_text.upper()} {time_text.upper()}")
            continue

        parsed.append({
            "name": f"SMS week {run_date.isoformat()} {time_text.upper()}",
            "schedule_type": "one_time",
            "days": "",
            "start_date": run_date.isoformat(),
            "end_date": run_date.isoformat(),
            "time_local": time_local,
            "action": action["action"],
            "mode": action["mode"],
            "temperature": action["temperature"],
            "fan": action["fan"],
            "enabled": True,
        })

    if errors:
        return {
            "ok": False,
            "reply": (
                "Week schedule not saved because some entries need fixing:\\n"
                + "\\n".join(errors[:8])
                + "\\nFix only those lines and resend the same schedule."
            ),
        }

    if not parsed:
        if skipped_past:
            return {
                "ok": False,
                "reply": "No future entries were saved. All valid entries in that WEEK message have already passed."
            }
        return {"ok": False, "reply": "No valid week schedule entries found."}

    created = [create_schedule(item) for item in parsed]
    lines = [f"Saved {len(created)} one-time schedules for the next 7 days:"]
    if skipped_past:
        lines.append(f"Skipped {len(skipped_past)} already-passed entr{'y' if len(skipped_past) == 1 else 'ies'}: " + ", ".join(skipped_past[:4]) + ("..." if len(skipped_past) > 4 else ""))
    for i, row in enumerate(created[:12], 1):
        date_text = str(row.get("start_date") or "")
        try:
            d = datetime.fromisoformat(date_text).strftime("%a %b %-d")
        except Exception:
            d = date_text
        lines.append(
            f"{i}) {d} {datetime.strptime(str(row['time_local'])[:5], '%H:%M').strftime('%-I:%M %p')} - {_schedule_command_text(row)}"
        )
    if len(created) > 12:
        lines.append(f"...and {len(created) - 12} more.")
    lines.append("Text SCHEDULE to see the upcoming numbered list.")
    return {"ok": True, "reply": "\n".join(lines)}


def _handle_schedule_command(command: str, raw_command: str | None = None) -> dict[str, Any] | None:
    batch = _handle_week_batch(raw_command or command)
    if batch is not None:
        return batch

    if command in {"SCHEDULE", "SCHEDULES"}:
        return {"ok": True, "reply": _schedule_list_reply()}

    detail = re.fullmatch(r"SCHEDULE\s+(\d+)", command)
    if detail:
        item = _schedule_by_number(int(detail.group(1)))
        if not item:
            return {"ok": False, "reply": "Schedule number not found. Text SCHEDULE to refresh the list."}
        row, nxt = item
        when = nxt.strftime("%a %b %-d, %-I:%M %p") if nxt else "No upcoming run"
        return {
            "ok": True,
            "reply": (
                f"Schedule {detail.group(1)}: {row.get('name') or 'Unnamed'} | "
                f"{when} | {_schedule_command_text(row)} | "
                f"{'Enabled' if row.get('enabled') else 'Disabled'}. "
                "Change with HEAT 68, COOL 75, OFF, ON, TIME 8:30 PM, FAN HIGH, ENABLE, DISABLE, or DELETE."
            ),
        }

    change = re.fullmatch(r"SCHEDULE\s+(\d+)\s+(.+)", command)
    if change:
        num = int(change.group(1))
        instruction = change.group(2).strip()
        item = _schedule_by_number(num)
        if not item:
            return {"ok": False, "reply": "Schedule number not found. Text SCHEDULE to refresh the list."}
        row, _ = item
        sid = int(row["id"])

        if instruction == "DELETE":
            delete_schedule(sid)
            return {"ok": True, "reply": f"Deleted schedule {num}.\n{_schedule_list_reply()}"}
        if instruction in {"ENABLE", "DISABLE"}:
            updated = update_schedule(sid, {"enabled": instruction == "ENABLE"})
            return {"ok": True, "reply": f"Schedule {num} {'enabled' if updated and updated.get('enabled') else 'disabled'}."}
        if instruction in {"OFF", "ON"}:
            update_schedule(sid, {"action": instruction.lower(), "mode": None, "temperature": None, "fan": None})
            return {"ok": True, "reply": f"Schedule {num} changed to {instruction}."}

        t = re.fullmatch(r"TIME\s+(.+)", instruction)
        if t:
            parsed = _parse_sms_time(t.group(1))
            if not parsed:
                return {"ok": False, "reply": "Use a time like: SCHEDULE 1 TIME 8:30 PM"}
            update_schedule(sid, {"time_local": parsed})
            return {"ok": True, "reply": f"Schedule {num} time updated to {t.group(1).upper()}."}

        m = re.fullmatch(r"(COOL|HEAT)\s+(\d{2}(?:\.\d)?)", instruction)
        if m:
            temp = float(m.group(2))
            if temp < 50 or temp > 90:
                return {"ok": False, "reply": "Temperature must be 50-90F."}
            update_schedule(
                sid,
                {"action": "set", "mode": m.group(1).lower(), "temperature": temp},
            )
            return {"ok": True, "reply": f"Schedule {num} changed to {m.group(1)} {temp:g}F."}

        temp_m = re.fullmatch(r"(?:TEMP|SET)\s+(\d{2}(?:\.\d)?)", instruction)
        if temp_m:
            temp = float(temp_m.group(1))
            if temp < 50 or temp > 90:
                return {"ok": False, "reply": "Temperature must be 50-90F."}
            update_schedule(sid, {"action": "set", "temperature": temp})
            return {"ok": True, "reply": f"Schedule {num} temperature changed to {temp:g}F."}

        fan_m = re.fullmatch(r"FAN\s+(AUTO|LOW|MEDIUM|HIGH)", instruction)
        if fan_m:
            update_schedule(sid, {"action": "set", "fan": fan_m.group(1).lower()})
            return {"ok": True, "reply": f"Schedule {num} fan changed to {fan_m.group(1)}."}

        return {"ok": False, "reply": "Unknown schedule change. Text SCHEDULE 1 for examples."}

    add = re.fullmatch(
        r"ADD\s+SCHEDULE\s+([A-Z,]+)\s+(\d{1,2}(?::\d{2})?\s*(?:AM|PM))\s+(.+)",
        command,
    )
    if add:
        repeat = add.group(1)
        time_local = _parse_sms_time(add.group(2))
        instruction = add.group(3).strip()
        if not time_local:
            return {"ok": False, "reply": "Use a time like 8:00 AM."}

        if repeat == "DAILY":
            schedule_type, days = "daily", "Mon,Tue,Wed,Thu,Fri,Sat,Sun"
        elif repeat == "WEEKDAYS":
            schedule_type, days = "weekdays", "Mon,Tue,Wed,Thu,Fri"
        else:
            day_map = {
                "MON": "Mon", "TUE": "Tue", "WED": "Wed", "THU": "Thu",
                "FRI": "Fri", "SAT": "Sat", "SUN": "Sun",
            }
            raw_days = repeat.split(",")
            if not raw_days or any(d not in day_map for d in raw_days):
                return {"ok": False, "reply": "Use DAILY, WEEKDAYS, or days like MON,WED,FRI."}
            schedule_type, days = "weekly", ",".join(day_map[d] for d in raw_days)

        payload: dict[str, Any] = {
            "name": f"SMS {repeat} {add.group(2).upper()}",
            "schedule_type": schedule_type,
            "days": days,
            "start_date": None,
            "end_date": None,
            "time_local": time_local,
            "action": "set",
            "mode": None,
            "temperature": None,
            "fan": None,
            "enabled": True,
        }

        if instruction in {"OFF", "ON"}:
            payload["action"] = instruction.lower()
        else:
            m = re.fullmatch(r"(COOL|HEAT)\s+(\d{2}(?:\.\d)?)", instruction)
            if not m:
                return {
                    "ok": False,
                    "reply": "Add format: ADD SCHEDULE DAILY 8:00 AM HEAT 68 (or COOL 75 / ON / OFF).",
                }
            temp = float(m.group(2))
            if temp < 50 or temp > 90:
                return {"ok": False, "reply": "Temperature must be 50-90F."}
            payload["mode"] = m.group(1).lower()
            payload["temperature"] = temp

        create_schedule(payload)
        return {"ok": True, "reply": "Schedule added.\n" + _schedule_list_reply()}

    return None


def _weather_command_reply(command: str) -> dict[str, Any] | None:
    natural = command.lower()
    if "weather" not in natural:
        return None
    try:
        if re.search(r"\bnow\b", natural):
            cur = current_weather()
            today = forecast_summary("today")
            return {
                "ok": True,
                "reply": (
                    f"Now {cur['temperature_f']}F, feels {cur['feels_like_f']}F, "
                    f"wind {cur['wind_mph']} mph. Today high {today['high_f']}F, "
                    f"low {today['low_f']}F, rain {today['rain_chance']}%."
                ),
            }

        if re.search(r"\bweek\b", natural):
            count = 7
            forecasts = forecast_range(count)
            lines = [
                f"{datetime.fromisoformat(f['date']).strftime('%a')}: {f['high_f']}/{f['low_f']}F rain {f['rain_chance']}%"
                for f in forecasts
            ]
            return {"ok": True, "reply": "7-day forecast:\n" + "\n".join(lines)}

        days_match = re.search(r"\b([2-8])\s*days?\b", natural)
        if days_match:
            count = int(days_match.group(1))
            forecasts = forecast_range(count)
            lines = [
                f"{datetime.fromisoformat(f['date']).strftime('%a')}: {f['high_f']}/{f['low_f']}F rain {f['rain_chance']}%"
                for f in forecasts
            ]
            return {"ok": True, "reply": f"{count}-day forecast:\n" + "\n".join(lines)}

        day = "today"
        for token in ["tomorrow", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]:
            if token in natural:
                day = token
                break
        date_match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", command)
        if date_match:
            day = date_match.group(1)

        rec = comfort_recommendation(day)
        f = rec["forecast"]
        label = datetime.fromisoformat(f["date"]).strftime("%a %b %-d")
        reply = f"{label}: high {f['high_f']}F, low {f['low_f']}F, rain {f['rain_chance']}%."
        if any(word in natural for word in ["recommend", "should", "set", "based"]):
            reply += f" Recommend {str(rec['recommended_mode']).upper()} {rec['recommended_temperature']}F."
        if any(p in natural for p in ["set it", "set the ac", "do it", "based on"]):
            payload = {
                "action": "set",
                "mode": rec["recommended_mode"],
                "temperature": rec["recommended_temperature"],
            }
            result = _execute(payload, f"{str(rec['recommended_mode']).upper()} {rec['recommended_temperature']}F")
            result["reply"] = reply + " " + result["reply"]
            return result
        return {"ok": True, "reply": reply}
    except Exception as exc:
        return {"ok": False, "reply": f"Weather lookup failed: {str(exc)[:120]}"}


def _ai_interpret(raw_text: str) -> dict[str, Any] | None:
    """Translate natural NetHome SMS into a safe existing command or clarification."""
    if not settings.openai_api_key:
        return None

    cached = get_latest_device_state()
    state_text = "Current AC state unavailable."
    if cached and cached.get("state"):
        try:
            state_text = "Current AC state: " + _status_reply(cached["state"])
        except Exception:
            pass

    schema = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["command", "clarify", "answer"]},
            "command": {"type": ["string", "null"]},
            "reply": {"type": "string"},
        },
        "required": ["kind", "command", "reply"],
        "additionalProperties": False,
    }
    instructions = f"""You are the natural-language interpreter for a private NetHome AC Control SMS system.
The authorized owner is texting their own HVAC controller. Keep replies short enough for SMS.

{state_text}

Return a command only when the user's intent is clear. Never guess a temperature, time, date, mode, or schedule.
If a request could cause an HVAC change and an important detail is ambiguous, use kind=clarify and ask one short question.
For harmless informational questions, you may map them to an existing command.
For unrelated general questions, use kind=answer and briefly say this SMS assistant is for AC, schedules, and weather.

Allowed canonical commands:
STATUS
MODE
ON
OFF
TEMP <50-90>
COOL <50-90>
HEAT <50-90>
FAN AUTO|LOW|MEDIUM|HIGH
WEATHER
WEATHER NOW
WEATHER TOMORROW
WEATHER <weekday>
WEATHER <2-8> DAYS
WEATHER WEEK
WEATHER TOMORROW RECOMMEND
SCHEDULE
SCHEDULE <number>
SCHEDULE <number> HEAT <50-90>
SCHEDULE <number> COOL <50-90>
SCHEDULE <number> OFF
SCHEDULE <number> ON
SCHEDULE <number> TIME <time AM/PM>
SCHEDULE <number> FAN AUTO|LOW|MEDIUM|HIGH
SCHEDULE <number> ENABLE|DISABLE|DELETE
ADD SCHEDULE DAILY <time AM/PM> HEAT|COOL <50-90>
ADD SCHEDULE WEEKDAYS <time AM/PM> HEAT|COOL <50-90>
ADD SCHEDULE <MON,WED,...> <time AM/PM> HEAT|COOL <50-90>
WEEK: <TODAY|TOMORROW|weekday|YYYY-MM-DD> <time AM/PM> <HEAT n|COOL n|ON|OFF>; ...

Examples:
"what is the AC doing" -> STATUS
"turn it on" -> ON
"make it 72" -> TEMP 72
"put it on cool at 72" -> COOL 72
"make it a little colder" -> clarify; ask whether to lower by 2F or use a specific temperature
"weather tomorrow" -> WEATHER TOMORROW
"what should I set it to tomorrow" -> WEATHER TOMORROW RECOMMEND
"turn it off tomorrow at 11 pm" -> WEEK: TOMORROW 11:00 PM OFF
"what's my schedule" -> SCHEDULE
"set schedule 2 to heat 69" -> SCHEDULE 2 HEAT 69
"""

    payload = {
        "model": settings.openai_model,
        "instructions": instructions,
        "input": raw_text,
        "max_output_tokens": 180,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "nethome_sms_intent",
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
        with urlopen(req, timeout=15) as resp:
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
            return None
        parsed = json.loads(output_text)
        if not isinstance(parsed, dict):
            return None
        return parsed
    except Exception as exc:
        add_activity("sms", "ai_interpret", "error", str(exc)[:300])
        return None


def _safe_ai_command(command: str) -> bool:
    text = " ".join((command or "").upper().split())
    patterns = [
        r"STATUS", r"MODE", r"ON", r"OFF",
        r"(?:TEMP|SET|TEMPERATURE)\s+\d{2}(?:\.\d)?",
        r"(?:COOL|HEAT)\s+\d{2}(?:\.\d)?",
        r"FAN\s+(?:AUTO|LOW|MEDIUM|HIGH)",
        r"WEATHER(?:\s+(?:NOW|TODAY|TOMORROW|MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY|WEEK))?(?:\s+RECOMMEND)?",
        r"WEATHER\s+[2-8]\s+DAYS?",
        r"SCHEDULES?",
        r"SCHEDULE\s+\d+(?:\s+(?:HEAT\s+\d{2}(?:\.\d)?|COOL\s+\d{2}(?:\.\d)?|OFF|ON|TIME\s+\d{1,2}(?::\d{2})?\s*(?:AM|PM)|FAN\s+(?:AUTO|LOW|MEDIUM|HIGH)|ENABLE|DISABLE|DELETE))?",
        r"ADD\s+SCHEDULE\s+(?:DAILY|WEEKDAYS|(?:MON|TUE|WED|THU|FRI|SAT|SUN)(?:,(?:MON|TUE|WED|THU|FRI|SAT|SUN))*)\s+\d{1,2}(?::\d{2})?\s*(?:AM|PM)\s+(?:(?:HEAT|COOL)\s+\d{2}(?:\.\d)?|ON|OFF)",
        r"(?:WEEK|WEEK SCHEDULE|SCHEDULE WEEK)\s*:\s+.+",
    ]
    return any(re.fullmatch(pattern, text, re.I | re.S) for pattern in patterns)


def _ai_fallback(raw_text: str) -> dict[str, Any] | None:
    interpreted = _ai_interpret(raw_text)
    if not interpreted:
        return None

    kind = str(interpreted.get("kind") or "")
    reply = str(interpreted.get("reply") or "").strip()
    ai_command = str(interpreted.get("command") or "").strip()

    if kind == "clarify":
        return {"ok": True, "reply": reply or "What exactly would you like me to change?"}
    if kind == "answer":
        return {"ok": True, "reply": reply or "I can help with the AC, schedules, and weather."}
    if kind == "command" and ai_command and _safe_ai_command(ai_command):
        result = process_text_command(ai_command)
        if result.get("ok"):
            add_activity("sms", "ai_interpret", "success", f"{raw_text[:120]} -> {ai_command[:120]}")
        return result
    return None


def process_text_command(raw: str) -> dict[str, Any]:
    raw_text = (raw or "").strip()
    command = " ".join(raw_text.upper().split())
    if not command:
        return {"ok": False, "reply": HELP_TEXT}

    add_activity("sms", "message_received", "received", raw_text[:500])
    natural = command.lower()

    if command in {"HELP", "?", "COMMANDS"}:
        return {"ok": True, "reply": HELP_TEXT}

    if command in {"START", "YES"}:
        return {
            "ok": True,
            "reply": "NetHome AC Control ready. " + HELP_TEXT,
        }

    if command == "STOP":
        return {
            "ok": True,
            "reply": "NetHome AC Control: messaging stopped. Text START to use it again.",
        }

    schedule_result = _handle_schedule_command(command, raw_text)
    if schedule_result is not None:
        return schedule_result

    weather_result = _weather_command_reply(command)
    if weather_result is not None:
        return weather_result

    if any(p in natural for p in ["what should i set", "what should the ac", "make it comfortable"]):
        try:
            rec = comfort_recommendation("today")
            f = rec["forecast"]
            return {
                "ok": True,
                "reply": (
                    f"Today {f['high_f']}F/{f['low_f']}F, rain {f['rain_chance']}%. "
                    f"Recommend {rec['recommended_mode']} {rec['recommended_temperature']}F."
                ),
            }
        except Exception as exc:
            return {"ok": False, "reply": f"Weather lookup failed: {str(exc)[:120]}"}

    # Common conversational phrases work even when the optional AI fallback is not configured.
    if any(p in natural for p in ["what's the ac status", "what is the ac status", "ac status", "what's the ac doing", "what is the ac doing"]):
        command = "STATUS"

    if any(p in natural for p in ["what's my schedule", "what is my schedule", "show my schedule", "show schedule"]):
        command = "SCHEDULE"
        schedule_result = _handle_schedule_command(command, raw_text)
        if schedule_result is not None:
            return schedule_result

    if any(p in natural for p in ["a little colder", "a bit colder", "make it colder"]):
        return {
            "ok": True,
            "reply": "Sure. Do you want me to lower the current temperature by 2F, or set a specific temperature?",
        }
    if any(p in natural for p in ["a little warmer", "a bit warmer", "make it warmer"]):
        return {
            "ok": True,
            "reply": "Sure. Do you want me to raise the current temperature by 2F, or set a specific temperature?",
        }

    # A future time in a power request must never be mistaken for an immediate ON/OFF.
    timed_power = re.search(
        r"\b(?:turn|switch|shut)\b.*?\b(on|off)\b.*?\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b",
        natural,
    )
    if timed_power:
        action = timed_power.group(1).upper()
        hour = timed_power.group(2)
        minute = timed_power.group(3) or "00"
        ampm = timed_power.group(4).upper()
        day = None
        if "tomorrow" in natural:
            day = "TOMORROW"
        elif "today" in natural or "tonight" in natural:
            day = "TODAY"
        if day:
            scheduled = _handle_schedule_command(
                f"WEEK: {day} {hour}:{minute} {ampm} {action}",
                f"WEEK: {day} {hour}:{minute} {ampm} {action}",
            )
            if scheduled is not None:
                return scheduled
        return {
            "ok": True,
            "reply": f"Do you mean {action.lower()} today at {hour}:{minute} {ampm}?",
        }

    fan_natural = re.search(r"\bfan\b.*?\b(auto|low|medium|high)\b", natural)
    if fan_natural:
        command = f"FAN {fan_natural.group(1).upper()}"

    temp_natural = re.search(
        r"\b(?:make|set)(?:\s+(?:it|the ac|temperature))?\s+(?:to\s+)?(\d{2}(?:\.\d)?)\s*(?:degrees?|f)?\b",
        natural,
    )
    if temp_natural and not re.search(r"\b(?:cool|heat|heating|cooling)\b", natural):
        command = f"TEMP {temp_natural.group(1)}"

    mode_only = re.search(r"\b(?:put|set|switch)(?:\s+(?:it|the ac))?\s+(?:to|on)?\s*(cool|heat|dry|fan|auto)\b", natural)
    if mode_only and not re.search(r"\d{2}", natural):
        mode = mode_only.group(1)
        return _execute({"action": "set", "mode": mode}, f"MODE {mode.upper()}")

    off_phrases = ["turn it off", "turn off", "shut it off", "switch it off"]
    on_phrases = ["turn it on", "turn on", "switch it on"]
    if any(p in natural for p in off_phrases):
        command = "OFF"
    elif any(p in natural for p in on_phrases):
        command = "ON"
    else:
        temp_match = re.search(r"(cool|cooling|heat|heating).*?(\d{2})", natural)
        if not temp_match:
            reverse = re.search(r"(\d{2}).*?(cool|cooling|heat|heating)", natural)
            if reverse:
                command = f"{'COOL' if reverse.group(2).startswith('cool') else 'HEAT'} {reverse.group(1)}"
        else:
            command = f"{'COOL' if temp_match.group(1).startswith('cool') else 'HEAT'} {temp_match.group(2)}"

    if command in {"STATUS", "MODE"}:
        try:
            result = midea.status()
            save_device_state(result, True, source="cloud")
        except Exception as exc:
            cached = get_latest_device_state()
            if cached and cached.get("online") and cached.get("state"):
                result = cached["state"]
            else:
                return {"ok": False, "reply": _short_error(exc)}

        if command == "MODE":
            mode_names = {1: "Auto", 2: "Cool", 3: "Dry", 4: "Heat", 5: "Fan"}
            code = result.get("mode")
            return {"ok": True, "reply": f"Mode {code} ({mode_names.get(code, 'Unknown')})."}
        return {"ok": True, "reply": _status_reply(result)}

    if command == "OFF":
        return _execute({"action": "off"}, "OFF")

    if command == "ON":
        return _execute({"action": "on"}, "ON")

    temp_only = re.fullmatch(r"(?:TEMP|SET|TEMPERATURE)?\s*(\d{2}(?:\.\d)?)", command)
    if temp_only:
        temp = float(temp_only.group(1))
        return _execute({"action": "set", "temperature": temp}, f"TEMP {temp:g}F")

    match = re.fullmatch(r"(COOL|HEAT)\s+(\d{2}(?:\.\d)?)", command)
    if match:
        temp = float(match.group(2))
        payload = {
            "action": "set",
            "mode": match.group(1).lower(),
            "temperature": temp,
        }
        return _execute(payload, f"{match.group(1)} {temp:g}F")

    fan = re.fullmatch(r"FAN\s+(AUTO|LOW|MEDIUM|HIGH)", command)
    if fan:
        payload = {"action": "set", "fan": fan.group(1).lower()}
        return _execute(payload, f"FAN {fan.group(1)}")

    ai_result = _ai_fallback(raw_text)
    if ai_result is not None:
        return ai_result

    return {"ok": False, "reply": "I didn't understand that. Text HELP for examples, or say what you want in a different way."}
