from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StringConstraints

DoctorReference = Annotated[str, StringConstraints(min_length=1, max_length=256)]


class DoctorQrRead(BaseModel):
    doctor_reference: DoctorReference
    qr_payload: str


class DoctorQrResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doctor_reference: DoctorReference


class ProfessionalIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    role: Literal["doctor"]
    display_name: str
    verification_status: str
    organization_id: UUID | None = None
    organization_name: str | None = None