"""Authorization and mapping for the limited FHIR R4 export endpoints."""

from datetime import UTC
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.fhir import repository
from app.modules.fhir.schemas import (
    FhirCodeableConcept,
    FhirDosage,
    FhirHumanName,
    FhirIdentifier,
    FhirMedicationRequest,
    FhirPatient,
    FhirReference,
)
from app.modules.identity.authorization import require_doctor_grant
from app.modules.identity.models import User


class FhirExportError(Exception):
    """Safe, contract-shaped FHIR endpoint error."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _error(status_code: int, code: str, message: str) -> FhirExportError:
    return FhirExportError(status_code, code, message)


def _not_found() -> FhirExportError:
    return _error(404, "RESOURCE_NOT_FOUND", "The requested resource is not available.")


def _denied() -> FhirExportError:
    return _error(403, "ACCESS_DENIED", "You do not have access to this record.")


async def _authorize_read(
    *,
    session: AsyncSession,
    user: User,
    patient_id: UUID,
    category: str,
) -> None:
    if user.status != "active":
        raise _denied()

    roles = await repository.get_active_role_codes(session, user.id)
    if user.id == patient_id and "patient" in roles:
        return

    if "doctor" not in roles:
        if "patient" in roles:
            raise _not_found()
        raise _denied()

    if not await repository.is_verified_doctor(session, user.id):
        if "patient" in roles:
            raise _not_found()
        raise _denied()

    organizations = await repository.get_current_grant_organizations(
        session,
        patient_id=patient_id,
        doctor_id=user.id,
        category=category,
    )
    if not organizations:
        # Conceal whether the patient or resource exists when the doctor has no
        # current grant for this category.
        raise _not_found()

    for organization_id in organizations:
        try:
            await require_doctor_grant(
                session,
                user,
                patient_id,
                organization_id,
                category,
            )
        except HTTPException:
            continue
        return

    # A matching grant row can still be unusable if its organization membership
    # was revoked or the organization became inactive after the query.
    raise _not_found()


async def _require_medication_export_actor(session: AsyncSession, user: User) -> None:
    """Authorize the actor class before lookup to keep inaccessible IDs indistinguishable."""
    if user.status != "active":
        raise _denied()
    roles = await repository.get_active_role_codes(session, user.id)
    if "patient" in roles:
        return
    if "doctor" not in roles or not await repository.is_verified_doctor(session, user.id):
        raise _denied()


async def export_patient(
    *,
    session: AsyncSession,
    user: User,
    patient_id: UUID,
) -> FhirPatient:
    await _authorize_read(
        session=session,
        user=user,
        patient_id=patient_id,
        category="history_allergies",
    )
    row = await repository.get_patient(session, patient_id)
    if row is None:
        raise _not_found()
    patient, profile = row
    display_name = patient.display_name.strip()
    if not display_name:
        raise _error(409, "INVALID_STATE", "The patient identity cannot be exported.")

    return FhirPatient(
        resourceType="Patient",
        id=str(patient.id),
        identifier=[FhirIdentifier(system="urn:medilink:patient", value=str(patient.id))],
        name=[FhirHumanName(text=display_name)],
        birthDate=profile.date_of_birth,
    )


def _fhir_status(status: str) -> str:
    mapping = {
        "active": "active",
        "completed": "completed",
        "discontinued": "stopped",
        "cancelled": "cancelled",
        "superseded": "cancelled",
    }
    try:
        return mapping[status]
    except KeyError:
        raise _error(409, "INVALID_STATE", "This medication request is not exportable.") from None


def _dosage_text(item) -> str:
    parts = []
    if item.dose_text and item.dose_text.strip():
        parts.append(f"Dose: {item.dose_text.strip()}")
    if item.route and item.route.strip():
        parts.append(f"Route: {item.route.strip()}")
    if item.frequency and item.frequency.strip():
        parts.append(f"Frequency: {item.frequency.strip()}")
    if item.duration_days is not None:
        parts.append(f"Duration: {item.duration_days} days")
    if item.instructions and item.instructions.strip():
        parts.append(f"Instructions: {item.instructions.strip()}")
    if not parts:
        raise _error(409, "INVALID_STATE", "The medication instructions cannot be exported.")
    return "; ".join(parts)


async def export_medication_request(
    *,
    session: AsyncSession,
    user: User,
    item_id: UUID,
) -> FhirMedicationRequest:
    await _require_medication_export_actor(session, user)
    item_row = await repository.get_medication_item(session, item_id)
    if item_row is None:
        raise _not_found()

    item, prescription, patient, _profile = item_row
    # Provenance is authoritative: review status never upgrades patient-source
    # orders into prescriptions issued by a doctor.
    if prescription.source != "doctor":
        raise _not_found()

    await _authorize_read(
        session=session,
        user=user,
        patient_id=prescription.patient_user_id,
        category="prescriptions",
    )

    if prescription.status == "draft" or prescription.issued_at is None:
        raise _error(409, "INVALID_STATE", "This medication request has not been issued.")
    if prescription.issued_at.tzinfo is None or prescription.issued_at.utcoffset() is None:
        raise _error(409, "INVALID_STATE", "The medication issue time cannot be exported.")

    medicine_name = item.medicine_name_snapshot.strip()
    if not medicine_name:
        raise _error(409, "INVALID_STATE", "The medication name cannot be exported.")

    return FhirMedicationRequest(
        resourceType="MedicationRequest",
        id=str(item.id),
        status=_fhir_status(str(prescription.status)),
        intent="order",
        subject=FhirReference(reference=f"Patient/{patient.id}"),
        medicationCodeableConcept=FhirCodeableConcept(text=medicine_name),
        authoredOn=prescription.issued_at.astimezone(UTC),
        dosageInstruction=[FhirDosage(text=_dosage_text(item), asNeededBoolean=item.is_prn)],
    )
