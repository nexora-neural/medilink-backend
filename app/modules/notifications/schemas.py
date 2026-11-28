"""Pydantic request and response schemas for notifications."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class NotificationsRead(BaseModel):
    """The contract projection for a notification owned by the caller."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    user_id: UUID
    notification_type: str
    title: str
    body: str
    related_type: str | None = None
    related_id: UUID | None = None
    created_at: datetime
    read_at: datetime | None = None


class NotificationsReadPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[NotificationsRead]
    page: int
    page_size: int
    total: int


class NotificationsErrorResponse(BaseModel):
    code: str
    message: str
    field_errors: dict[str, list[str]] = Field(default_factory=dict)
    request_id: str
