"""Prescription sharing enum values and PostgreSQL mappings."""

from enum import StrEnum

from sqlalchemy.dialects.postgresql import ENUM


class ShareStatus(StrEnum):
    CREATED = "created"
    CLAIMED = "claimed"
    CONFIRMED = "confirmed"
    EXPIRED = "expired"
    REVOKED = "revoked"


share_status_type = ENUM(
    ShareStatus,
    name="share_status",
    values_callable=lambda enum_class: [
        member.value for member in enum_class
    ],
    validate_strings=True,
)
