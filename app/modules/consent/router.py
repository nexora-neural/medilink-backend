"""API routes for patient-owned doctor access grants."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import get_session
from app.modules.consent import service
from app.modules.consent.schemas import (
    GrantDetail,
    GrantDetailPage,
    GrantErrorResponse,
    GrantInput,
    GrantListQuery,
    GrantRevokeInput,
)
from app.modules.identity.auth import AuthenticationError, get_current_user
from app.modules.identity.models import User
from app.modules.identity.request_limits import require_authenticated_rate_limit

ERROR_CODES = {403: "FORBIDDEN", 404: "NOT_FOUND", 409: "INVALID_STATE"}


class GrantRoute(APIRoute):
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
            except HTTPException as exc:
                if exc.status_code not in ERROR_CODES:
                    raise
                status_code = exc.status_code
                code, message = ERROR_CODES[status_code], exc.detail
            else:
                response.headers["Cache-Control"] = "no-store"
                return response

            error = GrantErrorResponse(
                code=code, message=message, field_errors={}, request_id=request.state.request_id
            )
            return JSONResponse(
                status_code=status_code,
                content=error.model_dump(),
                headers={"Cache-Control": "no-store"},
            )

        return handle


router = APIRouter(
    tags=["Consent"],
    route_class=GrantRoute,
    dependencies=[Depends(require_authenticated_rate_limit)],
    responses={
        code: {"model": GrantErrorResponse}
        for code in (400, 401, 403, 404, 409, 422, 429, 500)
    },
)
DB = Annotated[AsyncSession, Depends(get_session)]
Actor = Annotated[User, Depends(get_current_user)]


@router.post(
    "/access-grants", response_model=GrantDetail, status_code=201, operation_id="medilink_020"
)
async def create_access_grant(
    body: GrantInput, request: Request, db: DB, actor: Actor
) -> GrantDetail:
    return await service.create_grant(db, actor, body, request.state.request_id)


@router.get("/me/access-grants", response_model=GrantDetailPage, operation_id="medilink_021")
async def list_my_access_grants(
    query: Annotated[GrantListQuery, Query()], db: DB, actor: Actor
) -> GrantDetailPage:
    return await service.list_grants(db, actor, query)


@router.post(
    "/access-grants/{grant_id}/revoke",
    response_model=GrantDetail,
    status_code=201,
    operation_id="medilink_022",
)
async def revoke_access_grant(
    grant_id: UUID, body: GrantRevokeInput, request: Request, db: DB, actor: Actor
) -> GrantDetail:
    return await service.revoke_grant(db, actor, grant_id, body.reason, request.state.request_id)
