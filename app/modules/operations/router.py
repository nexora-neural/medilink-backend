"""Operations, delegated audit, backup evidence and own-patient activity."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import get_session
from app.modules.identity.auth import get_current_user
from app.modules.identity.models import User
from app.modules.identity.request_limits import require_authenticated_rate_limit
from app.modules.operations import service
from app.modules.operations.schemas import (
    AuditEventsReadPage,
    BackupPage,
    JobStatus,
    JobStatusPage,
    OperationalSummary,
    PatientActivityPage,
    ReasonInput,
)
from app.modules.profiles.route import ProfileRoute

router = APIRouter(
    tags=["Operations and activity"],
    route_class=ProfileRoute,
    dependencies=[Depends(require_authenticated_rate_limit)],
)
DB = Annotated[AsyncSession, Depends(get_session)]
Actor = Annotated[User, Depends(get_current_user)]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]


def queries(request, allowed):
    unknown = set(request.query_params) - set(allowed)
    if unknown:
        raise RequestValidationError(
            [
                {
                    "type": "extra_forbidden",
                    "loc": ("query", name),
                    "msg": "Unknown query field",
                    "input": None,
                }
                for name in sorted(unknown)
            ]
        )


@router.get("/admin/operations", response_model=OperationalSummary)
async def get_operations(request: Request, db: DB, actor: Actor):
    queries(request, set())
    return await service.summary(db, actor)


@router.get("/admin/jobs", response_model=JobStatusPage)
async def get_jobs(
    request: Request,
    db: DB,
    actor: Actor,
    page: Page = 1,
    page_size: PageSize = 20,
    status: Literal["pending", "running", "succeeded", "failed", "cancelled"] | None = None,
):
    queries(request, {"page", "page_size", "status"})
    return await service.jobs(db, actor, page, page_size, status)


@router.post("/admin/jobs/{job_id}/retry", response_model=JobStatus, status_code=202)
async def post_retry(
    job_id: UUID,
    payload: ReasonInput,
    request: Request,
    db: DB,
    actor: Actor,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
):
    queries(request, set())
    return await service.retry(
        db,
        actor,
        job_id,
        payload.reason,
        request.state.request_id,
        idempotency_key=idempotency_key,
    )


@router.get("/admin/audit-events", response_model=AuditEventsReadPage)
async def get_audit(
    request: Request,
    db: DB,
    actor: Actor,
    page: Page = 1,
    page_size: PageSize = 20,
    action: Annotated[str | None, Query(max_length=200)] = None,
    start: Annotated[datetime | None, Query(alias="from")] = None,
    end: Annotated[datetime | None, Query(alias="to")] = None,
    request_id: UUID | None = None,
):
    queries(request, {"page", "page_size", "action", "from", "to", "request_id"})
    return await service.audit(db, actor, page, page_size, action, start, end, request_id)


@router.get("/patients/{patient_id}/activity", response_model=PatientActivityPage)
async def get_activity(
    patient_id: UUID,
    request: Request,
    db: DB,
    actor: Actor,
    page: Page = 1,
    page_size: PageSize = 20,
):
    queries(request, {"page", "page_size"})
    return await service.activity(db, actor, patient_id, page, page_size)


@router.get("/admin/backups", response_model=BackupPage)
async def get_backups(
    request: Request, db: DB, actor: Actor, page: Page = 1, page_size: PageSize = 20
):
    queries(request, {"page", "page_size"})
    return await service.backups(db, actor, page, page_size)
