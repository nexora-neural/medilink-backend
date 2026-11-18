"""ML-104 patient lab upload request routes."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import get_session
from app.modules.identity.auth import AuthenticationError, get_current_user
from app.modules.identity.models import User
from app.modules.identity.request_limits import (
    require_authenticated_rate_limit,
    require_resolution_rate_limit,
)
from app.modules.laboratory import service
from app.modules.laboratory.schemas import (
    LabUploadRequestCreate,
    LabUploadRequestCreated,
    LabUploadRequestErrorResponse,
    LabUploadRequestListQuery,
    LabUploadRequestPage,
    LabUploadRequestRead,
    LabUploadRequestResolve,
    LabUploadRequestResolved,
    LabUploadRequestRevoke,
)


class LabUploadRequestRoute(APIRoute):
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
                status_code, code, message = 400, "BAD_REQUEST", "Malformed JSON."
            except service.LabUploadRequestError as exc:
                status_code, code, message = exc.status_code, exc.code, str(exc)
            except HTTPException as exc:
                if exc.status_code != 403:
                    raise
                status_code, code, message = 403, "FORBIDDEN", "Access denied."
            else:
                response.headers["Cache-Control"] = "no-store"
                return response

            error = LabUploadRequestErrorResponse(
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
    tags=["Laboratory"],
    route_class=LabUploadRequestRoute,
    responses={
        status_code: {"model": LabUploadRequestErrorResponse}
        for status_code in (400, 401, 403, 404, 409, 422, 429, 500)
    },
)

DB = Annotated[AsyncSession, Depends(get_session)]
Actor = Annotated[User, Depends(get_current_user)]


@router.post(
    "/lab-upload-requests",
    response_model=LabUploadRequestCreated,
    status_code=201,
    dependencies=[Depends(require_authenticated_rate_limit)],
)
async def create_lab_upload_request(
    body: LabUploadRequestCreate, request: Request, db: DB, actor: Actor
) -> LabUploadRequestCreated:
    return await service.create_request(
        db, actor, body.laboratory_id, request.state.request_id
    )


@router.get(
    "/me/lab-upload-requests",
    response_model=LabUploadRequestPage,
    dependencies=[Depends(require_authenticated_rate_limit)],
)
async def list_lab_upload_requests(
    db: DB,
    actor: Actor,
    query: Annotated[LabUploadRequestListQuery, Query()],
) -> LabUploadRequestPage:
    return await service.list_requests(
        db, actor, page=query.page, page_size=query.page_size
    )


@router.post(
    "/lab-upload-requests/{request_id}/revoke",
    response_model=LabUploadRequestRead,
    status_code=201,
    dependencies=[Depends(require_authenticated_rate_limit)],
)
async def revoke_lab_upload_request(
    request_id: UUID,
    body: LabUploadRequestRevoke,
    request: Request,
    db: DB,
    actor: Actor,
) -> LabUploadRequestRead:
    return await service.revoke_request(
        db, actor, request_id, body.reason, request.state.request_id
    )


@router.post(
    "/lab-upload-requests/resolve",
    response_model=LabUploadRequestResolved,
    dependencies=[Depends(require_resolution_rate_limit)],
)
async def resolve_lab_upload_request(
    body: LabUploadRequestResolve, request: Request, db: DB, actor: Actor
) -> LabUploadRequestResolved:
    return await service.resolve_request(
        db, actor, body.laboratory_id, body.request_token, request.state.request_id
    )