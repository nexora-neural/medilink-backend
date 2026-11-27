"""API router for prescriptions, OCR workflow, and medicine catalogue."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import get_session
from app.modules.identity.auth import get_current_user
from app.modules.identity.models import User
from app.modules.identity.request_limits import require_authenticated_rate_limit
from app.modules.prescriptions import service
from app.modules.prescriptions.schemas import (
    ClinicalWriteErrorResponse,
    MedicineInput,
    MedicinesRead,
    MedicinesReadPage,
    PrescriptionDetail,
    PrescriptionDetailPage,
    PrescriptionInput,
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
