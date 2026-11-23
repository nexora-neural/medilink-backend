"""Explicit response projections; never return raw ORM records."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReasonInput(StrictModel):
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Reason must not be blank.")
        return value.strip()


class BackupRead(StrictModel):
    id: UUID
    status: Literal["running", "succeeded", "failed"]
    database_included: bool
    files_included: bool
    started_at: datetime
    finished_at: datetime | None
    restore_check_status: Literal["not_run", "passed", "failed"]
    restore_checked_at: datetime | None
    error_code: str | None


class OperationalSummary(StrictModel):
    pending_verifications: int
    active_organizations: int
    failed_jobs: int
    last_backup_at: datetime | None


class JobStatus(StrictModel):
    id: UUID
    job_type: str
    status: Literal["pending", "running", "succeeded", "failed", "cancelled"]
    attempt_count: int
    error_code: str | None
    updated_at: datetime


class AuditRead(StrictModel):
    id: UUID
    actor_user_id: UUID | None
    actor_client_id: UUID | None
    action: str
    target_type: str
    target_id: UUID | None
    outcome: str
    request_id: str
    metadata: dict
    created_at: datetime


class PatientActivity(StrictModel):
    id: UUID
    action: str
    target_type: str
    target_id: UUID | None
    outcome: str
    created_at: datetime


class Page(StrictModel):
    page: int
    page_size: int
    total: int


class JobStatusPage(Page):
    items: list[JobStatus]


class AuditEventsReadPage(Page):
    items: list[AuditRead]


class PatientActivityPage(Page):
    items: list[PatientActivity]


class BackupPage(Page):
    items: list[BackupRead]
