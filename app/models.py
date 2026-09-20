from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
TIME_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"


class ScheduleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    schedule_type: Literal["one_time", "weekdays", "daily", "weekly"] = "weekly"
    days: str = Field(default="", max_length=80)
    start_date: str | None = Field(default=None, pattern=DATE_PATTERN)
    end_date: str | None = Field(default=None, pattern=DATE_PATTERN)
    time_local: str = Field(pattern=TIME_PATTERN)
    action: str = "set"
    mode: str | None = None
    temperature: float | None = Field(default=None, ge=50, le=90)
    fan: str | None = None
    enabled: bool = True

    @model_validator(mode="after")
    def normalize_schedule(self):
        if self.schedule_type == "weekdays":
            self.days = "Mon,Tue,Wed,Thu,Fri"
        elif self.schedule_type == "daily":
            self.days = "Mon,Tue,Wed,Thu,Fri,Sat,Sun"
        elif self.schedule_type == "one_time":
            if not self.start_date:
                raise ValueError("A date is required for a one-time schedule")
            self.end_date = self.start_date
            self.days = ""
        elif self.schedule_type == "weekly" and not self.days.strip():
            raise ValueError("Choose at least one weekday for a weekly schedule")

        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("End date cannot be before start date")
        return self


class ScheduleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    schedule_type: Literal["one_time", "weekdays", "daily", "weekly"] | None = None
    days: str | None = Field(default=None, max_length=80)
    start_date: str | None = Field(default=None, pattern=DATE_PATTERN)
    end_date: str | None = Field(default=None, pattern=DATE_PATTERN)
    time_local: str | None = Field(default=None, pattern=TIME_PATTERN)
    action: str | None = None
    mode: str | None = None
    temperature: float | None = Field(default=None, ge=50, le=90)
    fan: str | None = None
    enabled: bool | None = None


class DeviceCommand(BaseModel):
    action: str
    temperature: float | None = Field(default=None, ge=50, le=90)
    mode: str | None = None
    fan: str | None = None
    running: bool | None = None
    horizontal_swing: bool | None = None
    vertical_swing: bool | None = None
    eco_mode: bool | None = None
    comfort_sleep: bool | None = None
    turbo: bool | None = None


class SmsConfigUpdate(BaseModel):
    allowed_from: str = Field(min_length=7, max_length=30)
    rotate_webhook_secret: bool = False


class EmailBridgeConfigUpdate(BaseModel):
    inbound_address: str = Field(min_length=5, max_length=320)
    postmark_server_token: str = Field(min_length=3, max_length=300)
    from_email: str = Field(min_length=5, max_length=320)
    allowed_phone: str = Field(min_length=7, max_length=30)
    rotate_webhook_secret: bool = False


class GoogleVoiceConfigUpdate(BaseModel):
    allowed_phone: str = Field(min_length=7, max_length=30)
    rotate_bridge_secret: bool = False
