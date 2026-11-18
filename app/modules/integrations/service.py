"""Business rules and orchestration for integration administration."""

import hashlib
import secrets
from datetime import UTC, datetime
from uuid import UUID

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.service import append_event
from app.modules.identity.auth import AuthenticationError
from app.modules.identity.models import AccountSupportDecisionReason, Role, User, UserRole
from app.modules.integrations import repository
from app.modules.integrations.models import IntegrationClient, SubmissionStatus
from app.modules.integrations.schemas import (
    CredentialCreated,
    IntegrationAdminSubmissionRead,
    IntegrationClientInput,
    IntegrationClientRead,
    IntegrationClientsReadPage,
    IntegrationSubmissionsReadPage,
    ReasonInput,
    RotateCredential,
)


def _error(request: Request, status: int, code: str, message: str) -> AuthenticationError:
    return AuthenticationError(
        status_code=status,
        detail={
            "code": code,
            "message": message,
            "field_errors": {},
            "request_id": getattr(request.state, "request_id", ""),
        },
    )


async def _require_super_admin(db: AsyncSession, actor: User, request: Request) -> None:
    has_role = await db.scalar(
        select(Role.id)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(
            UserRole.user_id == actor.id,
            UserRole.revoked_at.is_(None),
            Role.code == "super_admin",
        )
    )
    if actor.status != "active" or has_role is None:
        raise _error(request, 403, "ACCESS_DENIED", "Permission denied.")


def _expires_at(value: datetime, request: Request) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise _error(request, 422, "VALIDATION_ERROR", "Expiry must include a timezone.")
    value = value.astimezone(UTC)
    now = datetime.now(UTC)
    if value <= now:
        raise _error(request, 422, "VALIDATION_ERROR", "Expiry must be in the future.")
    if (value - now).days > 366:
        raise _error(request, 422, "VALIDATION_ERROR", "Expiry cannot exceed one year.")
    return value


def _new_credential() -> tuple[str, str]:
    credential = secrets.token_urlsafe(48)
    return credential, hashlib.sha256(credential.encode("utf-8")).hexdigest()


async def _audit(
    db: AsyncSession,
    actor: User,
    request: Request,
    *,
    action: str,
    client_id: UUID,
    metadata: dict,
) -> UUID:
    event = await append_event(
        db,
        actor_user_id=actor.id,
        action=action,
        target_type="integration_client",
        target_id=client_id,
        outcome="success",
        request_id=request.state.request_id,
        metadata=metadata,
    )
    return event.id


async def _attach_reason(
    db: AsyncSession, event_id: UUID, reason: str
) -> None:
    db.add(AccountSupportDecisionReason(audit_event_id=event_id, reason=reason))


async def create_client(
    db: AsyncSession, actor: User, request: Request, payload: IntegrationClientInput
) -> CredentialCreated:
    await _require_super_admin(db, actor, request)
    expires_at = _expires_at(payload.expires_at, request)
    try:
        if not await repository.organization_is_approved_lab(db, payload.organization_id):
            raise _error(
                request, 422, "VALIDATION_ERROR", "An approved active laboratory is required."
            )
        credential, credential_hash = _new_credential()
        client = IntegrationClient(
            organization_id=payload.organization_id,
            name=payload.name,
            credential_hash=credential_hash,
            scopes=payload.scopes,
            status="active",
            expires_at=expires_at,
            created_by=actor.id,
        )
        db.add(client)
        await db.flush()
        await _audit(
            db,
            actor,
            request,
            action="integration_client.created",
            client_id=client.id,
            metadata={"organization_id": str(client.organization_id), "status": "active"},
        )
        result = CredentialCreated(
            client=IntegrationClientRead.model_validate(client), credential=credential
        )
        await db.commit()
        return result
    except AuthenticationError:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise


