"""Authorization and audited actions for account support."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.service import append_event
from app.modules.identity.auth import AuthenticationError
from app.modules.identity.enums import AccountStatus
from app.modules.identity.models import AccountSupportDecisionReason, Role, User, UserRole
from app.modules.identity.repository import count_active_super_admins, find_accounts, get_roles
from app.modules.identity.schemas import AccountDecision, ActionResult, UsersRead, UsersReadPage

PRIVILEGED_ROLES = {"admin", "super_admin"}


class SupportDecisionError(Exception):
    """An expected account-support rejection safe to return to the caller."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@asynccontextmanager
async def audited_action(
    session: AsyncSession,
    request: Request,
    actor_id: UUID,
    action: str,
    target_id: UUID | None = None,
    *,
    reason: str | None = None,
) -> AsyncIterator[Any]:
    """Keep a decision and its audit atomic, and record safe rejection metadata."""
    event = SimpleNamespace(outcome="success", metadata_={})
    try:
        yield event
        persisted_event = await append_event(
            session,
            actor_user_id=actor_id,
            action=action,
            target_type="users",
            target_id=target_id,
            outcome=event.outcome,
            request_id=request.state.request_id,
            metadata=event.metadata_,
        )
        if reason is not None:
            session.add(
                AccountSupportDecisionReason(audit_event_id=persisted_event.id, reason=reason)
            )
        await session.commit()
    except SupportDecisionError as error:
        await session.rollback()
        code = {
            403: "ACCESS_DENIED",
            404: "RESOURCE_NOT_FOUND",
            409: "INVALID_STATE",
            503: "SERVICE_UNAVAILABLE",
        }[error.status]
        await append_event(
            session,
            actor_user_id=actor_id,
            action=action,
            target_type="users",
            target_id=target_id,
            outcome="denied" if error.status == 403 else "failed",
            request_id=request.state.request_id,
            metadata={"code": code},
        )
        await session.commit()
        raise AuthenticationError(
            status_code=error.status,
            detail={
                "code": code,
                "message": str(error),
                "field_errors": {},
                "request_id": request.state.request_id,
            },
        ) from None
    except Exception:
        await session.rollback()
        raise


async def authorize_account_support(
    session: AsyncSession,
    actor_id: UUID,
    *,
    write: bool = False,
    super_admin_only: bool = False,
) -> set[str]:
    # Serialize account-support decisions before rereading role/status and counting
    # Super Admins. Searches share the lock so revocation cannot race authorization.
    lock = func.pg_advisory_xact_lock if write else func.pg_advisory_xact_lock_shared
    await session.execute(select(lock(717, 35)))
    actor = await session.get(User, actor_id, populate_existing=True)
    if actor is None or actor.status != AccountStatus.ACTIVE:
        raise SupportDecisionError(403, "An active administrative account is required.")
    roles = await get_roles(session, actor_id)
    if not roles & PRIVILEGED_ROLES or (super_admin_only and "super_admin" not in roles):
        raise SupportDecisionError(403, "Permission denied.")
    return roles


def require_privileged_mfa(identity: dict[str, Any]) -> None:
    """Require MFA evidence in the verified Firebase sign-in claims."""
    firebase_claims = identity.get("firebase")
    factor = (
        firebase_claims.get("sign_in_second_factor") if isinstance(firebase_claims, dict) else None
    )
    if not isinstance(factor, str) or not factor.strip():
        raise SupportDecisionError(403, "Multi-factor authentication is required.")


async def get_target_account(session: AsyncSession, user_id: UUID) -> User:
    user = await session.get(User, user_id, with_for_update=True, populate_existing=True)
    if user is None:
        raise SupportDecisionError(404, "Account not found.")
    return user


async def search_accounts(
    session: AsyncSession,
    request: Request,
    actor_id: UUID,
    *,
    query: str | None,
    status: AccountStatus | None,
    page: int,
    page_size: int,
) -> UsersReadPage:
    async with audited_action(session, request, actor_id, "account.support.search") as event:
        await authorize_account_support(session, actor_id)
        users, total = await find_accounts(
            session, query=query, status=status, page=page, page_size=page_size
        )
        event.metadata_ = {"result_count": len(users), "has_query": bool(query)}
        return UsersReadPage(
            items=[UsersRead.model_validate(user) for user in users],
            page=page,
            page_size=page_size,
            total=total,
        )


async def decide_account_status(
    session: AsyncSession,
    request: Request,
    actor_id: UUID,
    identity: dict[str, Any],
    user_id: UUID,
    decision: AccountDecision,
) -> UsersRead:
    async with audited_action(
        session, request, actor_id, "account.status.changed", user_id, reason=decision.reason
    ) as event:
        roles = await authorize_account_support(session, actor_id, write=True)
        target = await get_target_account(session, user_id)
        target_roles = await get_roles(session, user_id)
        if target_roles & PRIVILEGED_ROLES:
            if "super_admin" not in roles:
                raise SupportDecisionError(
                    403, "Delegated Admins cannot modify privileged accounts."
                )
            require_privileged_mfa(identity)

        previous_status = AccountStatus(target.status)
        new_status = AccountStatus(decision.status)
        if previous_status == new_status:
            raise SupportDecisionError(409, "The account already has the requested status.")
        if (
            "super_admin" in target_roles
            and previous_status == AccountStatus.ACTIVE
            and new_status != AccountStatus.ACTIVE
            and await count_active_super_admins(session) <= 1
        ):
            raise SupportDecisionError(409, "The final active Super Admin must remain active.")

        target.status = new_status
        target.updated_at = datetime.now(UTC)
        event.metadata_ = {"previous_status": previous_status.value, "status": new_status.value}
        return UsersRead.model_validate(target)


async def change_admin_assignment(
    session: AsyncSession,
    request: Request,
    actor_id: UUID,
    identity: dict[str, Any],
    user_id: UUID,
    reason: str,
    *,
    revoke: bool,
) -> ActionResult:
    action = "account.admin.revoked" if revoke else "account.admin.appointed"
    async with audited_action(session, request, actor_id, action, user_id, reason=reason) as event:
        await authorize_account_support(session, actor_id, write=True, super_admin_only=True)
        require_privileged_mfa(identity)
        target = await get_target_account(session, user_id)
        role_id = await session.scalar(select(Role.id).where(Role.code == "admin"))
        if role_id is None:
            raise SupportDecisionError(503, "The Admin role is unavailable.")

        assignment = await session.get(UserRole, (user_id, role_id))
        assigned = assignment is not None and assignment.revoked_at is None
        if revoke != assigned:
            message = "The Admin role is already assigned." if assigned else "No active Admin role."
            raise SupportDecisionError(409, message)
        if not revoke and target.status != AccountStatus.ACTIVE:
            raise SupportDecisionError(409, "Only an active account can be appointed as Admin.")

        changed_at = datetime.now(UTC)
        if revoke:
            assignment.revoked_at = changed_at
            event.metadata_ = {
                "previous_assigned_by": (
                    str(assignment.assigned_by) if assignment.assigned_by is not None else None
                ),
                "previous_assigned_at": assignment.assigned_at.isoformat(),
            }
        elif assignment is None:
            session.add(
                UserRole(
                    user_id=user_id, role_id=role_id, assigned_by=actor_id, assigned_at=changed_at
                )
            )
        else:
            assignment.assigned_by = actor_id
            assignment.assigned_at = changed_at
            assignment.revoked_at = None

        status = "revoked" if revoke else "active"
        event.metadata_ = {**(event.metadata_ or {}), "role": "admin", "status": status}
        return ActionResult(id=user_id, status=status, updated_at=changed_at)
