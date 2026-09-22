from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

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

HELP_TEXT = """NetHome commands:
STATUS - live AC status
ON / OFF - power
TEMP 72 - change target temperature
COOL 72 / HEAT 68 - set mode + temperature
FAN AUTO|LOW|MEDIUM|HIGH
MODE - current mode
WEATHER - today
WEATHER NOW - current conditions
WEATHER TOMORROW / WEATHER FRIDAY
WEATHER 3 DAYS / WEATHER WEEK
WEATHER TOMORROW RECOMMEND
SCHEDULE - numbered schedule list
SCHEDULE 1 - details
SCHEDULE 1 HEAT 68 / COOL 75 / OFF / ON
SCHEDULE 1 TIME 8:30 PM
SCHEDULE 1 FAN HIGH
SCHEDULE 1 ENABLE / DISABLE / DELETE
ADD SCHEDULE DAILY 8:00 AM HEAT 68
ADD SCHEDULE WEEKDAYS 7:30 AM COOL 75
ADD SCHEDULE MON,WED,FRI 6:00 PM OFF
WEEK: TUE 11:00 AM HEAT 69; TUE 2:00 PM HEAT 68; WED 8:00 AM HEAT 69
  - creates all entries for the next 7 days in ONE text
  - use TODAY, TOMORROW, MON...SUN, or YYYY-MM-DD
HELP - show this list"""


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

    entry_re = re.compile(
        r"^(TODAY|TOMORROW|MON(?:DAY)?|TUE(?:S|SDAY)?|WED(?:NESDAY)?|THU(?:R|RS|RSDAY)?|FRI(?:DAY)?|SAT(?:URDAY)?|SUN(?:DAY)?|20\\d{2}-\\d{2}-\\d{2})"
        r"\\s*[:,-]?\\s+"
        r"(\\d{1,2}(?::\\d{2})?\\s*(?:AM|PM))"
        r"\\s*(?:-|:|@)?\\s+(.+)$",
        re.I,
    )
    time_only_re = re.compile(
        r"^(\\d{1,2}(?::\\d{2})?\\s*(?:AM|PM))\\s*(?:-|:|@)?\\s+(.+)$",
        re.I,
    )

    last_day_text: str | None = None
    for idx, raw_part in enumerate(parts, 1):
        part = re.sub(r"^\\s*(?:[-*•]+|\\d+[.)])\\s*", "", raw_part).strip()

        match = entry_re.fullmatch(part)
        if match:
            day_text = match.group(1)
            time_text = match.group(2)
            action_text = match.group(3)
            last_day_text = day_text
        else:
            time_match = time_only_re.fullmatch(part)
            if time_match and last_day_text:
                day_text = last_day_text
                time_text = time_match.group(1)
                action_text = time_match.group(2)
            else:
                errors.append(f"{idx}: couldn't read '{part[:45]}'")
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
            errors.append(f"{idx}: {day_text.upper()} {time_text.upper()} has already passed")
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
        return {"ok": False, "reply": "No valid week schedule entries found."}

    created = [create_schedule(item) for item in parsed]
    lines = [f"Saved {len(created)} one-time schedules for the next 7 days:"]
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

    return {"ok": False, "reply": f"Unknown command. {HELP_TEXT}"}
