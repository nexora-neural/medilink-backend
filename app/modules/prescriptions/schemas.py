"""Pydantic request and response schemas for prescriptions and OCR workflow."""


from datetime import datetime
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)


class MedicineInput(BaseModel):
    """Input for an administrator-created sample medicine."""

    model_config = ConfigDict(extra="forbid")

    generic_name: str = Field(min_length=1, max_length=200)
    brand_name: str | None = Field(default=None, max_length=200)
    strength: str | None = Field(default=None, max_length=100)
    form: str | None = Field(default=None, max_length=100)

    @field_validator(
        "generic_name", "brand_name", "strength", "form"
    )
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
