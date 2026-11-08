
"""API router for prescriptions, OCR workflow, and medicine catalogue."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import get_session
from app.modules.identity.auth import get_current_user
from app.modules.identity.models import User
from app.modules.prescriptions import service
from app.modules.prescriptions.schemas import (
    MedicineInput,
    MedicinesRead,
    MedicinesReadPage,
)


class ContractValidationRoute(APIRoute):
    """Return contract-shaped validation errors."""

    def get_route_handler(self):
        original_handler = super().get_route_handler()

        async def handler(request: Request):
            try:
                return await original_handler(request)

            except RequestValidationError as exc:
                field_errors: dict[str, list[str]] = {}

                for error in exc.errors():
                    location = error.get("loc", ())
                    field = ".".join(
                        str(part) for part in location
                    )

                    field_errors.setdefault(field, []).append(
                        error.get("msg", "Invalid value.")
                    )

                return JSONResponse(
                    status_code=422,
                    content={
                        "code": "VALIDATION_ERROR",
                        "message": "Please check the highlighted fields.",
                        "field_errors": field_errors,
                        "request_id": getattr(
                            request.state,
                            "request_id",
                            "",
                        ),
                    },
                )

        return handler


# Original Prescriptions router
router = APIRouter(tags=["Prescriptions"])


# Medicines router.
medicine_router = APIRouter(
    tags=["Medicines"],
    route_class=ContractValidationRoute,
)


SessionDep = Annotated[AsyncSession, Depends(get_session)]
UserDep = Annotated[User, Depends(get_current_user)]


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
    q: Annotated[
        str | None, Query(max_length=200)
    ] = None,
    page: Annotated[
        int, Query(ge=1)
    ] = 1,
    page_size: Annotated[
        int, Query(ge=1, le=100)
    ] = 20,
) -> MedicinesReadPage:
    unknown = set(request.query_params) - {"q", "page", "page_size"}
    if unknown:
        return JSONResponse(
            status_code=422,
            content={
                "code": "VALIDATION_ERROR",
                "message": "Please check the highlighted fields.",
                "field_errors": {
                    f"query.{key}": ["Unknown query parameter."] for key in sorted(unknown)
                },
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


@medicine_router.post(
    "/admin/medicines",
    response_model=MedicinesRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add curated sample catalogue entry",
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
