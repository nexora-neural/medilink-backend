"""Request and response models for account support."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StringConstraints

from app.modules.identity.enums import AccountStatus


class UsersRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str | None = None
    display_name: str
    phone: str | None = None
    status: AccountStatus
    is_admin: bool
    created_at: datetime
    updated_at: datetime


class UsersReadPage(BaseModel):
    items: list[UsersRead]
    page: int
    page_size: int
    total: int


class ReasonInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True, min_length=1, max_length=1000, pattern=r"^[^\x00]+$"
        ),
    ]


class AccountDecision(ReasonInput):
    status: Literal["active", "suspended", "disabled"]


class AdminRoleInput(ReasonInput):
    user_id: UUID
    role: Literal["admin"]


class ActionResult(BaseModel):
    id: UUID
    status: str
    updated_at: datetime


class AccountSupportErrorResponse(BaseModel):
    code: str
    message: str
    field_errors: dict[str, list[str]]
    request_id: str
