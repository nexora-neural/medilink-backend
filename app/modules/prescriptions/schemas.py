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


class PrescriptionDetailPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[PrescriptionDetail]
    page: int
    page_size: int
    total: int


class ClinicalWriteErrorResponse(BaseModel):
    code: str
    message: str
    field_errors: dict[str, list[str]]
    request_id: str
