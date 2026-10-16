"""Lighting planning API. Hardware delivery is deliberately unavailable until connected."""
from __future__ import annotations

import re
import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator, model_validator

from .lighting_store import ZONE_DEFINITIONS, document

ZONES = {z["id"]: z for z in ZONE_DEFINITIONS}


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")
    power: StrictBool | None = None
    brightness: StrictInt | None = Field(default=None, ge=0, le=100)
    color: str | None = None

    @field_validator("color")
    @classmethod
    def valid_color(cls, value):
        if value is None or value == "white":
            return value
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            raise ValueError("Choose a six-digit hex color or white")
        return value.upper()

    @model_validator(mode="after")
    def nonempty(self):
        if all(getattr(self, key) is None for key in ("power", "brightness", "color")):
            raise ValueError("Choose at least one lighting setting")
        return self


def validate_zone_command(zone_id, command):
    if zone_id not in ZONES:
        raise HTTPException(404, "Lighting zone not found")
    if ZONES[zone_id]["kind"] == "switch" and (command.brightness is not None or command.color is not None):
        raise HTTPException(422, "Regular lights currently support on/off only")


class ZoneWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: StrictInt = Field(ge=0)
    command: Command


class LightingSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    zone_id: str
    command: Command
    repeat: Literal["daily", "weekly", "one_time"] = "daily"
    time_local: str
    timezone: str = "America/New_York"
    days: list[StrictInt] = Field(default_factory=list, max_length=7)
    date_local: date | None = None
    enabled: StrictBool = True

    @field_validator("name")
    @classmethod
    def trimmed_name(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Enter a schedule name")
        return value

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Choose a valid IANA timezone")
        return value

    @field_validator("time_local")
    @classmethod
    def valid_time(cls, value):
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("Time must use HH:MM")
        return value

    @model_validator(mode="after")
    def valid_recurrence(self):
        if self.command.power is None:
            raise ValueError("Choose on or off for a lighting schedule")
        if any(day < 0 or day > 6 for day in self.days) or len(set(self.days)) != len(self.days):
            raise ValueError("Select distinct weekdays from Monday through Sunday")
        if self.repeat == "weekly" and not self.days:
            raise ValueError("Choose at least one weekday")
        if self.repeat == "one_time" and self.date_local is None:
            raise ValueError("Choose a date for a one-time schedule")
        if self.repeat != "one_time" and self.date_local is not None:
            raise ValueError("A specific date is only used by one-time schedules")
        if self.repeat != "weekly" and self.days:
            raise ValueError("Weekdays are only used by weekly schedules")
        return self


class ScheduleWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: StrictInt = Field(ge=0)
    schedule: LightingSchedule


def occurrences(schedule, now=None, limit=3):
    """Return future instants; skip DST gaps and use the first repeated hour once."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    tz = ZoneInfo(schedule["timezone"])
    first = now.astimezone(tz).date()
    one_date = date.fromisoformat(str(schedule["date_local"])) if schedule.get("date_local") else None
    dates = [one_date] if schedule["repeat"] == "one_time" else [first + timedelta(days=i) for i in range(23)]
    result = []
    for day in dates:
        if day is None or (schedule["repeat"] == "weekly" and day.weekday() not in schedule["days"]):
            continue
        local = datetime.combine(day, time.fromisoformat(schedule["time_local"]), tzinfo=tz)
        utc = local.astimezone(timezone.utc)
        if utc.astimezone(tz).replace(tzinfo=None) != local.replace(tzinfo=None):
            continue
        if utc < now:
            continue
        result.append({"utc": utc.isoformat(), "local": local.isoformat()})
        if len(result) >= limit:
            break
    return result


def snapshot(value):
    return {
        "revision": value["revision"], "hardware_ready": False,
        "connection": "not_connected", "delivery": "preview_only",
        "message": "Settings and schedules are saved. Lights will respond after the hardware is connected.",
        "zones": [{**zone, "desired": value["zones"][zone["id"]], "reported": None,
                   "connection": "not_connected"} for zone in ZONE_DEFINITIONS],
        "schedules": [{**s, "status": "waiting_for_connection" if s["enabled"] else "disabled",
                       "next_occurrences": occurrences(s) if s["enabled"] else []} for s in value["schedules"]],
    }


def validate_schedule(schedule):
    validate_zone_command(schedule.zone_id, schedule.command)
    if schedule.enabled and schedule.repeat == "one_time" and not occurrences(schedule.model_dump(mode="json")):
        raise HTTPException(422, "Choose a future date and time for an enabled one-time schedule")


def lighting_router(auth):
    router = APIRouter(prefix="/api/lighting", dependencies=[Depends(auth)])

    @router.get("")
    def read(response: Response):
        response.headers["Cache-Control"] = "no-store"
        return snapshot(document())

    @router.patch("/zones/{zone_id}")
    def update_zone(zone_id: str, body: ZoneWrite):
        validate_zone_command(zone_id, body.command)
        def update(value):
            value["zones"][zone_id].update(body.command.model_dump(exclude_none=True))
        return snapshot(document(update, revision=body.revision, action="zone_" + zone_id))

    @router.post("/schedules/preview")
    def preview(body: LightingSchedule):
        validate_zone_command(body.zone_id, body.command)
        return {"occurrences": occurrences(body.model_dump(mode="json")), "delivery": "preview_only", "hardware_ready": False}

    @router.post("/schedules")
    def create(body: ScheduleWrite):
        validate_schedule(body.schedule)
        item = {**body.schedule.model_dump(mode="json", exclude_none=True), "id": uuid.uuid4().hex}
        def update(value):
            if len(value["schedules"]) >= 100:
                raise HTTPException(422, "A maximum of 100 lighting schedules can be saved")
            value["schedules"].append(item)
        return snapshot(document(update, revision=body.revision, action="schedule_created"))

    @router.put("/schedules/{schedule_id}")
    def edit(schedule_id: str, body: ScheduleWrite):
        validate_schedule(body.schedule)
        def update(value):
            index = next((i for i, s in enumerate(value["schedules"]) if s["id"] == schedule_id), None)
            if index is None:
                raise HTTPException(404, "Lighting schedule not found")
            value["schedules"][index] = {**body.schedule.model_dump(mode="json", exclude_none=True), "id": schedule_id}
        return snapshot(document(update, revision=body.revision, action="schedule_updated"))

    @router.delete("/schedules/{schedule_id}")
    def delete(schedule_id: str, revision: int = Query(ge=0)):
        def update(value):
            if not any(s["id"] == schedule_id for s in value["schedules"]):
                raise HTTPException(404, "Lighting schedule not found")
            value["schedules"] = [s for s in value["schedules"] if s["id"] != schedule_id]
        return snapshot(document(update, revision=revision, action="schedule_deleted"))

    return router
