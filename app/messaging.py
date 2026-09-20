from __future__ import annotations

import re
from typing import Any

from .db import add_activity
from .midea_client import midea


HELP_TEXT = (
    "Commands: STATUS, ON, OFF, COOL 72, HEAT 70, FAN AUTO. "
    "AC write commands remain locked until live control is enabled."
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

    if command in {"HELP", "?", "COMMANDS"}:
        return {"ok": True, "reply": HELP_TEXT}

    if command == "STATUS":
        try:
            result = midea.status()
            add_activity("sms-email", "status", "success")
            return {"ok": True, "reply": f"AC status: {result}"}
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
        result = midea.command(payload)
        add_activity("sms-email", "device_command", "success", str(payload))
        return {"ok": True, "reply": f"OK. Command confirmed: {command}.", "result": result}
    except Exception as exc:
        reply = _short_error(exc)
        add_activity("sms-email", "device_command", "error", f"{payload}: {exc}")
        return {"ok": False, "reply": reply}
