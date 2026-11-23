"""ML-68 clinical list, creation and detail operations."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import get_session
from app.modules.clinical_records import service
from app.modules.clinical_records.schemas import (
    ClinicalErrorResponse,
    ClinicalInput,
    ClinicalListQuery,
    ClinicalRecordRead,
    ClinicalRecordReadPage,
)
from app.modules.identity.auth import AuthenticationError, get_current_user
from app.modules.identity.models import User
from app.modules.identity.request_limits import require_authenticated_rate_limit


class ClinicalRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def handle(request: Request):
            try:
                response = await handler(request)
            except AuthenticationError:
                raise
            except RequestValidationError as exc:
                if not any(error["type"] == "json_invalid" for error in exc.errors()):
                    raise
                status, code, message = 400, "BAD_REQUEST", "Malformed JSON."
            except HTTPException as exc:
                codes = {403: "ACCESS_DENIED", 404: "RESOURCE_NOT_FOUND", 422: "VALIDATION_ERROR"}
                if exc.status_code not in codes:
                    raise
                status, code, message = exc.status_code, codes[exc.status_code], exc.detail
            else:
                response.headers["Cache-Control"] = "no-store"
                return response
            return JSONResponse(
                status_code=status,
                headers={"Cache-Control": "no-store"},
                content=ClinicalErrorResponse(
                    code=code,
                    message=message,
                    request_id=request.state.request_id,
                ).model_dump(),
            )

        return handle


router = APIRouter(
    tags=["Clinical records"],
    route_class=ClinicalRoute,
    dependencies=[Depends(require_authenticated_rate_limit)],
    responses={
        code: {"model": ClinicalErrorResponse} for code in (400, 401, 403, 404, 422, 429, 500, 503)
    },
)
DB = Annotated[AsyncSession, Depends(get_session)]
Actor = Annotated[User, Depends(get_current_user)]


@router.get(
    "/patients/{patient_id}/clinical-records",
    response_model=ClinicalRecordReadPage,
    operation_id="medilink_027",
)
async def list_clinical_records(
    patient_id: UUID,
    query: Annotated[ClinicalListQuery, Query()],
    request: Request,
    db: DB,
    actor: Actor,
):
    """List permitted typed records, retaining original source and status."""
    return await service.list_patient_records(
        db,
        actor,
        patient_id,
        query,
        request.state.request_id,
    )


@router.post(
    "/patients/{patient_id}/clinical-records",
    status_code=201,
    response_model=ClinicalRecordRead,
    operation_id="medilink_028",
)
async def post_clinical_record(
    patient_id: UUID,
    body: ClinicalInput,
    request: Request,
    db: DB,
    actor: Actor,
):
    """Append a patient entry or an authorized doctor's consultation entry."""
    return await service.create_record(db, actor, patient_id, body, request.state.request_id)


@router.get(
    "/clinical-records/{record_id}", response_model=ClinicalRecordRead, operation_id="medilink_037"
)
async def get_clinical_record(record_id: UUID, request: Request, db: DB, actor: Actor):
    """Read one record only after current category authorization."""
    return await service.get_record(db, actor, record_id, request.state.request_id)
