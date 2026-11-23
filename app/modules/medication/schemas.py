"""Pydantic request and response schemas for medication schedules and adherence."""

import re
from datetime import datetime, time
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


class ScheduleInput(BaseModel):
    """Request to create or replace a medication schedule."""

    model_config = ConfigDict(extra="forbid")

    prescription_item_id: UUID
    timezone: str = Field(min_length=1, max_length=100)
    starts_at: datetime
    ends_at: datetime | None = None
    times: list[str] = Field(min_length=1, max_length=24)
    confirmed: Literal[True]

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("A valid IANA timezone is required.") from None
        return value

    @field_validator("starts_at", "ends_at")
    @classmethod
    def validate_datetime_timezone(cls, value: datetime | None):
        if value is not None and value.tzinfo is None:
            raise ValueError("Datetime values must include a timezone.")
        return value

    @field_validator("times")
    @classmethod
    def validate_times(cls, values: list[str]) -> list[str]:
        pattern = re.compile(r"^([01]\d|2[0-3]):[0-5]\d:[0-5]\d$")

        for value in values:
            if not pattern.fullmatch(value):
                raise ValueError("Times must use HH:MM:SS format.")

        if len(values) != len(set(values)):
            raise ValueError("Reminder times must not contain duplicates.")

        return values

    @model_validator(mode="after")
    def validate_schedule_window(self):
        if self.ends_at is not None and self.ends_at < self.starts_at:
            raise ValueError("ends_at must be on or after starts_at.")
        return self


class MedicationScheduleRead(BaseModel):
    """Medication schedule response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    prescription_item_id: UUID
    patient_user_id: UUID
    timezone: str
    starts_at: datetime
    ends_at: datetime | None
    confirmed_at: datetime
    status: str
    created_at: datetime
    version: int


class ScheduleTimeRead(BaseModel):
    """Scheduled reminder time response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    schedule_id: UUID
    time_of_day: time


class ScheduleDetail(BaseModel):
    """Medication schedule with its reminder times."""

    model_config = ConfigDict(extra="forbid")

    schedule: MedicationScheduleRead
    times: list[ScheduleTimeRead]
