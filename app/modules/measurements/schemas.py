from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

MeasurementType = Literal["weight", "blood_pressure", "blood_glucose"]
Unit = Literal["kg", "lb", "mmol/L", "mg/dL", "mmHg"]
Quantity = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=4)]


class ReadingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Quantity
    secondary_value: Quantity | None = None
    unit: Unit
    measured_at: AwareDatetime
    notes: Annotated[str, Field(max_length=2000)] | None = None

    @field_validator("value", "secondary_value", mode="before")
    @classmethod
    def json_number(cls, value):
        if type(value) not in (int, float):
            raise ValueError("Use a JSON number.")
        return value

    @field_validator("measured_at", mode="before")
    @classmethod
    def json_timestamp(cls, value):
        if not isinstance(value, str) or "T" not in value.upper():
            raise ValueError("Use an RFC3339 timestamp with a timezone.")
        return value

    @field_validator("notes", mode="before")
    @classmethod
    def nonnull_notes(cls, value):
        if value is None:
            raise ValueError("Omit notes if there are none.")
        return value

    def validate_type(self, measurement_type: MeasurementType) -> None:
        units = {
            "weight": {"kg", "lb"},
            "blood_pressure": {"mmHg"},
            "blood_glucose": {"mmol/L", "mg/dL"},
        }
        if self.unit not in units[measurement_type]:
            raise ValueError("Unit does not match the measurement type.")
        if (measurement_type == "blood_pressure") != (self.secondary_value is not None):
            raise ValueError("Provide a secondary value only for blood pressure.")


class MeasurementInput(ReadingInput):
    type: MeasurementType
    consultation_id: UUID | None = None

    @field_validator("consultation_id", mode="before")
    @classmethod
    def nonnull_consultation(cls, value):
        if value is None:
            raise ValueError("Omit consultation_id when it does not apply.")
        return value

    @model_validator(mode="after")
    def check_reading(self):
        self.validate_type(self.type)
        return self


class MeasurementCorrectionInput(ReadingInput):
    reason: Annotated[str, Field(min_length=1, max_length=2000)]

    @field_validator("reason")
    @classmethod
    def clean_reason(cls, value):
        if not value.strip():
            raise ValueError("A nonblank reason is required.")
        return value.strip()


class MeasurementsRead(BaseModel):
    id: UUID
    patient_user_id: UUID
    type: MeasurementType
    value: float
    secondary_value: float | None
    unit: Unit
    measured_at: AwareDatetime
    recorded_by: UUID
    source: Literal["patient", "doctor", "pharmacist", "lab_staff",
                    "approved_external_system", "admin"]
    notes: str | None
    status: Literal["active", "superseded", "entered_in_error"]
    supersedes_id: UUID | None
    correction_reason: str | None
    created_at: datetime


class MeasurementsReadPage(BaseModel):
    items: list[MeasurementsRead]
    page: int
    page_size: int
    total: int
