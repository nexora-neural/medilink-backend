"""API router for prescriptions, OCR workflow, and medicine catalogue."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import get_session
from app.modules.identity.auth import get_current_user
from app.modules.identity.models import User
from app.modules.identity.request_limits import (
    require_authenticated_rate_limit,
    require_upload_rate_limit,
)
from app.modules.prescriptions import ocr_service, service
from app.modules.prescriptions.schemas import (
    ClinicalWriteErrorResponse,
    MedicineInput,
    MedicinesRead,
    MedicinesReadPage,
    PrescriptionDetail,
    PrescriptionDetailPage,
    PrescriptionInput,
    PrescriptionOcrDraftCreated,
    PrescriptionOcrDraftInput,
    PrescriptionOcrJobInput,
    PrescriptionOcrJobRead,
    PrescriptionOcrJobReadPage,
    PrescriptionOcrRetryInput,
)

router = APIRouter(tags=["Prescriptions"])


# Medicines router.
medicine_router = APIRouter(
    tags=["Medicines"],
    dependencies=[Depends(require_authenticated_rate_limit)],
)


SessionDep = Annotated[AsyncSession, Depends(get_session)]
UserDep = Annotated[User, Depends(get_current_user)]


@router.post(
    "/prescriptions/{prescription_id}/confirm",
    operation_id="medilink_048",
    response_model=PrescriptionDetail,
    status_code=status.HTTP_200_OK,
    summary="Confirm patient-added draft for personal use",
    description=(
        "After reviewing the saved items, the owning patient explicitly confirms "
        "the draft. No body or query parameters. Source remains patient and "
        "confirmation does not grant clinical verification or dispensing permission."
    ),
    dependencies=[Depends(require_authenticated_rate_limit)],
    responses={
        code: {"model": ClinicalWriteErrorResponse, "description": description}
        for code, description in {
            400: "Request body is not allowed",
            401: "Missing or invalid identity",
            403: "Current patient role required or forbidden source",
            404: "Prescription absent or concealed",
            409: "Invalid prescription state",
            422: "Invalid saved items, references or query parameters",
            429: "Authenticated request quota exceeded",
            500: "Safe internal error",
            503: "Request quota store unavailable",
        }.items()
    },
)
async def confirm_patient_prescription(
    prescription_id: UUID,
    request: Request,
    response: Response,
    session: SessionDep,
    current_user: UserDep,
) -> PrescriptionDetail:
    """The explicit action confirms saved items; clients cannot promote source."""
    if request.query_params:
        raise service.confirmation_error(
            request,
            422,
            "VALIDATION_ERROR",
            "Query parameters are not allowed.",
            {"query": ["Unknown query parameter."]},
        )
    # Read at most the first nonempty chunk: never buffer an unsolicited upload.
    async for chunk in request.stream():
        if chunk:
            raise service.confirmation_error(
                request, 400, "INVALID_REQUEST", "This operation does not accept a body."
            )
    result = await service.confirm_patient_draft(
        session=session,
        user=current_user,
        request=request,
        prescription_id=prescription_id,
    )
    response.headers["Cache-Control"] = "no-store"
    return result


@medicine_router.get(
    "/medicines",
    response_model=MedicinesReadPage,
    status_code=status.HTTP_200_OK,
    summary="Search sample medicine catalogue",
)
async def get_medicines(
    request: Request,
    session: SessionDep,
    current_user: UserDep,
    q: Annotated[str | None, Query(max_length=200)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> MedicinesReadPage:
    unknown = set(request.query_params) - {"q", "page", "page_size"}
    if unknown:
        return JSONResponse(
            status_code=422,
            content={
                "code": "VALIDATION_ERROR",
                "message": "Please check the highlighted fields.",
                "field_errors": {"query": ["Unknown query parameter."]},
                "request_id": getattr(request.state, "request_id", ""),
            },
        )
    return await service.list_medicines(
        session=session,
        user=current_user,
        request=request,
        q=q,
        page=page,
        page_size=page_size,
    )


@medicine_router.get(
    "/admin/medicines",
    response_model=MedicinesReadPage,
    summary="List sample medicine catalogue entries for management",
)
async def get_admin_medicines(
    request: Request,
    session: SessionDep,
    current_user: UserDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> MedicinesReadPage:
    return await service.list_admin_medicines(
        session=session,
        user=current_user,
        request=request,
        page=page,
        page_size=page_size,
    )


@medicine_router.post(
    "/admin/medicines",
    response_model=MedicinesRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add curated sample catalogue entry",
    responses={409: {"description": "A sample medicine with these details already exists."}},
)
async def create_medicine(
    payload: MedicineInput,
    request: Request,
    session: SessionDep,
    current_user: UserDep,
) -> MedicinesRead:

    return await service.add_medicine(
        session=session,
        user=current_user,
        request=request,
        payload=payload,
    )


@router.put(
    "/prescriptions/{prescription_id}/draft",
    response_model=PrescriptionDetail,
    status_code=status.HTTP_200_OK,
    responses={
        403: {"description": "Access denied"},
        404: {"description": "Prescription not found"},
        409: {"description": "Stale version or invalid state"},
        422: {"description": "Validation error"},
    },
)
async def update_prescription_draft(
    prescription_id: UUID,
    payload: PrescriptionInput,
    request: Request,
    session: SessionDep,
    current_user: UserDep,
    if_match: str = Header(alias="If-Match"),
):
    """Replace an owned draft prescription using optimistic concurrency."""

    try:
        expected_version = int(if_match.strip().strip('"'))
        if expected_version < 1:
            raise ValueError
    except ValueError:
        from app.modules.identity.auth import AuthenticationError

        raise AuthenticationError(
            status_code=422,
            detail={
                "code": "VALIDATION_ERROR",
                "message": "If-Match must contain a positive prescription version.",
                "field_errors": {},
                "request_id": request.state.request_id,
            },
        ) from None

    return await service.replace_prescription_draft(
        session=session,
        user=current_user,
        request=request,
        prescription_id=prescription_id,
        expected_version=expected_version,
        payload=payload,
    )


@router.get(
    "/patients/{patient_id}/prescriptions",
    response_model=PrescriptionDetailPage,
    status_code=status.HTTP_200_OK,
)
async def get_patient_prescriptions(
    patient_id: UUID,
    request: Request,
    session: SessionDep,
    current_user: UserDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    status_filter: Annotated[
        str | None,
        Query(
            alias="status",
            pattern=(
                r"^(draft|active|completed|discontinued|"
                r"cancelled|superseded)$"
            ),
        ),
    ] = None,
    source: Annotated[
        str | None,
        Query(pattern=r"^(patient|doctor|approved_external_system)$"),
    ] = None,
) -> PrescriptionDetailPage:
    return await service.list_prescriptions(
        session=session,
        user=current_user,
        request=request,
        patient_id=patient_id,
        page=page,
        page_size=page_size,
        status=status_filter,
        source=source,
    )


@router.get(
    "/prescriptions/{prescription_id}",
    response_model=PrescriptionDetail,
    status_code=status.HTTP_200_OK,
)
async def get_prescription(
    prescription_id: UUID,
    request: Request,
    session: SessionDep,
    current_user: UserDep,
) -> PrescriptionDetail:
    return await service.read_prescription(
        session=session,
        user=current_user,
        request=request,
        prescription_id=prescription_id,
    )


@router.post(
    "/patients/{patient_id}/prescriptions",
    response_model=PrescriptionDetail,
    status_code=status.HTTP_201_CREATED,
    responses={
        403: {"description": "Access denied"},
        422: {"description": "Validation error"},
    },
)
async def create_patient_prescription(
    patient_id: UUID,
    payload: PrescriptionInput,
    request: Request,
    session: SessionDep,
    current_user: UserDep,
) -> PrescriptionDetail:
    return await service.create_draft(
        session=session,
        user=current_user,
        request=request,
        patient_id=patient_id,
        payload=payload,
    )


@router.delete(
    "/prescriptions/{prescription_id}/draft",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses={
        403: {"description": "Access denied"},
        404: {"description": "Prescription not found"},
        409: {"description": "Invalid state or linked records"},
    },
)
async def delete_prescription_draft(
    prescription_id: UUID,
    request: Request,
    session: SessionDep,
    current_user: UserDep,
) -> Response:
    await service.delete_draft(
        session=session,
        user=current_user,
        request=request,
        prescription_id=prescription_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/prescriptions/{prescription_id}/issue",
    response_model=PrescriptionDetail,
    status_code=status.HTTP_200_OK,
    responses={
        403: {"description": "Access denied"},
        404: {"description": "Prescription not found"},
        409: {"description": "Invalid prescription state"},
        422: {"description": "Incomplete prescription details"},
    },
)
async def issue_doctor_prescription(
    prescription_id: UUID,
    request: Request,
    session: SessionDep,
    current_user: UserDep,
) -> PrescriptionDetail:
    return await service.issue_prescription(
        session=session,
        user=current_user,
        request=request,
        prescription_id=prescription_id,
    )


_OCR_ERROR_RESPONSES = {
    code: {"model": ClinicalWriteErrorResponse, "description": description}
    for code, description in {
        400: "Invalid action or malformed request",
        401: "Missing or invalid identity",
        403: "Permission denied or inactive account",
        404: "Resource absent or not visible",
        409: "State conflict, duplicate job or idempotency mismatch",
        422: "Field or business validation failed",
        429: "Rate limit exceeded",
        500: "Unexpected failure with safe request_id",
        503: "Rate limit or authentication store unavailable",
    }.items()
}
_OCR_STATUS_PATTERN = r"^(queued|processing|ready_for_review|failed|draft_created)$"


def _reject_unknown_query(request: Request, allowed: set[str]) -> None:
    unknown = set(request.query_params) - allowed
    if unknown:
        raise ocr_service._error(
            request,
            422,
            "VALIDATION_ERROR",
            "Invalid request input.",
            {"query": ["Unknown query parameter."]},
        )


@router.post(
    "/patients/{patient_id}/prescription-ocr-jobs",
    response_model=PrescriptionOcrJobRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start printed English prescription OCR",
    description=(
        "Patient: own record only. Starts an offline OCR job for an active, "
        "patient-owned prescription document detected as PDF, JPEG or PNG. "
        "Handwriting is unsupported. OCR output is untrusted and requires "
        "patient review; it never verifies, issues or authorizes dispensing."
    ),
    dependencies=[Depends(require_upload_rate_limit)],
    responses=_OCR_ERROR_RESPONSES,
)
async def create_prescription_ocr_job(
    patient_id: UUID,
    payload: PrescriptionOcrJobInput,
    request: Request,
    response: Response,
    session: SessionDep,
    current_user: UserDep,
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
    ],
) -> PrescriptionOcrJobRead:
    _reject_unknown_query(request, set())
    result = await ocr_service.create_job(
        session=session,
        user=current_user,
        request=request,
        patient_id=patient_id,
        payload=payload,
        idempotency_key=idempotency_key,
    )
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get(
    "/patients/{patient_id}/prescription-ocr-jobs",
    response_model=PrescriptionOcrJobReadPage,
    status_code=status.HTTP_200_OK,
    summary="List own prescription OCR jobs",
    description="Patient owner only. Filters by job status and supports bounded pagination.",
    dependencies=[Depends(require_authenticated_rate_limit)],
    responses=_OCR_ERROR_RESPONSES,
)
async def list_prescription_ocr_jobs(
    patient_id: UUID,
    request: Request,
    response: Response,
    session: SessionDep,
    current_user: UserDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    status_filter: Annotated[
        str | None, Query(alias="status", pattern=_OCR_STATUS_PATTERN)
    ] = None,
) -> PrescriptionOcrJobReadPage:
    _reject_unknown_query(request, {"page", "page_size", "status"})
    result = await ocr_service.list_jobs(
        session=session,
        user=current_user,
        request=request,
        patient_id=patient_id,
        page=page,
        page_size=page_size,
        status_filter=status_filter,
    )
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get(
    "/prescription-ocr-jobs/{job_id}",
    response_model=PrescriptionOcrJobRead,
    status_code=status.HTTP_200_OK,
    summary="Read OCR state and extracted draft fields",
    description="Only the owning patient can read job metadata or extracted fields.",
    dependencies=[Depends(require_authenticated_rate_limit)],
    responses=_OCR_ERROR_RESPONSES,
)
async def read_prescription_ocr_job(
    job_id: UUID,
    request: Request,
    response: Response,
    session: SessionDep,
    current_user: UserDep,
) -> PrescriptionOcrJobRead:
    _reject_unknown_query(request, set())
    result = await ocr_service.read_job(
        session=session, user=current_user, request=request, job_id=job_id
    )
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post(
    "/prescription-ocr-jobs/{job_id}/retry",
    response_model=PrescriptionOcrJobRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Retry a failed prescription OCR job",
    description=(
        "The owning patient can queue a failed job again while it is below the "
        "configured total-attempt limit. The retained original file is reused."
    ),
    dependencies=[Depends(require_authenticated_rate_limit)],
    responses=_OCR_ERROR_RESPONSES,
)
async def retry_prescription_ocr_job(
    job_id: UUID,
    payload: PrescriptionOcrRetryInput,
    request: Request,
    response: Response,
    session: SessionDep,
    current_user: UserDep,
) -> PrescriptionOcrJobRead:
    _reject_unknown_query(request, set())
    result = await ocr_service.retry_job(
        session=session,
        user=current_user,
        request=request,
        job_id=job_id,
        payload=payload,
    )
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post(
    "/patients/{patient_id}/prescription-ocr-jobs/{job_id}/draft",
    response_model=PrescriptionOcrDraftCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Save patient-reviewed OCR fields as a prescription draft",
    description=(
        "The owning patient submits reviewed/corrected fields for a review-ready "
        "job. The resulting prescription remains patient-source and unconfirmed; "
        "explicit patient confirmation remains a separate action."
    ),
    dependencies=[Depends(require_authenticated_rate_limit)],
    responses=_OCR_ERROR_RESPONSES,
)
async def create_prescription_ocr_draft(
    patient_id: UUID,
    job_id: UUID,
    payload: PrescriptionOcrDraftInput,
    request: Request,
    response: Response,
    session: SessionDep,
    current_user: UserDep,
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
    ],
) -> PrescriptionOcrDraftCreated:
    _reject_unknown_query(request, set())
    result = await ocr_service.create_reviewed_draft(
        session=session,
        user=current_user,
        request=request,
        patient_id=patient_id,
        job_id=job_id,
        payload=payload,
        idempotency_key=idempotency_key,
    )
    response.headers["Cache-Control"] = "no-store"
    return result
