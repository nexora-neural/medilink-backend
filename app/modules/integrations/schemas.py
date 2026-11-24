"""Pydantic request and response schemas for integration administration."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.modules.integrations.models import IntegrationStatus, SubmissionStatus


class EligibleLaboratoryRead(BaseModel):
    """Minimal organization option; never expose staff identity or verification evidence."""

    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str


class EligibleLaboratoriesReadPage(BaseModel):
    items: list[EligibleLaboratoryRead]
    page: int
    page_size: int
    total: int


class IntegrationClientInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    organization_id: UUID
    name: str = Field(min_length=1, max_length=200)
    scopes: list[str] = Field(min_length=1, max_length=1)
    expires_at: datetime

    @field_validator("scopes")
    @classmethod
    def only_reports_write(cls, scopes: list[str]) -> list[str]:
        if scopes != ["reports:write"]:
            raise ValueError("Only the reports:write scope is supported.")
        return scopes


class RotateCredential(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expires_at: datetime
    reason: str = Field(min_length=1, max_length=1000)


class ReasonInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str = Field(min_length=1, max_length=1000)


class IntegrationClientRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    organization_id: UUID
    name: str
    scopes: list[str]
    status: IntegrationStatus
    expires_at: datetime
    created_by: UUID
    created_at: datetime
    revoked_at: datetime | None = None


class CredentialCreated(BaseModel):
    client: IntegrationClientRead
    credential: str


class IntegrationClientsReadPage(BaseModel):
    items: list[IntegrationClientRead]
    page: int
    page_size: int
    total: int


class IntegrationAdminSubmissionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    client_id: UUID
    upload_request_id: UUID
    external_report_id: str
    status: SubmissionStatus
    report_id: UUID | None = None
    received_at: datetime
    error_code: str | None = None


class IntegrationSubmissionRead(IntegrationAdminSubmissionRead):
    """Report submission/correction response preserving request idempotency."""

    idempotency_key: str


class IntegrationSubmissionsReadPage(BaseModel):
    items: list[IntegrationAdminSubmissionRead]
    page: int
    page_size: int
    total: int
