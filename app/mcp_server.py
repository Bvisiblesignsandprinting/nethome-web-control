from __future__ import annotations

import hmac
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from .config import settings
from .db import (
    create_schedule,
    delete_schedule,
    list_activity,
    list_schedules,
    update_schedule,
)
from .midea_client import midea

mcp = FastMCP(
    "NetHome AC Control",
    instructions=(
        "Controls the Sukkah / Play Room mini-split and its schedules. "
        "Read status before changing the AC when practical. "
        "AC writes may be locked by the server; never claim a command succeeded unless the tool result says it did."
    ),
    stateless_http=True,
)

_READ = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)

_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)

_DELETE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=True,
    openWorldHint=True,
)


@mcp.tool(annotations=_READ)
def get_ac_status() -> dict[str, Any]:
    """Get the live status of the Sukkah / Play Room AC from the Midea cloud."""
    try:
        return {"ok": True, "status": midea.status()}
    except Exception as exc:
        return {
            "ok": False,
            "offline": "offline" in str(exc).lower(),
            "error": str(exc),
        }


@mcp.tool(annotations=_READ)
def list_ac_schedules() -> dict[str, Any]:
    """List every saved AC schedule, including whether each schedule is enabled."""
    return {"schedules": list_schedules()}


@mcp.tool(annotations=_READ)
def get_ac_activity(limit: int = 25) -> dict[str, Any]:
    """Get recent NetHome activity and schedule execution records."""
    limit = max(1, min(int(limit), 100))
    return {"activity": list_activity(limit)}


@mcp.tool(annotations=_WRITE)
def create_ac_schedule(
    name: str,
    schedule_type: str,
    time_local: str,
    days: str = "",
    start_date: str | None = None,
    end_date: str | None = None,
    mode: str | None = None,
    temperature: float | None = None,
    fan: str | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    """Create an AC schedule. schedule_type must be one_time, weekdays, daily, or weekly."""
    if schedule_type not in {"one_time", "weekdays", "daily", "weekly"}:
        raise ValueError("schedule_type must be one_time, weekdays, daily, or weekly")
    payload = {
        "name": name,
        "schedule_type": schedule_type,
        "days": days,
        "start_date": start_date,
        "end_date": end_date,
        "time_local": time_local,
        "action": "set",
        "mode": mode,
        "temperature": temperature,
        "fan": fan,
        "enabled": enabled,
    }
    return create_schedule(payload)


@mcp.tool(annotations=_WRITE)
def update_ac_schedule(
    schedule_id: int,
    name: str | None = None,
    schedule_type: str | None = None,
    days: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    time_local: str | None = None,
    mode: str | None = None,
    temperature: float | None = None,
    fan: str | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    """Update selected fields of an existing AC schedule."""
    payload: dict[str, Any] = {}
    for key, value in {
        "name": name,
        "schedule_type": schedule_type,
        "days": days,
        "start_date": start_date,
        "end_date": end_date,
        "time_local": time_local,
        "mode": mode,
        "temperature": temperature,
        "fan": fan,
        "enabled": enabled,
    }.items():
        if value is not None:
            payload[key] = value
    if schedule_type is not None and schedule_type not in {"one_time", "weekdays", "daily", "weekly"}:
        raise ValueError("schedule_type must be one_time, weekdays, daily, or weekly")
    row = update_schedule(int(schedule_id), payload)
    if not row:
        raise ValueError("Schedule not found")
    return row


@mcp.tool(annotations=_DELETE)
def delete_ac_schedule(schedule_id: int) -> dict[str, Any]:
    """Delete one AC schedule by its numeric ID."""
    deleted = delete_schedule(int(schedule_id))
    if not deleted:
        raise ValueError("Schedule not found")
    return {"ok": True, "schedule_id": int(schedule_id)}


@mcp.tool(annotations=_WRITE)
def set_ac(
    action: str = "set",
    temperature: float | None = None,
    mode: str | None = None,
    fan: str | None = None,
) -> dict[str, Any]:
    """Change the AC state. The server will refuse this while AC writes are locked."""
    command: dict[str, Any] = {"action": action}
    if temperature is not None:
        command["temperature"] = temperature
    if mode is not None:
        command["mode"] = mode
    if fan is not None:
        command["fan"] = fan
    result = midea.command(command)
    return {"ok": True, "result": result}


class BearerTokenMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin1").lower(): value.decode("latin1")
            for key, value in scope.get("headers", [])
        }
        supplied = headers.get("authorization", "")
        expected = settings.api_token
        good = (
            bool(expected)
            and expected != "change-me-before-remote-access"
            and hmac.compare_digest(supplied, f"Bearer {expected}")
        )

        if not good:
            body = b'{"error":"Unauthorized"}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"www-authenticate", b'Bearer realm="NetHome MCP"'),
                        (b"content-length", str(len(body)).encode("ascii")),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return

        await self.app(scope, receive, send)


mcp_http_app = BearerTokenMiddleware(
    mcp.streamable_http_app(
        json_response=True,
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        ),
    )
)
