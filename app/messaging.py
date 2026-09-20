from __future__ import annotations

import re
from typing import Any

from .db import add_activity, enqueue_device_command, get_latest_device_state
from .weather import comfort_recommendation, forecast_summary


HELP_TEXT = (
    "Commands: STATUS, ON, OFF, COOL 72, HEAT 70, FAN HIGH, WEATHER TODAY, HELP."
)


def _short_error(exc: Exception) -> str:
    text = str(exc).strip()
    if "3123" in text or "offline" in text.lower():
        return "AC is offline (Midea error 3123)."
    if "locked" in text.lower():
        return "AC control is currently locked."
    return f"AC error: {text[:120]}"


def process_text_command(raw: str) -> dict[str, Any]:
    command = " ".join((raw or "").strip().upper().split())
    if not command:
        return {"ok": False, "reply": HELP_TEXT}

    add_activity("sms-email", "message_received", "received", command[:200])


    natural = command.lower()

    if "weather" in natural:
        day = "tomorrow" if "tomorrow" in natural else "today"
        try:
            rec = comfort_recommendation(day)
            f = rec["forecast"]
            if any(word in natural for word in ["set", "should", "recommend", "based"]):
                reply = (
                    f"{day.title()}: high {f['high_f']}F, low {f['low_f']}F, "
                    f"rain {f['rain_chance']}%. I recommend "
                    f"{rec['recommended_mode']} {rec['recommended_temperature']}F."
                )
                if any(word in natural for word in ["set it", "set the ac", "do it", "based on"]):
                    try:
                        queued = enqueue_device_command("sms-email", {
                            "action": "set",
                            "mode": rec["recommended_mode"],
                            "temperature": rec["recommended_temperature"],
                        })
                        return {"ok": True, "reply": reply + " AC change queued.", "result": {"execution_id": queued["id"], "queued": True}}
                    except Exception as exc:
                        return {"ok": False, "reply": reply + " " + _short_error(exc)}
                return {"ok": True, "reply": reply}
            return {
                "ok": True,
                "reply": (
                    f"{day.title()}: high {f['high_f']}F, low {f['low_f']}F, "
                    f"rain chance {f['rain_chance']}%."
                ),
            }
        except Exception as exc:
            return {"ok": False, "reply": f"Weather lookup failed: {str(exc)[:120]}"}

    if any(p in natural for p in ["what should i set", "what should the ac", "make it comfortable"]):
        try:
            rec = comfort_recommendation("today")
            f = rec["forecast"]
            return {
                "ok": True,
                "reply": (
                    f"Today's forecast is {f['high_f']}F high / {f['low_f']}F low "
                    f"with {f['rain_chance']}% rain chance. I recommend "
                    f"{rec['recommended_mode']} {rec['recommended_temperature']}F."
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
            temp_match = re.search(r"(\d{2}).*?(cool|cooling|heat|heating)", natural)
            if temp_match:
                temp_match = (temp_match.group(2), temp_match.group(1))
        if temp_match and not isinstance(temp_match, tuple):
            command = f"{'COOL' if temp_match.group(1).startswith('cool') else 'HEAT'} {temp_match.group(2)}"
        elif isinstance(temp_match, tuple):
            command = f"{'COOL' if temp_match[0].startswith('cool') else 'HEAT'} {temp_match[1]}"

    if command in {"HELP", "?", "COMMANDS"}:
        return {"ok": True, "reply": HELP_TEXT}

    if command in {"STATUS", "MODE"}:
        try:
            cached = get_latest_device_state()
            if not cached:
                return {"ok": False, "reply": "AC status unavailable. Local worker has not reported yet."}
            if not cached.get("online"):
                return {"ok": False, "reply": "AC is unavailable."}
            result = cached.get("state") or {}
            add_activity("sms-email", "status", "success")
            mode_names = {1: "Auto", 2: "Cool", 3: "Dry", 4: "Heat", 5: "Fan"}
            mode_code = result.get("mode")
            if command == "MODE":
                return {"ok": True, "reply": f"Mode code {mode_code} ({mode_names.get(mode_code, 'Unknown')})."}
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
            return {"ok": True, "reply": " | ".join(parts)}
        except Exception as exc:
            reply = _short_error(exc)
            add_activity("sms-email", "status", "error", str(exc))
            return {"ok": False, "reply": reply}

    if command == "OFF":
        payload = {"action": "off"}
    elif command == "ON":
        payload = {"action": "on"}
    else:
        match = re.fullmatch(r"(COOL|HEAT)\s+(\d{2}(?:\.\d)?)", command)
        if match:
            payload = {
                "action": "set",
                "mode": match.group(1).lower(),
                "temperature": float(match.group(2)),
            }
        else:
            fan = re.fullmatch(r"FAN\s+(AUTO|LOW|MEDIUM|HIGH)", command)
            if fan:
                payload = {"action": "set", "fan": fan.group(1).lower()}
            else:
                return {
                    "ok": False,
                    "reply": f"Unknown command: {command}. {HELP_TEXT}",
                }

    try:
        queued = enqueue_device_command("sms-email", payload)
        return {
            "ok": True,
            "reply": f"Queued: {command}.",
            "result": {"execution_id": queued["id"], "queued": True},
        }
    except Exception as exc:
        reply = _short_error(exc)
        add_activity("sms-email", "device_command", "error", f"{payload}: {exc}")
        return {"ok": False, "reply": reply}
