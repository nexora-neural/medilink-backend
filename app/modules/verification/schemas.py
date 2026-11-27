"""Request and response contracts for professional verification applications."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.modules.identity.enums import ProfessionalRole


class VerificationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProfessionalProfileInput(VerificationInput):
    role: ProfessionalRole
    registration_number: str | None = Field(default=None, max_length=100)
    workplace: str | None = Field(default=None, max_length=200)
    qualifications: str | None = Field(default=None, max_length=2000)
    specialty: str | None = Field(default=None, max_length=150)

    @field_validator("registration_number", "workplace", "qualifications", "specialty")
    @classmethod
    def normalize_text(cls, value: str | None) -> str | None:
        return None if value is not None and not value.strip() else value


class ProfessionalProfileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: UUID
    role: ProfessionalRole
    doctor_reference: str | None
    registration_number: str | None
    workplace: str | None
    qualifications: str | None
    specialty: str | None
    verification_status: str
    created_at: datetime
    updated_at: datetime


class VerificationApplicationInput(VerificationInput):
    role: ProfessionalRole
    organization_id: UUID | None = None
    registration_number: str = Field(min_length=1, max_length=100)
    workplace: str | None = Field(default=None, max_length=200)
    evidence_document_ids: list[UUID] = Field(min_length=1, max_length=20)

    @field_validator("registration_number")
    @classmethod
    def nonblank_registration(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Registration number must not be blank.")
        return value

    @field_validator("workplace")
    @classmethod
    def nonblank_workplace(cls, value: str | None) -> str | None:
        return None if value is not None and not value.strip() else value

    @model_validator(mode="after")
    def unique_evidence(self):
        if len(set(self.evidence_document_ids)) != len(self.evidence_document_ids):
            raise ValueError("Duplicate evidence document IDs are not allowed.")
        return self


class VerificationApplicationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    organization_id: UUID | None
    role: ProfessionalRole
    registration_number: str | None
    workplace: str | None = None
    status: str
    submitted_at: datetime
    reviewed_by: UUID | None
    reviewed_at: datetime | None
    review_reason: str | None
    evidence_document_ids: list[UUID] = Field(default_factory=list)
    applicant_display_name: str | None = None
    organization_name: str | None = None


class VerificationApplicationsReadPage(BaseModel):
    items: list[VerificationApplicationRead]
    page: int
    page_size: int
    total: int


class VerificationDecisionInput(VerificationInput):
    status: Literal["approved", "rejected", "more_info_requested"]
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def nonblank_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Decision reason must not be blank.")
        return value


class VerificationDecisionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    application_id: UUID
    reviewer_user_id: UUID
    decision: str
    reason: str
    decided_at: datetime
    reviewer_display_name: str | None = None


class VerificationDecisionsReadPage(BaseModel):
    items: list[VerificationDecisionRead]
    page: int
    page_size: int
    total: int


# Retain import compatibility for code that used the old name.
VerificationApplicationList = VerificationApplicationsReadPage
