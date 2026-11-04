"""Enum definitions for healthcare organizations."""

from enum import StrEnum

from sqlalchemy.dialects.postgresql import ENUM


class OrganizationType(StrEnum):
    CLINIC = "clinic"
    HOSPITAL = "hospital"
    PHARMACY = "pharmacy"
    LABORATORY = "laboratory"
    OTHER = "other"


class EntityStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    REJECTED = "rejected"
    INACTIVE = "inactive"


organization_type_type = ENUM(
    OrganizationType,
    name="organization_type",
    values_callable=lambda enum_class: [member.value for member in enum_class],
    validate_strings=True,
)

entity_status_type = ENUM(
    EntityStatus,
    name="entity_status",
    values_callable=lambda enum_class: [member.value for member in enum_class],
    validate_strings=True,
)
