from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.request import Request as UrlRequest, urlopen
from zoneinfo import ZoneInfo

from .config import settings
from .db import (
    add_activity,
    create_schedule,
    delete_schedule,
    enqueue_device_command_at,
    get_latest_device_state,
    list_schedules,
    save_device_state,
    update_schedule,
)
from .midea_client import midea
from .weather import comfort_recommendation, current_weather, forecast_range, forecast_summary

HELP_TEXT = """🏠 NetHome AC Control
Just text what you want.
Try:
• Check the AC status
• Make it 72°
• Cool to 74°
• Fan high
• Swing up and down
• Swing left and right
• Weather tomorrow?
• Best temperature tonight?
• Show my schedule
• Show the next 10 days
• Turn it off tomorrow at 11 PM
Shortcuts: STATUS • ON • OFF • COOL 72 • HEAT 70 • FAN AUTO
If a change isn't clear, I'll ask before changing the AC."""


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
    mode_icons = {1: "🔄", 2: "❄️", 3: "💧", 4: "🔥", 5: "💨"}
    mode_code = result.get("mode")

    def _f(c):
        return None if c is None else round((float(c) * 9 / 5) + 32)

    power = "🟢 On" if result.get("running") else "⚫ Off"
    mode = mode_names.get(mode_code, f"Mode {mode_code}")
    icon = mode_icons.get(mode_code, "🌡️")
    target = _f(result.get("target_temperature_c"))
    indoor = _f(result.get("indoor_temperature_c"))
    fan = result.get("fan_speed")

    lines = ["🌡️ AC Status", f"Power: {power}", f"Mode: {icon} {mode}"]
    if target is not None:
        lines.append(f"Set: {target}°F")
    if indoor is not None:
        lines.append(f"Room: {indoor}°F")
    if fan is not None:
        lines.append(f"Fan: {fan}%")
    vertical = result.get("vertical_swing")
    horizontal = result.get("horizontal_swing")
    if vertical is not None or horizontal is not None:
        v = "On" if vertical else "Off"
        h = "On" if horizontal else "Off"
        lines.append(f"Swing: ↕ {v} • ↔ {h}")
    return "\n".join(lines)


def _execute(payload: dict[str, Any], label: str) -> dict[str, Any]:
    try:
        result = midea.command(payload)
        save_device_state(result, True, source="cloud")
        add_activity("sms", "device_command", "success", f"{label} verified={result.get('verified', False)}")
        if result.get("verified") is False and result.get("verification_note"):
            requested_c = result.get("requested_temperature_c")
            requested_f = None
            if requested_c is not None:
                requested_f = round((float(requested_c) * 9.0 / 5.0) + 32.0)
            requested_line = f"Requested: {label}" if label else (
                f"Requested: {requested_f}°F" if requested_f is not None else "Requested change sent"
            )
            return {
                "ok": True,
                "reply": (
                    f"✅ Command sent\n"
                    f"{requested_line}\n"
                    f"Cloud status is still catching up.\n"
                    f"Last cloud reading:\n{_status_reply(result)}"
                ),
                "result": result,
            }
        return {
            "ok": True,
            "reply": f"✅ Updated\n{_status_reply(result)}",
            "result": result,
        }
    except Exception as exc:
        add_activity("sms", "device_command", "error", f"{payload}: {exc}")
        text = str(exc).lower()
        if "3123" in text or "offline" in text:
            retry_at = datetime.now(timezone.utc) + timedelta(minutes=5)
            retry_command = {
                **payload,
                "_retry_count": 1,
                "_retry_max": 12,
                "_retry_label": label,
            }
            enqueue_device_command_at("sms-retry", retry_command, retry_at)
            return {
                "ok": False,
                "reply": "⚠️ AC is offline. I saved this command and will retry every 5 minutes for up to 1 hour.",
                "retry_scheduled_for": retry_at.isoformat(),
            }
        return {"ok": False, "reply": f"⚠️ {_short_error(exc)}"}



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


