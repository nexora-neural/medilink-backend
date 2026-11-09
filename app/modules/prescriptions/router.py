
"""API router for prescriptions, OCR workflow, and medicine catalogue."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import get_session
from app.modules.identity.auth import get_current_user
from app.modules.identity.models import User
from app.modules.identity.request_limits import require_authenticated_rate_limit
from app.modules.prescriptions import service
from app.modules.prescriptions.schemas import (
    MedicineInput,
    MedicinesRead,
    MedicinesReadPage,
)

router = APIRouter(tags=["Prescriptions"])


# Medicines router.
medicine_router = APIRouter(
    tags=["Medicines"],
    dependencies=[Depends(require_authenticated_rate_limit)],
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
