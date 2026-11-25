"""Contract inputs and responses for confirmed daily fixed-time reminders."""

import re
from datetime import datetime, time
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.modules.medication.enum import ScheduleStatus
from app.modules.prescriptions.core_models import PrescriptionSource


class ScheduleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prescription_item_id: UUID
    timezone: str = Field(min_length=1, max_length=100)
    starts_at: AwareDatetime
    ends_at: AwareDatetime | None = None
    times: list[str] = Field(min_length=1, max_length=24)
    confirmed: Literal[True]

    @field_validator("confirmed", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("Explicit confirmation is required.")
        return value

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("A valid IANA timezone is required.") from None
        return value

    @field_validator("starts_at", "ends_at", mode="before")
    @classmethod
    def validate_datetime_timezone(cls, value):
        if (
            value is not None
            and not isinstance(value, datetime)
            and (not isinstance(value, str) or "T" not in value.upper())
        ):
            raise ValueError("Use an RFC3339 timestamp with a timezone.")
        return value

    @field_validator("times")
    @classmethod
    def validate_times(cls, values):
        if any(not re.fullmatch(r"([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]", v) for v in values):
            raise ValueError("Times must use HH:MM:SS format.")
        if len(values) != len(set(values)):
            raise ValueError("Reminder times must not contain duplicates.")
        return sorted(values)

    @model_validator(mode="after")
    def validate_schedule_window(self):
        if self.ends_at is not None and self.ends_at <= self.starts_at:
            raise ValueError("ends_at must follow starts_at.")
        return self


class ScheduleAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ScheduleStatus
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def nonblank_reason(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("A reason is required.")
        return value


class ScheduleListQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: int = Field(default=1, ge=1, le=2147483647)
    page_size: int = Field(default=20, ge=1, le=100)
    status: ScheduleStatus | None = None


class MedicationScheduleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    prescription_item_id: UUID
    patient_user_id: UUID
    timezone: str
    starts_at: AwareDatetime
    ends_at: AwareDatetime | None
    confirmed_at: AwareDatetime
    status: ScheduleStatus
    created_at: AwareDatetime
    version: int
    previous_schedule_id: UUID | None = None


class ScheduleTimeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    schedule_id: UUID
    time_of_day: time


class ScheduleDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schedule: MedicationScheduleRead
    times: list[ScheduleTimeRead]
    prescription_source: PrescriptionSource | None = None


class ScheduleDetailPage(BaseModel):
    items: list[ScheduleDetail]
    page: int
    page_size: int
    total: int


class ScheduleErrorResponse(BaseModel):
    code: str
    message: str
    field_errors: dict[str, list[str]] = Field(default_factory=dict)
    request_id: str
