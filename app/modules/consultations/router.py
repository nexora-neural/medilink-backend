"""API routes for consultations and the doctor patient queue."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import get_session
from app.modules.consultations import service
from app.modules.consultations.schemas import (
    ConsultationEnd,
    ConsultationErrorResponse,
    ConsultationInput,
    ConsultationListQuery,
    ConsultationsRead,
    ConsultationsReadPage,
    QueueEntryPage,
)
from app.modules.identity.auth import AuthenticationError, get_current_user
from app.modules.identity.models import User
from app.modules.identity.request_limits import require_authenticated_rate_limit

ERROR_CODES = {
    403: "ACCESS_DENIED",
    404: "RESOURCE_NOT_FOUND",
    409: "INVALID_STATE",
}


class ConsultationRoute(APIRoute):
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

                status_code = 400
                code = "BAD_REQUEST"
                message = "Malformed JSON."
            except HTTPException as exc:
                if exc.status_code not in ERROR_CODES:
                    raise

                status_code = exc.status_code
                code = ERROR_CODES[status_code]
                message = exc.detail
            else:
                response.headers["Cache-Control"] = "no-store"
                return response

            error = ConsultationErrorResponse(
                code=code,
                message=message,
                field_errors={},
                request_id=request.state.request_id,
            )

            return JSONResponse(
                status_code=status_code,
                content=error.model_dump(),
                headers={"Cache-Control": "no-store"},
            )

        return handle


router = APIRouter(
    tags=["Consultations"],
    route_class=ConsultationRoute,
    dependencies=[Depends(require_authenticated_rate_limit)],
    responses={
        code: {"model": ConsultationErrorResponse}
        for code in (400, 401, 403, 404, 409, 422, 429, 500)
    },
)

DB = Annotated[AsyncSession, Depends(get_session)]
Actor = Annotated[User, Depends(get_current_user)]


@router.get("/me/patient-queue", response_model=QueueEntryPage, operation_id="medilink_023")
async def get_patient_queue(
    query: Annotated[ConsultationListQuery, Query()],
    db: DB,
    actor: Actor,
) -> QueueEntryPage:
    return await service.list_patient_queue(db, actor, query)


@router.post(
    "/consultations", response_model=ConsultationsRead, status_code=201, operation_id="medilink_024"
)
async def post_consultation(
    body: ConsultationInput,
    request: Request,
    db: DB,
    actor: Actor,
) -> ConsultationsRead:
    return await service.start_consultation(
        db,
        actor,
        body,
        request.state.request_id,
    )


@router.get(
    "/patients/{patient_id}/consultations",
    response_model=ConsultationsReadPage,
    operation_id="medilink_025",
)
async def get_patient_consultations(
    patient_id: UUID,
    query: Annotated[ConsultationListQuery, Query()],
    db: DB,
    actor: Actor,
) -> ConsultationsReadPage:
    return await service.list_consultations(
        db,
        actor,
        patient_id,
        query,
    )


@router.post(
    "/consultations/{consultation_id}/end",
    response_model=ConsultationsRead,
    status_code=201,
    operation_id="medilink_026",
)
async def post_consultation_end(
    consultation_id: UUID,
    body: ConsultationEnd,
    request: Request,
    db: DB,
    actor: Actor,
) -> ConsultationsRead:
    return await service.end_consultation(
        db,
        actor,
        consultation_id,
        body,
        request.state.request_id,
    )
