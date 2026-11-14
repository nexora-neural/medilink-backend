"""Organization and membership workflows with explicit authorization."""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.service import append_event
from app.modules.identity.auth import AuthenticationError
from app.modules.identity.models import AccountSupportDecisionReason, Role, User, UserRole
from app.modules.organizations import repository
from app.modules.organizations.models import Organization, OrganizationMembership
from app.modules.organizations.schemas import (
    MembershipDecision,
    MembershipInput,
    MembershipRead,
    MembershipReadPage,
    OrganizationDecision,
    OrganizationInput,
    OrganizationPublic,
    OrganizationPublicPage,
    OrganizationRead,
    OrganizationReadPage,
)
from app.modules.profiles.models import ProfessionalProfile


def _error(request: Request, status: int, code: str, message: str):
    return AuthenticationError(
        status_code=status,
        detail={
            "code": code,
            "message": message,
            "field_errors": {},
            "request_id": getattr(request.state, "request_id", ""),
        },
    )


async def _has_role(session: AsyncSession, user_id: UUID, role: str) -> bool:
    return (
        await session.scalar(
            select(Role.id)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == user_id, Role.code == role)
        )
        is not None
    )


async def _require_super_admin(session: AsyncSession, user: User, request: Request) -> None:
    if user.status != "active" or not await _has_role(session, user.id, "super_admin"):
        raise _error(request, 403, "ACCESS_DENIED", "Permission denied.")


async def list_public_organizations(
    session: AsyncSession,
    user: User,
    request: Request,
    organization_type: str | None,
    q: str | None,
    page: int,
    page_size: int,
):
    if user.status != "active":
        raise _error(request, 403, "ACCOUNT_INACTIVE", "An active account is required.")
    rows, total = await repository.list_organizations(
        session,
        public=True,
        status=None,
        organization_type=organization_type,
        q=q,
        page=page,
        page_size=page_size,
    )
    return OrganizationPublicPage(
        items=[OrganizationPublic.model_validate(x) for x in rows],
        page=page,
        page_size=page_size,
        total=total,
    )


async def submit_organization(
    session: AsyncSession, user: User, request: Request, payload: OrganizationInput
):
    if user.status != "active":
        raise _error(request, 403, "ACCOUNT_INACTIVE", "An active account is required.")
    roles = (
        await session.scalars(
            select(Role.code)
            .join(UserRole, Role.id == UserRole.role_id)
            .where(UserRole.user_id == user.id)
        )
    ).all()
    profile = await session.get(ProfessionalProfile, user.id)
    if (
        profile is None
        or profile.role not in {"doctor", "pharmacist", "lab_staff"}
        or profile.role not in roles
    ):
        raise _error(request, 403, "ACCESS_DENIED", "A professional profile is required.")
    row = Organization(
        name=payload.name.strip(),
        type=payload.type,
        registration_ref=payload.registration_ref.strip() if payload.registration_ref else None,
        status="pending",
    )
    try:
        session.add(row)
        await session.flush()
        await append_event(
            session,
            actor_user_id=user.id,
            action="organization.submitted",
            target_type="organization",
            target_id=row.id,
            outcome="success",
            request_id=request.state.request_id,
            metadata={"type": row.type},
        )
        result = OrganizationRead.model_validate(row)
        await session.commit()
        return result
    except Exception:
        await session.rollback()
        raise


async def request_membership(
    session: AsyncSession, user: User, request: Request, payload: MembershipInput
):
    if user.status != "active":
        raise _error(request, 403, "ACCOUNT_INACTIVE", "An active account is required.")
    profile = await session.get(ProfessionalProfile, user.id)
    if (
        profile is None
        or profile.role != payload.role
        or not await _has_role(session, user.id, payload.role)
    ):
        raise _error(
            request,
            403,
            "ACCESS_DENIED",
            "The requested professional role is not assigned to this account.",
        )
    org = await repository.get_organization(session, payload.organization_id)
    if org is None or org.status != "active":
        raise _error(request, 404, "RESOURCE_NOT_FOUND", "Organization not found.")
    try:
        existing = await repository.get_existing_membership(
            session, org.id, user.id, payload.role, lock=True
        )
        if existing and existing.status in {"pending", "approved", "suspended"}:
            raise _error(
                request,
                409,
                "INVALID_STATE",
                "A membership already exists for this organization and role.",
            )
        if existing:
            existing.status = "pending"
            existing.verified_at = None
            row = existing
        else:
            row = OrganizationMembership(
                organization_id=org.id, user_id=user.id, role=payload.role, status="pending"
            )
            session.add(row)
        await session.flush()
        await append_event(
            session,
            actor_user_id=user.id,
            action="membership.requested",
            target_type="organization_membership",
            target_id=row.id,
            outcome="success",
            request_id=request.state.request_id,
            metadata={"organization_id": str(org.id), "role": payload.role},
        )
        result = MembershipRead.model_validate(row)
        await session.commit()
        return result
    except IntegrityError as exc:
        await session.rollback()
        if _is_membership_duplicate(exc):
            raise _error(
                request,
                409,
                "INVALID_STATE",
                "A membership already exists for this organization and role.",
            ) from exc
        raise
    except Exception:
        await session.rollback()
        raise


