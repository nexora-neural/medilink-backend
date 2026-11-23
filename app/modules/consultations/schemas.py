"""Request and response schemas for consultations."""

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ConsultationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grant_id: UUID


class ConsultationEnd(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(default="", max_length=4000, strict=True)


class ConsultationsRead(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
        extra="forbid",
    )

    id: UUID
    patient_user_id: UUID
    doctor_user_id: UUID
    organization_id: UUID | None
    grant_id: UUID
    started_at: datetime
    ended_at: datetime | None
    summary: str | None


class ConsultationsReadPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ConsultationsRead]
    page: int
    page_size: int
    total: int


class ConsultationListQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


class PatientIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    patient_user_id: UUID
    display_name: str
    date_of_birth: date | None = None


class QueueEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    patient: PatientIdentity
    grant_id: UUID
    categories: list[str]
    expires_at: datetime
    allow_consultation: bool


class QueueEntryPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[QueueEntry]
    page: int
    page_size: int
    total: int


class ConsultationErrorResponse(BaseModel):
    code: str
    message: str
    field_errors: dict[str, list[str]]
    request_id: str
