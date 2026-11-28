"""Pydantic request and response schemas for prescriptions and OCR workflow."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


class MedicineInput(BaseModel):
    """Input for an administrator-created sample medicine."""

    model_config = ConfigDict(extra="forbid")

    generic_name: str = Field(min_length=1, max_length=200)
    brand_name: str | None = Field(default=None, max_length=200)
    strength: str | None = Field(default=None, max_length=100)
    form: str | None = Field(default=None, max_length=100)

    @field_validator("generic_name", "brand_name", "strength", "form")
    @classmethod
    def validate_text(cls, value: str | None) -> str | None:
        if value is None:
            return None

        value = value.strip()

        if not value:
            raise ValueError("Value cannot be empty.")

        return value


class MedicinesRead(BaseModel):
    """Public medicine catalogue entry."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    generic_name: str
    brand_name: str | None
    strength: str | None
    form: str | None
    is_sample: bool
    created_at: datetime


class MedicinesReadPage(BaseModel):
    """Paginated catalogue response."""

    items: list[MedicinesRead]
    page: int
    page_size: int
    total: int


# ML-112 prescription draft editing schemas


class PrescriptionItemInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    medicine_id: UUID | None = None
    medicine_name_snapshot: str = Field(min_length=1, max_length=200)
    dose_text: str = Field(min_length=1, max_length=200)
    route: str | None = Field(default=None, max_length=100)
    frequency: str = Field(min_length=1, max_length=200)
    duration_days: int | None = Field(default=None, ge=1)
    instructions: str | None = Field(default=None, max_length=2000)
    quantity: Decimal | None = Field(
        default=None,
        gt=0,
        max_digits=12,
        decimal_places=3,
    )
    quantity_unit: Literal["tablet", "capsule", "mL"] | None = None
    is_prn: bool

    @field_validator(
        "medicine_name_snapshot",
        "dose_text",
        "frequency",
    )
    @classmethod
    def required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Value must not be blank.")
        return value

    @field_validator("route", "instructions")
    @classmethod
    def optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @model_validator(mode="after")
    def quantity_pair(self):
        if (self.quantity is None) != (self.quantity_unit is None):
            raise ValueError("quantity and quantity_unit must be supplied together.")
        return self


class PrescriptionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    consultation_id: UUID | None = None
    claimed_prescriber: str | None = Field(default=None, max_length=200)
    starts_on: date | None = None
    ends_on: date | None = None
    original_document_id: UUID | None = None
    items: list[PrescriptionItemInput]

    @field_validator("claimed_prescriber")
    @classmethod
    def clean_claimed_prescriber(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @model_validator(mode="after")
    def date_order(self):
        if (
            self.starts_on is not None
            and self.ends_on is not None
            and self.ends_on < self.starts_on
        ):
            raise ValueError("ends_on must be on or after starts_on.")
        return self


class PrescriptionIssueItemInput(PrescriptionItemInput):
    """Complete medication fields required before doctor issuance."""

    route: str = Field(min_length=1, max_length=100)
    duration_days: int = Field(ge=1)
    quantity: Decimal = Field(
        gt=0,
        max_digits=12,
        decimal_places=3,
    )
    quantity_unit: Literal["tablet", "capsule", "mL"]

    @field_validator("route")
    @classmethod
    def require_issue_route(cls, value: str | None) -> str:
        if value is None or not value.strip():
            raise ValueError("Route is required before issuing.")
        return value.strip()


class PrescriptionIssueContent(PrescriptionInput):
    """Validate saved draft content before changing its state."""

    starts_on: date
    ends_on: date
    items: list[PrescriptionIssueItemInput] = Field(min_length=1)


class PrescriptionReasonInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def nonblank_reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("A reason is required.")
        return value


class PrescriptionVersionInput(PrescriptionInput, PrescriptionReasonInput):
    items: list[PrescriptionItemInput] = Field(min_length=1, max_length=100)


class PrescriptionLifecycleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    prescription_id: UUID
    related_prescription_id: UUID | None
    actor_user_id: UUID
    action: str
    reason: str
    previous_status: str | None
    status: str
    previous_version: int
    new_version: int
    created_at: datetime


class PrescriptionRecordRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    patient_user_id: UUID
    consultation_id: UUID | None
    prescriber_user_id: UUID | None
    source: str
    claimed_prescriber: str | None
    review_status: str
    reviewed_by: UUID | None
    reviewed_at: datetime | None
    status: str
    issued_at: datetime | None
    starts_on: date | None
    ends_on: date | None
    previous_prescription_id: UUID | None
    original_document_id: UUID | None
    created_by: UUID
    created_at: datetime
    updated_at: datetime
    version: int
    status_reason: str | None


class PrescriptionItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    prescription_id: UUID
    medicine_id: UUID | None
    medicine_name_snapshot: str
    dose_text: str
    route: str | None
    frequency: str
    duration_days: int | None
    instructions: str | None
    quantity: Decimal | None
    quantity_unit: str | None
    is_prn: bool
    created_at: datetime


class PrescriptionReviewRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    prescription_id: UUID
    reviewer_user_id: UUID
    outcome: str
    notes: str | None
    reviewed_at: datetime


class PrescriptionDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prescription: PrescriptionRecordRead
    items: list[PrescriptionItemRead]
    reviews: list[PrescriptionReviewRead]
    lifecycle_events: list[PrescriptionLifecycleRead] = Field(default_factory=list)


class PrescriptionDetailPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[PrescriptionDetail]
    page: int
    page_size: int
    total: int


class PrescriptionOcrJobInput(BaseModel):
    """Start OCR for one already-uploaded, patient-owned prescription document."""

    model_config = ConfigDict(extra="forbid")

    original_document_id: UUID


class PrescriptionOcrItemFields(BaseModel):
    """Untrusted OCR item suggestion; dose may be absent pending patient review."""

    model_config = ConfigDict(extra="forbid")

    medicine_id: UUID | None = None
    medicine_name_snapshot: str = Field(min_length=1, max_length=200)
    dose_text: str | None = Field(default=None, max_length=200)
    route: str | None = Field(default=None, max_length=100)
    frequency: str = Field(min_length=1, max_length=200)
    duration_days: int | None = Field(default=None, ge=1)
    instructions: str | None = Field(default=None, max_length=2000)
    quantity: Decimal | None = Field(
        default=None,
        gt=0,
        max_digits=12,
        decimal_places=3,
    )
    quantity_unit: Literal["tablet", "capsule", "mL"] | None = None
    is_prn: bool


class PrescriptionOcrFields(BaseModel):
    """Untrusted OCR suggestions that always require patient review."""

    model_config = ConfigDict(extra="forbid")

    claimed_prescriber: str | None = Field(default=None, max_length=200)
    starts_on: date | None = None
    ends_on: date | None = None
    items: list[PrescriptionOcrItemFields] = Field(default_factory=list)

    @model_validator(mode="after")
    def date_order(self):
        if (
            self.starts_on is not None
            and self.ends_on is not None
            and self.ends_on < self.starts_on
        ):
            raise ValueError("ends_on must be on or after starts_on.")
        return self


class PrescriptionOcrJobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    patient_user_id: UUID
    original_document_id: UUID
    status: Literal["queued", "processing", "ready_for_review", "failed", "draft_created"]
    extracted_fields: PrescriptionOcrFields | None = None
    attempt_count: int
    error_code: str | None = None
    resulting_prescription_id: UUID | None = None
    created_at: datetime
    updated_at: datetime


class PrescriptionOcrJobReadPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[PrescriptionOcrJobRead]
    page: int
    page_size: int
    total: int


class PrescriptionOcrRetryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def clean_reason(cls, value: str) -> str:
        value = value.strip()
        if not value or "\x00" in value:
            raise ValueError("reason must contain non-blank text.")
        return value


class PrescriptionOcrDraftInput(BaseModel):
    """Patient-confirmed/corrected field values used to create a draft only."""

    model_config = ConfigDict(extra="forbid")

    claimed_prescriber: str | None = Field(default=None, max_length=200)
    starts_on: date | None = None
    ends_on: date | None = None
    items: list[PrescriptionItemInput] = Field(min_length=1)

    @field_validator("claimed_prescriber")
    @classmethod
    def clean_claimed_prescriber(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @model_validator(mode="after")
    def date_order(self):
        if (
            self.starts_on is not None
            and self.ends_on is not None
            and self.ends_on < self.starts_on
        ):
            raise ValueError("ends_on must be on or after starts_on.")
        return self


class PrescriptionOcrDraftCreated(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job: PrescriptionOcrJobRead
    prescription: PrescriptionDetail


class ClinicalWriteErrorResponse(BaseModel):
    code: str
    message: str
    field_errors: dict[str, list[str]]
    request_id: str
