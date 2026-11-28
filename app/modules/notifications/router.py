"""Private in-app notification routes."""

from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import get_session
from app.modules.identity.auth import AuthenticationError, get_current_user
from app.modules.identity.models import User
from app.modules.identity.request_limits import require_authenticated_rate_limit
from app.modules.notifications import service
from app.modules.notifications.schemas import (
    NotificationsErrorResponse,
    NotificationsRead,
    NotificationsReadPage,
)


class NotificationRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def handle(request: Request):
            request_id = getattr(request.state, "request_id", None) or str(uuid4())
            request.state.request_id = request_id
            try:
                response = await handler(request)
            except AuthenticationError:
                raise
            except service.NotificationError as exc:
                status, code, message = exc.status, exc.code, exc.message
            except RequestValidationError as exc:
                if any(error["type"] == "json_invalid" for error in exc.errors()):
                    status, code, message = 400, "BAD_REQUEST", "Malformed JSON."
                else:
                    validation_handler = request.app.exception_handlers[RequestValidationError]
                    response = await validation_handler(request, exc)
                    response.headers["Cache-Control"] = "no-store"
                    return response
            else:
                response.headers["Cache-Control"] = "no-store"
                return response

            error = NotificationsErrorResponse(
                code=code,
                message=message,
                request_id=request_id,
            )
            return JSONResponse(
                status_code=status,
                content=error.model_dump(),
                headers={"Cache-Control": "no-store"},
            )

        return handle


router = APIRouter(
    tags=["Notifications"],
    route_class=NotificationRoute,
    dependencies=[Depends(require_authenticated_rate_limit)],
    responses={
        code: {"model": NotificationsErrorResponse}
        for code in (400, 401, 403, 404, 409, 422, 429, 500)
    },
)
DB = Annotated[AsyncSession, Depends(get_session)]
Actor = Annotated[User, Depends(get_current_user)]


@router.get(
    "/me/notifications",
    response_model=NotificationsReadPage,
    operation_id="medilink_104",
)
async def get_notifications(
    request: Request,
    db: DB,
    actor: Actor,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    unread: bool | None = None,
) -> NotificationsReadPage:
    if set(request.query_params) - {"page", "page_size", "unread"}:
        raise service.NotificationError(
            422,
            "VALIDATION_ERROR",
            "Unsupported notification filter.",
        )
    return await service.list_own_notifications(
        db,
        actor,
        unread=unread,
        page=page,
        page_size=page_size,
    )


@router.post(
    "/notifications/{notification_id}/read",
    response_model=NotificationsRead,
    operation_id="medilink_105",
)
async def post_notification_read(
    notification_id: UUID,
    request: Request,
    db: DB,
    actor: Actor,
) -> NotificationsRead:
    return await service.mark_notification_read(
        db,
        actor,
        notification_id,
        request.state.request_id,
    )
