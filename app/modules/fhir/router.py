"""Authenticated HTTP routes for limited FHIR R4 exports."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import get_session
from app.modules.fhir import service
from app.modules.fhir.schemas import (
    FhirErrorResponse,
    FhirMedicationRequest,
    FhirPatient,
)
from app.modules.identity.auth import get_current_user
from app.modules.identity.models import User
from app.modules.identity.request_limits import require_authenticated_rate_limit


class FHIRJSONResponse(JSONResponse):
    media_type = "application/fhir+json"


router = APIRouter(
    prefix="/fhir/r4",
    tags=["FHIR export"],
    dependencies=[Depends(require_authenticated_rate_limit)],
)

SessionDep = Annotated[AsyncSession, Depends(get_session)]
UserDep = Annotated[User, Depends(get_current_user)]

ERROR_RESPONSE_SCHEMA = FhirErrorResponse.model_json_schema()
ERROR_RESPONSES = {
    code: {
        "description": description,
        "content": {"application/json": {"schema": ERROR_RESPONSE_SCHEMA}},
    }
    for code, description in {
        400: "Invalid action or malformed request.",
        401: "Missing or invalid identity.",
        403: "The account is inactive or the doctor is not verified.",
        404: "The resource is absent or concealed by ownership, grant, or organization access.",
        409: "The medication request is not in an exportable state.",
        422: "Malformed identifier or request validation failed.",
        429: "Authenticated request quota exceeded.",
        500: "Unexpected failure with a safe request ID.",
        503: "Authentication or request-limit service is unavailable.",
    }.items()
}


@router.get(
    "/Patient/{patient_id}",
    operation_id="medilink_123",
    response_model=FhirPatient,
    response_class=FHIRJSONResponse,
    status_code=status.HTTP_200_OK,
    summary="Export a limited FHIR R4 (4.0.1) Patient",
    description=(
        "Allowed actor: patient reading their own record or verified doctor with a current "
        "history_allergies grant. Returns only identity fields: internal UUID id, "
        "urn:medilink:patient identifier, authorized display name, and birthDate when available. "
        "Successful responses use application/fhir+json; inaccessible resources are concealed. "
        "FHIR search, write operations, and full server conformance are not supported."
    ),
    responses=ERROR_RESPONSES,
)
async def get_patient_export(
    patient_id: UUID,
    session: SessionDep,
    current_user: UserDep,
    response: Response,
) -> FhirPatient:
    result = await service.export_patient(
        session=session,
        user=current_user,
        patient_id=patient_id,
    )
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get(
    "/MedicationRequest/{item_id}",
    operation_id="medilink_124",
    response_model=FhirMedicationRequest,
    response_class=FHIRJSONResponse,
    status_code=status.HTTP_200_OK,
    summary="Export one doctor-issued FHIR R4 (4.0.1) MedicationRequest",
    description=(
        "Allowed actor: patient reading their own item or verified doctor with a current "
        "prescriptions grant. One resource per doctor-issued item; patient-source and "
        "external-system items return 404 even if reviewed. Draft or missing issued_at returns "
        "409. Maps active to active, completed to completed, discontinued to stopped, cancelled "
        "and superseded to cancelled. The subject references Patient/{patient_uuid}; requester "
        "is omitted until Practitioner export is supported. Successful responses use "
        "application/fhir+json; FHIR search, write operations, Bundles, SMART authentication, "
        "and full server conformance are not supported."
    ),
    responses=ERROR_RESPONSES,
)
async def get_medication_request_export(
    item_id: UUID,
    session: SessionDep,
    current_user: UserDep,
    response: Response,
) -> FhirMedicationRequest:
    result = await service.export_medication_request(
        session=session,
        user=current_user,
        item_id=item_id,
    )
    response.headers["Cache-Control"] = "no-store"
    return result
