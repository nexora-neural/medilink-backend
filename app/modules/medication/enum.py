"""Python enums for medication schedules and adherence."""

from enum import StrEnum


class ScheduleStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class DoseStatus(StrEnum):
    UPCOMING = "upcoming"
    DUE = "due"
    TAKEN = "taken"
    SKIPPED = "skipped"
    UNREPORTED = "unreported"
    CANCELLED = "cancelled"


class DoseResponse(StrEnum):
    TAKEN = "taken"
    SKIPPED = "skipped"
