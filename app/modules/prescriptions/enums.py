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


class PrescriptionOcrJobStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    READY_FOR_REVIEW = "ready_for_review"
    FAILED = "failed"
    DRAFT_CREATED = "draft_created"


prescription_ocr_job_status_type = ENUM(
    PrescriptionOcrJobStatus,
    name="prescription_ocr_job_status",
    values_callable=lambda enum_class: [member.value for member in enum_class],
    validate_strings=True,
)


class PrescriptionTemplateType(StrEnum):
    FAVOURITE = "favourite"
    TEMPLATE = "template"


prescription_template_type = ENUM(
    PrescriptionTemplateType,
    name="prescription_template_type",
    values_callable=lambda enum_class: [member.value for member in enum_class],
    validate_strings=True,
)


class PrescriptionTemplateStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


prescription_template_status_type = ENUM(
    PrescriptionTemplateStatus,
    name="prescription_template_status",
    values_callable=lambda enum_class: [member.value for member in enum_class],
    validate_strings=True,
)
