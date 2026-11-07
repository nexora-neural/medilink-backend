"""Patient measurement database model."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Numeric, Text, text
from sqlalchemy.dialects.postgresql import ENUM
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import conv

from app.database.base import Base
from app.modules.clinical_records.models import RecordSource, record_source_enum


class MeasurementType(StrEnum):
    WEIGHT = "weight"
    BLOOD_PRESSURE = "blood_pressure"
    BLOOD_GLUCOSE = "blood_glucose"


class MeasurementStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    ENTERED_IN_ERROR = "entered_in_error"


def enum_type(cls, name):
    return ENUM(cls, name=name, values_callable=lambda enum: [x.value for x in enum])


class Measurement(Base):
    __tablename__ = "measurements"
    __table_args__ = (
        CheckConstraint(
            "(type = 'blood_pressure' AND value IS NOT NULL AND secondary_value IS NOT NULL "
            "AND unit = 'mmHg') OR (type <> 'blood_pressure' AND value IS NOT NULL "
            "AND secondary_value IS NULL)",
            name=conv("ck_measurement_values"),
        ),
        CheckConstraint("value IS NULL OR value > 0", name=conv("ck_measurement_positive")),
        CheckConstraint(
            "secondary_value IS NULL OR secondary_value > 0",
            name=conv("ck_measurement_secondary_positive"),
        ),
        CheckConstraint(
            "(type = 'weight' AND unit IN ('kg','lb')) OR "
            "(type = 'blood_glucose' AND unit IN ('mmol/L','mg/dL')) OR "
            "(type = 'blood_pressure' AND unit = 'mmHg')",
            name=conv("ck_measurement_unit"),
        ),
        CheckConstraint(
            "(supersedes_id IS NULL) = (correction_reason IS NULL)",
            name=conv("ck_measurement_correction"),
        ),
        Index(
            "ix_measurements_patient_type_date", "patient_user_id", "type", text("measured_at DESC")
        ),
        Index(
            "uq_measurements_supersedes_id",
            "supersedes_id",
            unique=True,
            postgresql_where=text("supersedes_id IS NOT NULL"),
        ),
    )
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    patient_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("patient_profiles.user_id", ondelete="RESTRICT")
    )
    type: Mapped[MeasurementType] = mapped_column(enum_type(MeasurementType, "measurement_type"))
    value: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    secondary_value: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    unit: Mapped[str] = mapped_column(Text)
    measured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_by: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    source: Mapped[RecordSource] = mapped_column(record_source_enum)
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[MeasurementStatus] = mapped_column(
        enum_type(MeasurementStatus, "measurement_status"), server_default=text("'active'")
    )
    supersedes_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("measurements.id", ondelete="RESTRICT")
    )
    correction_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
