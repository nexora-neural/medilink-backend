"""Bounded clinical API inputs and provenance-preserving read models."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.modules.clinical_records.models import ClinicalRecordStatus, RecordSource

ClinicalType = Literal["condition", "diagnosis", "allergy", "note"]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class ClinicalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    consultation_id: UUID | None = None
    type: ClinicalType
    title: Title
    description: str | None = Field(default=None, max_length=8000)
    clinical_code: str | None = Field(default=None, max_length=100)
    recorded_at: AwareDatetime

    @field_validator("recorded_at", mode="before")
    @classmethod
    def timestamp(cls, value):
        if not isinstance(value, (str, datetime)) or (
            isinstance(value, str) and "T" not in value.upper()
        ):
            raise ValueError("Use an RFC3339 timestamp with a timezone.")
        return value


class ClinicalRecordRead(BaseModel):
    id: UUID
    patient_user_id: UUID
    consultation_id: UUID | None = None
    type: ClinicalType
    title: str
    description: str | None = None
    clinical_code: str | None = None
    source: RecordSource
    recorded_by: UUID
    organization_id: UUID | None = None
    status: ClinicalRecordStatus
    recorded_at: AwareDatetime
    created_at: AwareDatetime
    supersedes_id: UUID | None = None
    correction_reason: str | None = None


class ClinicalRecordReadPage(BaseModel):
    items: list[ClinicalRecordRead]
    page: int
    page_size: int
    total: int


class ClinicalListQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: int = Field(default=1, ge=1, le=2147483647)
    page_size: int = Field(default=20, ge=1, le=100)
    type: ClinicalType | None = None
    start: AwareDatetime | None = Field(default=None, alias="from")
    end: AwareDatetime | None = Field(default=None, alias="to")

    @field_validator("start", "end", mode="before")
    @classmethod
    def timestamp(cls, value):
        return ClinicalInput.timestamp(value) if value is not None else value

    @model_validator(mode="after")
    def window(self):
        if (self.start is None) != (self.end is None):
            raise ValueError("Provide both from and to.")
        if self.start is not None and self.start >= self.end:
            raise ValueError("from must precede to.")
        return self


class ClinicalErrorResponse(BaseModel):
    code: str
    message: str
    field_errors: dict[str, list[str]] = Field(default_factory=dict)
    request_id: str
