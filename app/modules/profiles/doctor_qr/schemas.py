from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StringConstraints

DoctorReference = Annotated[str, StringConstraints(min_length=1)]


class DoctorQrRead(BaseModel):
    doctor_reference: DoctorReference


class DoctorQrResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doctor_reference: DoctorReference


class DoctorQrClinicRead(BaseModel):
    id: UUID
    name: str


class DoctorQrResolveRead(BaseModel):
    display_name: str
    verification_status: str
    clinics: list[DoctorQrClinicRead]