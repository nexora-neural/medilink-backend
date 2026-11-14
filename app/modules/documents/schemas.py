"""Pydantic request and response schemas for private documents."""


from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class DocumentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    owner_user_id: UUID
    patient_user_id: UUID | None
    uploaded_by: UUID | None
    source: str
    document_type: str
    original_filename: str
    media_type: str
    byte_size: int
    sha256: str | None
    status: str
    created_at: datetime
    supersedes_id: UUID | None