def _schedule_list_reply(days: int | None = None) -> str:
    entries = _schedule_entries()
    if days is not None:
        tz = ZoneInfo("America/New_York")
        cutoff = datetime.now(tz) + timedelta(days=days)
        entries = [(row, nxt) for row, nxt in entries if nxt is not None and nxt <= cutoff]

    if not entries:
        if days is not None:
            return f"📅 No upcoming schedules in the next {days} days."
        return "📅 No schedules yet.\nTry: ADD SCHEDULE DAILY 8:00 AM HEAT 68"

    def _display_action(row: dict[str, Any]) -> str:
        action = str(row.get("action") or "set").lower()
        if action == "off":
            return "⏹️ Off"
        if action == "on":
            return "▶️ On"

        mode = str(row.get("mode") or "").lower()
        temp = row.get("temperature")
        fan = row.get("fan")
        mode_icon = {
            "heat": "🔥",
            "cool": "❄️",
            "dry": "💧",
            "fan": "💨",
            "auto": "🔄",
        }.get(mode, "🌡️")

        parts = []
        if mode:
            parts.append(mode.title())
        if temp is not None:
            parts.append(f"{float(temp):g}°F")
        if fan:
            parts.append(f"Fan {str(fan).title()}")
        return f"{mode_icon} " + " • ".join(parts or ["Set"])

    title = f"📅 Next {days} days" if days is not None else "📅 Upcoming schedule"
    lines = [title]
    limit = 20 if days is not None else 10
    visible = entries[:limit]
    last_day = None

    for i, (row, nxt) in enumerate(visible, 1):
        disabled = not row.get("enabled")
        if nxt:
            day_key = nxt.date()
            if day_key != last_day:
                lines.append(nxt.strftime("%a • %b %-d"))
                last_day = day_key
            status = " • ⏸️ Disabled" if disabled else ""
            lines.append(f"{i}. {nxt.strftime('%-I:%M %p')} • {_display_action(row)}{status}")
        else:
            if last_day != "disabled":
                lines.append("⏸️ Disabled")
                last_day = "disabled"
            lines.append(f"{i}. {_display_action(row)}")

    if len(entries) > limit:
        lines.append(f"…and {len(entries) - limit} more.")

    lines.extend([
        "✏️ Change one by number:",
        "SCHEDULE 1 HEAT 68",
        "SCHEDULE 1 TIME 8:30 PM",
        "SCHEDULE 1 OFF",
    ])
    return "\n".join(lines).rstrip()


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
                    f"🌤️ Weather now\n"
                    f"{cur['temperature_f']}°F • Feels {cur['feels_like_f']}°F\n"
                    f"💨 Wind {cur['wind_mph']} mph\n"
                    f"Today: {today['high_f']}° / {today['low_f']}° • 🌧️ {today['rain_chance']}%"
                ),
            }

        if re.search(r"\bweek\b", natural):
            count = 7
            forecasts = forecast_range(count)
            lines = [
                f"{datetime.fromisoformat(f['date']).strftime('%a')}: {f['high_f']}° / {f['low_f']}° • 🌧️ {f['rain_chance']}%"
                for f in forecasts
            ]
            return {"ok": True, "reply": "7-day forecast:\n" + "\n".join(lines)}

        days_match = re.search(r"\b([2-8])\s*days?\b", natural)
        if days_match:
            count = int(days_match.group(1))
            forecasts = forecast_range(count)
            lines = [
                f"{datetime.fromisoformat(f['date']).strftime('%a')}: {f['high_f']}° / {f['low_f']}° • 🌧️ {f['rain_chance']}%"
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
        reply = f"🌤️ {label}\nHigh {f['high_f']}° • Low {f['low_f']}°\n🌧️ Rain {f['rain_chance']}%"
        if any(word in natural for word in ["recommend", "should", "set", "based"]):
            mode = str(rec['recommended_mode']).title()
            icon = "🔥" if mode.lower() == "heat" else "❄️" if mode.lower() == "cool" else "🌡️"
            reply += f"\n💡 Suggested: {icon} {mode} {rec['recommended_temperature']}°F"
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
            "command": {"type": "string"},
            "reply": {"type": "string"},
        },
        "required": ["kind", "command", "reply"],
        "additionalProperties": False,
    }
    instructions = f"""You are the natural-language interpreter for a private NetHome AC Control SMS system.
The authorized owner is texting their own HVAC controller. Keep replies short enough for SMS.

{state_text}

Return a command only when the user's intent is clear. Never guess a temperature, time, date, mode, or schedule.
CRITICAL SAFETY RULE: if the user mentions a future time, day, "schedule", "later", "until", "today", "tomorrow", or "tonight", NEVER translate that into immediate ON or OFF. Use a schedule command, or clarify if the day/time is incomplete.
If the user says "keep/stay on until <time>", that means schedule OFF at that time; do not turn it off now.
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
SWING VERTICAL ON|OFF
SWING HORIZONTAL ON|OFF
SWING BOTH ON|OFF
WEATHER
WEATHER NOW
WEATHER TOMORROW
WEATHER <weekday>
WEATHER <2-8> DAYS
WEATHER WEEK
WEATHER TOMORROW RECOMMEND
SCHEDULE
SCHEDULE NEXT <1-30> DAYS
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
"swing up and down" -> SWING VERTICAL ON
"stop the up and down swing" -> SWING VERTICAL OFF
"swing left and right" -> SWING HORIZONTAL ON
"stop the left and right swing" -> SWING HORIZONTAL OFF
"swing both ways" -> SWING BOTH ON
"stop swinging" -> SWING BOTH OFF
"make it a little colder" -> clarify; ask whether to lower by 2F or use a specific temperature
"weather tomorrow" -> WEATHER TOMORROW
"what should I set it to tomorrow" -> WEATHER TOMORROW RECOMMEND
"turn it off tomorrow at 11 pm" -> WEEK: TOMORROW 11:00 PM OFF
"add to the schedule it should turn off 4:00 p.m. today" -> WEEK: TODAY 4:00 PM OFF
"keep it on until 4:00 p.m. today" -> WEEK: TODAY 4:00 PM OFF
"what's my schedule" -> SCHEDULE
"show me about ten days of schedules" -> SCHEDULE NEXT 10 DAYS
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
        r"SWING\s+(?:VERTICAL|HORIZONTAL|BOTH)\s+(?:ON|OFF)",
        r"WEATHER(?:\s+(?:NOW|TODAY|TOMORROW|MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY|WEEK))?(?:\s+RECOMMEND)?",
        r"WEATHER\s+[2-8]\s+DAYS?",
        r"SCHEDULES?",
        r"SCHEDULE\s+NEXT\s+(?:[1-9]|[12]\d|30)\s+DAYS?",
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



def _handle_natural_one_time_schedule(raw_text: str) -> dict[str, Any] | None:
    """Handle plain-English one-time schedule requests before immediate-command parsing."""
    natural = raw_text.lower()
    if not any(word in natural for word in ["schedule", "today", "tomorrow", "tonight", "later", " at "]):
        return None

    action_match = re.search(
        r"\b(?:turn|switch|start|power|shut|stop)(?:\s+(?:it|the ac))?\s+(on|off)\b",
        natural,
    )
    if not action_match:
        if re.search(r"\b(?:start|turn)\b", natural) and re.search(r"\bon\b", natural):
            action = "ON"
        elif re.search(r"\b(?:stop|shut|turn)\b", natural) and re.search(r"\boff\b", natural):
            action = "OFF"
        else:
            return None
    else:
        action = action_match.group(1).upper()

    time_match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", natural)
    if not time_match:
        return {
            "ok": True,
            "reply": "🕒 I understood this as a schedule request, but I need a time like 4:20 PM.",
        }

    hour = int(time_match.group(1))
    minute = int(time_match.group(2) or 0)
    ampm = time_match.group(3).upper()
    if hour < 1 or hour > 12 or minute > 59:
        return {"ok": False, "reply": "⚠️ That time does not look valid."}
    hour24 = hour % 12 + (12 if ampm == "PM" else 0)

    tz = ZoneInfo("America/New_York")
    now = datetime.now(tz)
    run_date = None

    mdY = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", natural)
    iso = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", natural)
    try:
        if mdY:
            month, day, year = [int(x) for x in mdY.groups()]
            run_date = datetime(year, month, day, tzinfo=tz).date()
        elif iso:
            run_date = datetime.fromisoformat(iso.group(1)).date()
        elif "tomorrow" in natural:
            run_date = now.date() + timedelta(days=1)
        elif any(word in natural for word in ["today", "tonight"]):
            run_date = now.date()
    except ValueError:
        return {"ok": False, "reply": "⚠️ I couldn't read that date."}

    if run_date is None:
        return {
            "ok": True,
            "reply": f"🕒 I won't change the AC now. What day should I schedule {action.lower()} at {hour}:{minute:02d} {ampm}?",
        }

    run_at = datetime(run_date.year, run_date.month, run_date.day, hour24, minute, tzinfo=tz)
    if run_at <= now:
        return {
            "ok": False,
            "reply": f"⏰ {run_at.strftime('%-I:%M %p')} on {run_at.strftime('%b %-d')} has already passed. Nothing was changed.",
        }

    fan = None
    fan_match = re.search(r"\bfan(?:\s+(?:at|to|of))?\s*(\d{1,3})\s*%?\b", natural)
    if fan_match:
        fan_value = int(fan_match.group(1))
        if fan_value < 0 or fan_value > 100:
            return {"ok": False, "reply": "⚠️ Fan must be between 0% and 100%."}
        fan = str(fan_value)

    payload = {
        "name": f"SMS one-time {run_date.isoformat()} {hour}:{minute:02d} {ampm}",
        "schedule_type": "one_time",
        "days": "",
        "start_date": run_date.isoformat(),
        "end_date": run_date.isoformat(),
        "time_local": f"{hour24:02d}:{minute:02d}",
        "action": action.lower(),
        "mode": None,
        "temperature": None,
        "fan": fan,
        "enabled": True,
    }
    created = create_schedule(payload)

    fan_text = f" • Fan {fan}%" if fan is not None else ""
    return {
        "ok": True,
        "reply": (
            f"✅ Scheduled\n"
            f"{run_at.strftime('%a • %b %-d • %-I:%M %p')}\n"
            f"{'▶️ On' if action == 'ON' else '⏹️ Off'}{fan_text}"
        ),
        "schedule": created,
    }


def process_text_command(raw: str) -> dict[str, Any]:
    raw_text = (raw or "").strip()
    normalized_raw = re.sub(r"\b([ap])\s*\.?\s*m\.?\b", r"\1m", raw_text, flags=re.I)
    command = " ".join(normalized_raw.upper().split())
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

    natural_schedule = _handle_natural_one_time_schedule(normalized_raw)
    if natural_schedule is not None:
        return natural_schedule

    if command == "AI STATUS":
        state = "On" if settings.openai_api_key else "Off"
        return {"ok": True, "reply": f"🤖 AI: {state}\nModel: {settings.openai_model}"}

    canonical_range = re.fullmatch(r"SCHEDULE\s+NEXT\s+(\d{1,2})\s+DAYS?", command)
    if canonical_range:
        days = max(1, min(30, int(canonical_range.group(1))))
        return {"ok": True, "reply": _schedule_list_reply(days=days)}

    schedule_range = re.search(r"\b(?:schedule|schedules)\b.*?\bnext\s+(\d{1,2})\s+days?\b", natural)
    if not schedule_range:
        schedule_range = re.search(r"\bnext\s+(\d{1,2})\s+days?\b.*?\b(?:schedule|schedules)\b", natural)
    if schedule_range:
        days = max(1, min(30, int(schedule_range.group(1))))
        return {"ok": True, "reply": _schedule_list_reply(days=days)}

    if re.search(r"\b(?:list|show|give|tell)\b.*?\b(?:my\s+)?(?:schedule|schedules)\b", natural):
        return {"ok": True, "reply": _schedule_list_reply()}

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
    if any(p in natural for p in ["what's the ac status", "what is the ac status", "ac status", "what's the ac doing", "what is the ac doing"]) or re.fullmatch(r"(?:please\s+)?(?:check|show|tell me|give me)\s+(?:the\s+)?(?:ac\s+)?status(?:\s+please)?", natural):
        command = "STATUS"

    if any(p in natural for p in ["what's my schedule", "what is my schedule", "show my schedule", "show schedule"]):
        command = "SCHEDULE"
        schedule_result = _handle_schedule_command(command, raw_text)
        if schedule_result is not None:
            return schedule_result

    if any(p in natural for p in ["a little colder", "a bit colder", "make it colder"]):
        return {
            "ok": True,
            "reply": "🤔 How much colder?\nYou can say:\n• 2 degrees colder\n• Set it to 72°",
        }
    if any(p in natural for p in ["a little warmer", "a bit warmer", "make it warmer"]):
        return {
            "ok": True,
            "reply": "🤔 How much warmer?\nYou can say:\n• 2 degrees warmer\n• Set it to 72°",
        }

    # SAFETY: anything that looks scheduled/future must never fall through to immediate ON/OFF.
    power_action = re.search(r"\b(?:turn|switch|shut)(?:\s+(?:it|the ac))?\s+(on|off)\b", natural)
    time_match = re.search(r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", natural)
    future_words = any(word in natural for word in [
        "schedule", "scheduled", "later", "today", "tomorrow", "tonight", "until", "at "
    ])

    if power_action and (time_match or future_words):
        action = power_action.group(1).upper()

        if time_match:
            hour = time_match.group(1)
            minute = time_match.group(2) or "00"
            ampm = time_match.group(3).upper()
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
                "reply": f"🕒 I won't change the AC right now. Do you mean {action.lower()} today at {hour}:{minute} {ampm}?",
            }

        return {
            "ok": True,
            "reply": "🕒 I understood this as a scheduled change, so I did not change the AC now. What day and time should I use?",
        }

    # "Keep/stay on until 4 PM" means schedule OFF at that time, not OFF now.
    stay_on_until = re.search(
        r"\b(?:stay|keep)(?:\s+(?:it|the ac))?\s+on\s+until\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b",
        natural,
    )
    if stay_on_until:
        hour = stay_on_until.group(1)
        minute = stay_on_until.group(2) or "00"
        ampm = stay_on_until.group(3).upper()
        day = "TOMORROW" if "tomorrow" in natural else "TODAY"
        scheduled = _handle_schedule_command(
            f"WEEK: {day} {hour}:{minute} {ampm} OFF",
            f"WEEK: {day} {hour}:{minute} {ampm} OFF",
        )
        if scheduled is not None:
            return scheduled

    swing_command = None
    if re.fullmatch(r"SWING\s+(VERTICAL|HORIZONTAL|BOTH)\s+(ON|OFF)", command):
        swing_command = command
    elif re.search(r"\b(?:stop|disable|turn off)\b.*?\b(?:swing|swinging)\b", natural):
        if re.search(r"\b(?:up\s*(?:and|&)\s*down|up/down|up|down|vertical)\b", natural):
            swing_command = "SWING VERTICAL OFF"
        elif re.search(r"\b(?:left\s*(?:and|&)\s*right|right\s*(?:and|&)\s*left|left/right|right/left|left|right|horizontal)\b", natural):
            swing_command = "SWING HORIZONTAL OFF"
        else:
            swing_command = "SWING BOTH OFF"
    elif re.search(r"\b(?:swing|swinging)\b", natural):
        if re.search(r"\b(?:up\s*(?:and|&)\s*down|up/down|up|down|vertical)\b", natural):
            swing_command = "SWING VERTICAL ON"
        elif re.search(r"\b(?:left\s*(?:and|&)\s*right|right\s*(?:and|&)\s*left|left/right|right/left|left|right|horizontal)\b", natural):
            swing_command = "SWING HORIZONTAL ON"
        elif re.search(r"\b(?:both|all directions|both ways)\b", natural):
            swing_command = "SWING BOTH ON"

    if swing_command:
        direction, state = swing_command.split()[1:]
        value = state == "ON"
        if direction == "VERTICAL":
            return _execute({"action": "set", "vertical_swing": value}, swing_command)
        if direction == "HORIZONTAL":
            return _execute({"action": "set", "horizontal_swing": value}, swing_command)
        return _execute(
            {"action": "set", "vertical_swing": value, "horizontal_swing": value},
            swing_command,
        )

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

    return {
        "ok": False,
        "reply": "🤔 I didn't catch that.\nTry saying it naturally, like:\n• Check status\n• Make it 72°\n• Show next 10 days\nOr text HELP.",
    }
