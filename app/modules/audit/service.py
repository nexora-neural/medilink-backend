"""Business rules and orchestration for append-only audit events."""

import logging
from typing import Any
from uuid import UUID

from fastapi import Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import async_session
from app.modules.audit import repository
from app.modules.audit.models import AuditEvent, BackupRun, JobRun
from app.modules.audit.schemas import AuditEventInput

logger = logging.getLogger(__name__)


async def append_event(
    session: AsyncSession,
    *,
    action: str,
    target_type: str,
    outcome: str,
    request_id: str,
    actor_user_id: UUID | None = None,
    actor_client_id: UUID | None = None,
    target_id: UUID | None = None,
    metadata: dict[str, Any] | None = None,
) -> AuditEvent:
    """Append a validated event using the caller's transaction."""
    data = AuditEventInput(
        actor_user_id=actor_user_id,
        actor_client_id=actor_client_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        outcome=outcome,
        request_id=request_id,
        metadata=metadata or {},
    )
    return await repository.append_event(session, data)


async def record_api_request(request: Request, response: Response) -> None:
    """Persist a low-detail audit event for an API request outcome.

    Only the route template, method, status and established actor IDs are stored.
    Query values, request/response bodies, headers and credentials are never read.
    """
    route = request.scope.get("route")
    route_template = getattr(route, "path", None) or "unmatched"
    status_code = response.status_code
    outcome = (
        "success" if status_code < 400 else "denied" if status_code in {401, 403} else "failed"
    )
    event = {
        "action": "api.request",
        "target_type": "api_route",
        "outcome": outcome,
        "request_id": request.state.request_id,
        "actor_user_id": getattr(request.state, "audit_actor_user_id", None),
        "actor_client_id": getattr(request.state, "audit_actor_client_id", None),
        "metadata": {
            "method": request.method,
            "route": route_template,
            "status_code": status_code,
        },
    }
    session_factory = getattr(request.app.state, "audit_session_factory", None)
    if session_factory is None:
        # FastAPI dependency overrides identify isolated tests. Do not attempt a
        # connection to the developer's configured database from those tests.
        if request.app.dependency_overrides:
            return
        session_factory = async_session
    try:
        async with session_factory() as session:
            await append_event(session, **event)
            await session.commit()
    except Exception:
        # Do not include request payloads, headers, SQL parameters or exception
        # details in logs; the request ID is sufficient for operational triage.
        logger.error("Could not persist API audit event (request_id=%s)", request.state.request_id)


async def record_job_attempt(
    session: AsyncSession,
    run: JobRun,
    *,
    request_id: str,
    outcome: str,
    actor_user_id: UUID | None = None,
    actor_client_id: UUID | None = None,
) -> AuditEvent:
    """Append a safe audit entry for a worker attempt in the run transaction."""
    return await append_event(
        session,
        actor_user_id=actor_user_id,
        actor_client_id=actor_client_id,
        action="job.attempt",
        target_type="job_run",
        target_id=run.id,
        outcome=outcome,
        request_id=request_id,
        metadata={"job_type": run.job_type, "attempt_count": run.attempt_count},
    )


async def record_backup_run(
    session: AsyncSession,
    run: BackupRun,
    *,
    request_id: str,
    outcome: str,
    actor_user_id: UUID | None = None,
    actor_client_id: UUID | None = None,
) -> AuditEvent:
    """Append a safe audit entry for a backup attempt in the run transaction."""
    return await append_event(
        session,
        actor_user_id=actor_user_id,
        actor_client_id=actor_client_id,
        action="backup.run",
        target_type="backup_run",
        target_id=run.id,
        outcome=outcome,
        request_id=request_id,
        metadata={
            "status": run.status,
            "database_included": run.database_included,
            "files_included": run.files_included,
            "restore_check_status": run.restore_check_status,
        },
    )
