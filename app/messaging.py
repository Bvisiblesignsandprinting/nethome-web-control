from __future__ import annotations

import re
from typing import Any

from .db import add_activity, get_latest_device_state, save_device_state
from .midea_client import midea
from .weather import comfort_recommendation

HELP_TEXT = "STATUS, ON, OFF, TEMP 72, COOL 72, HEAT 70, FAN HIGH, WEATHER."


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


def process_text_command(raw: str) -> dict[str, Any]:
    command = " ".join((raw or "").strip().upper().split())
    if not command:
        return {"ok": False, "reply": HELP_TEXT}

    add_activity("sms", "message_received", "received", command[:200])
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

    if "weather" in natural:
        day = "tomorrow" if "tomorrow" in natural else "today"
        try:
            rec = comfort_recommendation(day)
            f = rec["forecast"]
            reply = (
                f"{day.title()}: high {f['high_f']}F, low {f['low_f']}F, "
                f"rain {f['rain_chance']}%."
            )
            if any(word in natural for word in ["set", "should", "recommend", "based"]):
                reply += (
                    f" Recommend {rec['recommended_mode']} "
                    f"{rec['recommended_temperature']}F."
                )
                if any(word in natural for word in ["set it", "set the ac", "do it", "based on"]):
                    payload = {
                        "action": "set",
                        "mode": rec["recommended_mode"],
                        "temperature": rec["recommended_temperature"],
                    }
                    result = _execute(
                        payload,
                        f"{str(rec['recommended_mode']).upper()} {rec['recommended_temperature']}F",
                    )
                    result["reply"] = reply + " " + result["reply"]
                    return result
            return {"ok": True, "reply": reply}
        except Exception as exc:
            return {"ok": False, "reply": f"Weather lookup failed: {str(exc)[:120]}"}

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