def _is_membership_duplicate(error: IntegrityError) -> bool:
    original = error.orig
    while original is not None:
        constraint_name = getattr(original, "constraint_name", None)
        diagnostic = getattr(original, "diag", None)
        if constraint_name is None:
            constraint_name = getattr(diagnostic, "constraint_name", None)
        if constraint_name == "uq_membership_org_user_role":
            return True
        original = getattr(original, "__cause__", None) or getattr(original, "__context__", None)
    return False


async def admin_list_organizations(
    session: AsyncSession,
    user: User,
    request: Request,
    status: str | None,
    organization_type: str | None,
    page: int,
    page_size: int,
):
    await _require_super_admin(session, user, request)
    rows, total = await repository.list_organizations(
        session,
        public=False,
        status=status,
        organization_type=organization_type,
        q=None,
        page=page,
        page_size=page_size,
    )
    return OrganizationReadPage(
        items=[OrganizationRead.model_validate(x) for x in rows],
        page=page,
        page_size=page_size,
        total=total,
    )


async def admin_get_organization(
    session: AsyncSession, user: User, request: Request, organization_id: UUID
):
    await _require_super_admin(session, user, request)
    row = await repository.get_organization(session, organization_id)
    if row is None:
        raise _error(request, 404, "RESOURCE_NOT_FOUND", "Organization not found.")
    return OrganizationRead.model_validate(row)


async def admin_list_memberships(
    session: AsyncSession,
    user: User,
    request: Request,
    organization_id: UUID | None,
    status: str | None,
    page: int,
    page_size: int,
):
    await _require_super_admin(session, user, request)
    rows, total = await repository.list_memberships(
        session, organization_id=organization_id, status=status, page=page, page_size=page_size
    )
    return MembershipReadPage(
        items=[MembershipRead.model_validate(x) for x in rows],
        page=page,
        page_size=page_size,
        total=total,
    )


async def decide_organization(
    session: AsyncSession,
    user: User,
    request: Request,
    organization_id: UUID,
    payload: OrganizationDecision,
):
    await _require_super_admin(session, user, request)
    row = await repository.get_organization(session, organization_id, lock=True)
    if row is None:
        raise _error(request, 404, "RESOURCE_NOT_FOUND", "Organization not found.")
    if row.status not in {"pending", "active", "suspended"}:
        raise _error(
            request, 409, "INVALID_STATE", "Organization cannot transition from its current status."
        )
    if row.status == "pending" and payload.status in {"suspended", "inactive"}:
        raise _error(
            request,
            422,
            "VALIDATION_ERROR",
            "Pending organizations must be approved or rejected first.",
        )
    try:
        row.status = payload.status
        await session.flush()
        await session.refresh(row)
        event = await append_event(
            session,
            actor_user_id=user.id,
            action="organization.decision",
            target_type="organization",
            target_id=row.id,
            outcome="success",
            request_id=request.state.request_id,
        )
        session.add(AccountSupportDecisionReason(
            audit_event_id=event.id, reason=payload.reason
        ))
        result = OrganizationRead.model_validate(row)
        await session.commit()
        return result
    except Exception:
        await session.rollback()
        raise


async def decide_membership(
    session: AsyncSession,
    user: User,
    request: Request,
    membership_id: UUID,
    payload: MembershipDecision,
):
    await _require_super_admin(session, user, request)
    row = await repository.get_membership(session, membership_id, lock=True)
    if row is None:
        raise _error(request, 404, "RESOURCE_NOT_FOUND", "Membership not found.")
    if row.status not in {"pending", "more_info_requested", "approved", "suspended"}:
        raise _error(
            request, 409, "INVALID_STATE", "Membership cannot transition from its current status."
        )
    try:
        row.status = payload.status
        row.verified_at = datetime.now(UTC) if payload.status == "approved" else None
        await session.flush()
        event = await append_event(
            session,
            actor_user_id=user.id,
            action="membership.decision",
            target_type="organization_membership",
            target_id=row.id,
            outcome="success",
            request_id=request.state.request_id,
        )
        session.add(AccountSupportDecisionReason(
            audit_event_id=event.id, reason=payload.reason
        ))
        result = MembershipRead.model_validate(row)
        await session.commit()
        return result
    except Exception:
        await session.rollback()
        raise
