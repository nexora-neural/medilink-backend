"""ML-104 request and response schemas."""

from datetime import date, datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from app.modules.laboratory.models import UploadRequestStatus


class LabUploadRequestCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    laboratory_id: UUID


class LabUploadRequestRevoke(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=1000),
    ]

    @field_validator("reason")
    @classmethod
    def reject_null_character(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("Revocation reason must not contain a null character.")
        return value


class LabUploadRequestResolve(BaseModel):
    model_config = ConfigDict(extra="forbid")

    laboratory_id: UUID
    request_token: Annotated[str, Field(min_length=1, max_length=256, repr=False)]


class LabUploadRequestRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    patient_user_id: UUID
    laboratory_id: UUID
    expires_at: datetime
    status: UploadRequestStatus
    created_at: datetime
    used_at: datetime | None = None


class LabUploadRequestCreated(BaseModel):
    request: LabUploadRequestRead
    request_token: str = Field(repr=False)


class LabUploadRequestPage(BaseModel):
    items: list[LabUploadRequestRead]
    page: int
    page_size: int
    total: int


class PatientIdentity(BaseModel):
    patient_user_id: UUID
    display_name: str
    date_of_birth: date | None = None


class LabUploadRequestResolved(BaseModel):
    request_id: UUID
    patient: PatientIdentity
    laboratory_id: UUID
    expires_at: datetime


class LabUploadRequestErrorResponse(BaseModel):
    code: str
    message: str
    field_errors: dict[str, list[str]]
    request_id: str