async def list_clients(
    db: AsyncSession,
    actor: User,
    request: Request,
    *,
    page: int,
    page_size: int,
) -> IntegrationClientsReadPage:
    await _require_super_admin(db, actor, request)
    rows, total = await repository.list_clients(db, page=page, page_size=page_size)
    return IntegrationClientsReadPage(
        items=[IntegrationClientRead.model_validate(row) for row in rows],
        page=page,
        page_size=page_size,
        total=total,
    )


async def rotate_client_credential(
    db: AsyncSession,
    actor: User,
    request: Request,
    client_id: UUID,
    payload: RotateCredential,
) -> CredentialCreated:
    await _require_super_admin(db, actor, request)
    expires_at = _expires_at(payload.expires_at, request)
    client = await repository.get_client(db, client_id, lock=True)
    if client is None:
        raise _error(request, 404, "RESOURCE_NOT_FOUND", "Integration client not found.")
    if client.status in {"suspended", "revoked", "expired"}:
        raise _error(request, 409, "INVALID_STATE", "This client cannot rotate credentials.")
    try:
        credential, credential_hash = _new_credential()
        previous_status = client.status
        client.credential_hash = credential_hash
        client.expires_at = expires_at
        await db.flush()
        event_id = await _audit(
            db,
            actor,
            request,
            action="integration_client.credential_rotated",
            client_id=client.id,
            metadata={"previous_status": previous_status, "status": previous_status},
        )
        await _attach_reason(db, event_id, payload.reason)
        result = CredentialCreated(
            client=IntegrationClientRead.model_validate(client), credential=credential
        )
        await db.commit()
        return result
    except AuthenticationError:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise


async def _change_client_status(
    db: AsyncSession,
    actor: User,
    request: Request,
    client_id: UUID,
    payload: ReasonInput,
    *,
    target_status: str,
) -> IntegrationClientRead:
    await _require_super_admin(db, actor, request)
    client = await repository.get_client(db, client_id, lock=True)
    if client is None:
        raise _error(request, 404, "RESOURCE_NOT_FOUND", "Integration client not found.")
    if client.status in {"revoked", "expired"}:
        raise _error(request, 409, "INVALID_STATE", "This client cannot change status.")
    try:
        previous_status = client.status
        client.status = target_status
        if target_status == "revoked":
            client.revoked_at = datetime.now(UTC)
        await db.flush()
        event_id = await _audit(
            db,
            actor,
            request,
            action=f"integration_client.{target_status}",
            client_id=client.id,
            metadata={"previous_status": previous_status, "status": target_status},
        )
        await _attach_reason(db, event_id, payload.reason)
        result = IntegrationClientRead.model_validate(client)
        await db.commit()
        return result
    except AuthenticationError:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise


async def revoke_client(
    db: AsyncSession, actor: User, request: Request, client_id: UUID, payload: ReasonInput
) -> IntegrationClientRead:
    return await _change_client_status(
        db, actor, request, client_id, payload, target_status="revoked"
    )


async def suspend_client(
    db: AsyncSession, actor: User, request: Request, client_id: UUID, payload: ReasonInput
) -> IntegrationClientRead:
    return await _change_client_status(
        db, actor, request, client_id, payload, target_status="suspended"
    )


async def list_submissions(
    db: AsyncSession,
    actor: User,
    request: Request,
    *,
    page: int,
    page_size: int,
    status_filter: str | None,
) -> IntegrationSubmissionsReadPage:
    await _require_super_admin(db, actor, request)
    allowed = {submission_status.value for submission_status in SubmissionStatus}
    if status_filter is not None and status_filter not in allowed:
        raise _error(request, 422, "VALIDATION_ERROR", "Submission status is invalid.")
    rows, total = await repository.list_submissions(
        db, page=page, page_size=page_size, status=status_filter
    )
    return IntegrationSubmissionsReadPage(
        items=[IntegrationAdminSubmissionRead.model_validate(row) for row in rows],
        page=page,
        page_size=page_size,
        total=total,
    )
