"""Python enums for medication schedules and adherence."""

from enum import Enum


class ScheduleStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class DoseStatus(str, Enum):
    UPCOMING = "upcoming"
    DUE = "due"
    TAKEN = "taken"
    SKIPPED = "skipped"
    UNREPORTED = "unreported"
    CANCELLED = "cancelled"


class DoseResponse(str, Enum):
    TAKEN = "taken"
    SKIPPED = "skipped"

