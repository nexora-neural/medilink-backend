"""Enum values and SQLAlchemy PostgreSQL enum mappings."""

from enum import Enum

from sqlalchemy.dialects.postgresql import ENUM


class AccountStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DISABLED = "disabled"


class ProfessionalRole(str, Enum):
    DOCTOR = "doctor"
    PHARMACIST = "pharmacist"
    LAB_STAFF = "lab_staff"


class VerificationStatus(str, Enum):
    NOT_SUBMITTED = "not_submitted"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    MORE_INFO_REQUESTED = "more_info_requested"
    SUSPENDED = "suspended"


# Store enum values ("pending"), rather than member names ("PENDING").
account_status_type = ENUM(
    AccountStatus,
    name="account_status",
    values_callable=lambda enum_class: [
        member.value for member in enum_class
    ],
    create_type=False,
    validate_strings=True,
)

professional_role_type = ENUM(
    ProfessionalRole,
    name="professional_role",
    values_callable=lambda enum_class: [
        member.value for member in enum_class
    ],
    create_type=False,
    validate_strings=True,
)

verification_status_type = ENUM(
    VerificationStatus,
    name="verification_status",
    values_callable=lambda enum_class: [
        member.value for member in enum_class
    ],
    create_type=False,
    validate_strings=True,
)