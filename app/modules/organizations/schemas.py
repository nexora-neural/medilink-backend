"""Request and response schemas for organizations and memberships."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class OrganizationInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    type: str = Field(pattern="^(clinic|hospital|pharmacy|laboratory|other)$")
    registration_ref: str | None = Field(default=None, max_length=100)


class MembershipInput(BaseModel):
    organization_id: UUID
    role: str = Field(pattern="^(doctor|pharmacist|lab_staff)$")


class OrganizationDecision(BaseModel):
    status: str = Field(pattern="^(active|suspended|rejected|inactive)$")
    reason: str = Field(min_length=1, max_length=1000)


class MembershipDecision(BaseModel):
    status: str = Field(pattern="^(approved|rejected|more_info_requested|suspended)$")
    reason: str = Field(min_length=1, max_length=1000)


class OrganizationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    type: str
    registration_ref: str | None
    status: str
    created_at: datetime
    updated_at: datetime


class OrganizationPublic(BaseModel):
    id: UUID
    name: str
    type: str


class MembershipRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    organization_id: UUID
    user_id: UUID
    role: str
    status: str
    verified_at: datetime | None
    created_at: datetime


class OrganizationPublicPage(BaseModel):
    items: list[OrganizationPublic]
    page: int
    page_size: int
    total: int


class OrganizationReadPage(BaseModel):
    items: list[OrganizationRead]
    page: int
    page_size: int
    total: int


class MembershipReadPage(BaseModel):
    items: list[MembershipRead]
    page: int
    page_size: int
    total: int
