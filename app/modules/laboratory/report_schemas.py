from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.modules.clinical_records.models import RecordSource
from app.modules.laboratory.models import ReportStatus


class ReportInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    document_id: UUID
    report_type: str = Field(min_length=1, max_length=200)
    report_date: date | None = None
    upload_request_id: UUID | None = None
    lab_reference: str | None = Field(default=None, max_length=150)
    notes: str | None = Field(default=None, max_length=2000)


class ReportsRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    patient_user_id: UUID
    laboratory_id: UUID | None
    report_identity_id: UUID | None
    upload_request_id: UUID | None
    document_id: UUID
    report_type: str
    report_date: date | None
    notes: str | None
    source: RecordSource
    uploaded_by: UUID | None
    status: ReportStatus
    created_at: datetime
    supersedes_id: UUID | None
    correction_reason: str | None


class ReportsReadPage(BaseModel):
    items: list[ReportsRead]
    page: int
    page_size: int
    total: int
