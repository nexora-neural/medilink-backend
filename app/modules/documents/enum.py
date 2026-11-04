from enum import Enum

from sqlalchemy.dialects.postgresql import ENUM

from app.database.base import Base


class RecordSource(str, Enum):
    PATIENT = "patient"
    DOCTOR = "doctor"
    PHARMACIST = "pharmacist"
    LAB_STAFF = "lab_staff"
    APPROVED_EXTERNAL_SYSTEM = "approved_external_system"
    ADMIN = "admin"


class DocumentStatus(str, Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    WITHDRAWN = "withdrawn"
    QUARANTINED = "quarantined"


record_source_type = ENUM(
    RecordSource,
    name="record_source",
    metadata=Base.metadata,
    values_callable=lambda enum_class: [
        member.value for member in enum_class
    ],
    validate_strings=True,
)


document_status_type = ENUM(
    DocumentStatus,
    name="document_status",
    metadata=Base.metadata,
    values_callable=lambda enum_class: [
        member.value for member in enum_class
    ],
    validate_strings=True,
)