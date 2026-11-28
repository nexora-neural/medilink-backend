"""Limited FHIR R4 resource and safe error schemas."""

from datetime import date
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class FhirElement(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FhirIdentifier(FhirElement):
    system: str = Field(min_length=1)
    value: str = Field(min_length=1)


class FhirHumanName(FhirElement):
    text: str = Field(min_length=1)


class FhirReference(FhirElement):
    reference: str = Field(min_length=1)


class FhirCodeableConcept(FhirElement):
    text: str = Field(min_length=1)


class FhirDosage(FhirElement):
    text: str = Field(min_length=1)
    asNeededBoolean: bool


class FhirPatient(FhirElement):
    resourceType: Literal["Patient"]
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9.\-]+$")
    identifier: list[FhirIdentifier] = Field(min_length=1)
    name: list[FhirHumanName] = Field(min_length=1)
    birthDate: date | None = None


class FhirMedicationRequest(FhirElement):
    resourceType: Literal["MedicationRequest"]
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9.\-]+$")
    status: Literal["active", "completed", "stopped", "cancelled", "draft", "entered-in-error"]
    intent: Literal["order"]
    subject: FhirReference
    medicationCodeableConcept: FhirCodeableConcept
    authoredOn: AwareDatetime
    dosageInstruction: list[FhirDosage] = Field(min_length=1)


class FhirErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    field_errors: dict[str, list[str]] = Field(default_factory=dict)
    request_id: str
