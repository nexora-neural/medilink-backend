"""API router for medication schedules and adherence."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import get_session
from app.modules.identity.auth import get_current_user
from app.modules.identity.models import User
from app.modules.medication.schemas import ScheduleDetail, ScheduleInput
from app.modules.medication.service import (
    MedicationScheduleError,
    replace_schedule,
)
from app.modules.prescriptions.schemas import ClinicalWriteErrorResponse

router = APIRouter(tags=["Medication"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
UserDep = Annotated[User, Depends(get_current_user)]


@router.post(
    "/medication-schedules/{schedule_id}/replacement",
    response_model=ScheduleDetail,
    status_code=status.HTTP_201_CREATED,
    responses={
        403: {"model": ClinicalWriteErrorResponse},
        404: {"model": ClinicalWriteErrorResponse},
        409: {"model": ClinicalWriteErrorResponse},
        422: {"model": ClinicalWriteErrorResponse},
    },
)
async def replace_medication_schedule(
    schedule_id: UUID,
    payload: ScheduleInput,
    request: Request,
    session: SessionDep,
    current_user: UserDep,
    if_match: Annotated[str, Header(alias="If-Match")],
):
    """Replace reminder times without changing clinical instructions."""

    try:
        expected_version = int(if_match.strip().strip('"'))
        if expected_version < 1:
            raise ValueError
    except ValueError:
        error = MedicationScheduleError(
            422,
            "VALIDATION_ERROR",
            "If-Match must contain a positive schedule version.",
        )
        return JSONResponse(
            status_code=error.status,
            content={
                "code": error.code,
                "message": error.message,
                "field_errors": {},
                "request_id": getattr(request.state, "request_id", "unknown"),
            },
        )

    try:
        schedule, times = await replace_schedule(
            session,
            schedule_id=schedule_id,
            actor_user_id=current_user.id,
            expected_version=expected_version,
            payload=payload,
            request_id=getattr(request.state, "request_id", "unknown"),
        )
        return ScheduleDetail(schedule=schedule, times=times)

    except MedicationScheduleError as error:
        return JSONResponse(
            status_code=error.status,
            content={
                "code": error.code,
                "message": error.message,
                "field_errors": {},
                "request_id": getattr(request.state, "request_id", "unknown"),
            },
        )
