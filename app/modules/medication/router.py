"""ML-155 patient-confirmed schedule operations with safe private responses."""

import re
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import get_session
from app.modules.identity.auth import AuthenticationError, get_current_user
from app.modules.identity.models import User
from app.modules.identity.request_limits import require_authenticated_rate_limit
from app.modules.medication import service
from app.modules.medication.schemas import (
    DoseOccurrenceListQuery,
    DoseOccurrencesRead,
    DoseOccurrencesReadPage,
    ScheduleAction,
    ScheduleDetail,
    ScheduleDetailPage,
    ScheduleErrorResponse,
    ScheduleInput,
    ScheduleListQuery,
    SnoozeInput,
)


class ScheduleRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def handle(request: Request):
            try:
                response = await handler(request)
            except AuthenticationError:
                raise
            except service.MedicationScheduleError as exc:
                status, code, message = exc.status, exc.code, exc.message
            except HTTPException as exc:
                if exc.status_code != 403:
                    raise
                status, code, message = 403, "ACCESS_DENIED", "Access denied."
            except RequestValidationError as exc:
                if not any(error["type"] == "json_invalid" for error in exc.errors()):
                    raise
                status, code, message = 400, "BAD_REQUEST", "Malformed JSON."
            else:
                response.headers["Cache-Control"] = "no-store"
                return response
            return JSONResponse(
                status_code=status,
                headers={"Cache-Control": "no-store"},
                content=ScheduleErrorResponse(
                    code=code,
                    message=message,
                    field_errors={},
                    request_id=request.state.request_id,
                ).model_dump(),
            )

        return handle


router = APIRouter(
    tags=["Medication"],
    route_class=ScheduleRoute,
    dependencies=[Depends(require_authenticated_rate_limit)],
    responses={
        code: {"model": ScheduleErrorResponse}
        for code in (400, 401, 403, 404, 409, 422, 429, 500, 503)
    },
)
SessionDep = Annotated[AsyncSession, Depends(get_session)]
UserDep = Annotated[User, Depends(get_current_user)]


def expected_version(value: str) -> int:
    if not re.fullmatch(r'(?:[1-9][0-9]{0,9}|"[1-9][0-9]{0,9}")', value):
        raise service.MedicationScheduleError(
            422,
            "VALIDATION_ERROR",
            "If-Match must contain a positive schedule version.",
        )
    version = int(value.strip('"'))
    if version > 2147483647:
        raise service.MedicationScheduleError(422, "VALIDATION_ERROR", "Invalid schedule version.")
    return version


@router.post(
    "/patients/{patient_id}/medication-schedules",
    response_model=ScheduleDetail,
    status_code=201,
    operation_id="medilink_068",
)
async def confirm_medication_schedule(
    patient_id: UUID,
    payload: ScheduleInput,
    request: Request,
    session: SessionDep,
    current_user: UserDep,
):
    """Confirm daily reminder times without altering prescription instructions."""
    return await service.create_schedule(
        session,
        current_user,
        patient_id,
        payload,
        request.state.request_id,
    )


@router.get(
    "/patients/{patient_id}/medication-schedules",
    response_model=ScheduleDetailPage,
    operation_id="medilink_069",
)
async def list_medication_schedules(
    patient_id: UUID,
    query: Annotated[ScheduleListQuery, Query()],
    request: Request,
    session: SessionDep,
    current_user: UserDep,
):
    """Read own schedules or use current Doctor medication_logs consent."""
    return await service.list_schedules(
        session,
        current_user,
        patient_id,
        query,
        request.state.request_id,
    )


@router.post(
    "/medication-schedules/{schedule_id}/status",
    response_model=ScheduleDetail,
    status_code=201,
    operation_id="medilink_070",
)
async def change_medication_schedule_status(
    schedule_id: UUID,
    payload: ScheduleAction,
    request: Request,
    session: SessionDep,
    current_user: UserDep,
    if_match: Annotated[str, Header(alias="If-Match")],
):
    """Pause/resume/end reminder state, retaining reasons and clinical history."""
    return await service.change_status(
        session,
        current_user,
        schedule_id,
        expected_version(if_match),
        payload,
        request.state.request_id,
    )


@router.post(
    "/medication-schedules/{schedule_id}/replacement",
    response_model=ScheduleDetail,
    status_code=201,
    operation_id="medilink_071",
)
async def replace_medication_schedule(
    schedule_id: UUID,
    payload: ScheduleInput,
    request: Request,
    session: SessionDep,
    current_user: UserDep,
    if_match: Annotated[str, Header(alias="If-Match")],
):
    """Replace a current reminder schedule while retaining its predecessor."""
    return await service.replace_schedule(
        session,
        schedule_id=schedule_id,
        actor_user_id=current_user.id,
        expected_version=expected_version(if_match),
        payload=payload,
        request_id=request.state.request_id,
    )


@router.get(
    "/patients/{patient_id}/dose-occurrences",
    response_model=DoseOccurrencesReadPage,
    operation_id="medilink_072",
)
async def list_dose_occurrences(
    patient_id: UUID,
    query: Annotated[DoseOccurrenceListQuery, Query()],
    request: Request,
    session: SessionDep,
    current_user: UserDep,
):
    """List retained dose occurrences using patient or current doctor consent."""
    return await service.list_occurrences(
        session,
        current_user,
        patient_id,
        query,
        request.state.request_id,
    )


@router.post(
    "/dose-occurrences/{occurrence_id}/snooze",
    response_model=DoseOccurrencesRead,
    status_code=201,
    operation_id="medilink_073",
)
async def snooze_dose_occurrence(
    occurrence_id: UUID,
    payload: SnoozeInput,
    request: Request,
    session: SessionDep,
    current_user: UserDep,
):
    """Postpone reminder delivery on the same unanswered dose occurrence."""
    return await service.snooze_occurrence(
        session,
        current_user,
        occurrence_id,
        payload,
        request.state.request_id,
    )
