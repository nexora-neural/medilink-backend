"""Pydantic request and response schemas for patient consent and access grants."""


from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.modules.profiles.doctor_qr.schemas import DoctorReference

GrantCategoryName = Literal[
    "history_allergies",
    "prescriptions",
    "reports",
    "measurements_vaccinations",
    "medication_logs",
]
GrantStatus = Literal["active", "expired", "revoked"]


class GrantInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doctor_reference: DoctorReference
    organization_id: UUID | None = None
    categories: list[GrantCategoryName] = Field(min_length=1, max_length=5)
    duration_hours: Literal[24, 168, 720] = 24
    allow_consultation: bool = Field(strict=True)

    @field_validator("duration_hours", mode="before")
    @classmethod
    def validate_duration_type(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Use an integer duration in hours.")
        return value

    @field_validator("categories")
    @classmethod
    def validate_unique_categories(cls, value: list[GrantCategoryName]) -> list[GrantCategoryName]:
        if len(value) != len(set(value)):
            raise ValueError("Select each category only once.")
        return value


class GrantRevokeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]


class GrantListQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)
    status: GrantStatus | None = None


class AccessGrantsRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    patient_user_id: UUID
    professional_user_id: UUID
    organization_id: UUID | None
    allow_consultation: bool
    status: GrantStatus
    starts_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    created_at: datetime


class GrantDetail(BaseModel):
    grant: AccessGrantsRead
    categories: list[GrantCategoryName]


class GrantDetailPage(BaseModel):
    items: list[GrantDetail]
    page: int
    page_size: int
    total: int


class GrantErrorResponse(BaseModel):
    code: str
    message: str
    field_errors: dict[str, list[str]]
    request_id: str
