
"""Pydantic schemas for professional verification applications."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.modules.identity.enums import ProfessionalRole


class VerificationInput(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )


class ProfessionalProfileInput(VerificationInput):
    role: ProfessionalRole
    registration_number: str | None = Field(default=None, max_length=150)
    qualifications: str | None = Field(default=None, max_length=2000)
    specialty: str | None = Field(default=None, max_length=200)

    @field_validator("registration_number", "qualifications", "specialty")
    @classmethod
    def normalize_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            return None
        return value


class ProfessionalProfileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: UUID
    role: ProfessionalRole
    doctor_reference: str | None
    registration_number: str | None
    qualifications: str | None
    specialty: str | None
    verification_status: str
    created_at: datetime
    updated_at: datetime


class VerificationApplicationInput(VerificationInput):
    organization_id: UUID | None = None
    registration_number: str | None = Field(default=None, max_length=150)
    document_ids: list[UUID] = Field(default_factory=list, max_length=20)

    @field_validator("registration_number")
    @classmethod
    def normalize_registration(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def validate_documents(self):
        if len(set(self.document_ids)) != len(self.document_ids):
            raise ValueError("Duplicate document IDs are not allowed.")
        return self


class VerificationApplicationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    organization_id: UUID | None
    role: ProfessionalRole
    registration_number: str | None
    status: str
    submitted_at: datetime
    reviewed_at: datetime | None
    review_reason: str | None
    document_ids: list[UUID] = Field(default_factory=list)


class VerificationApplicationList(BaseModel):
    items: list[VerificationApplicationRead]

