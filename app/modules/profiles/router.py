"""API router for user profiles."""

from collections.abc import Awaitable, Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import get_session
from app.modules.audit.service import append_event
from app.modules.identity.auth import get_current_user, get_verified_identity
from app.modules.identity.authorization import require_role
from app.modules.identity.models import User
from app.modules.identity.request_limits import require_authenticated_rate_limit
from app.modules.profiles.models import ProfessionalProfile
from app.modules.profiles.route import ProfileRoute
from app.modules.profiles.schemas import (
    AccountUpdateRequest,
    MeResponse,
    OnboardRequest,
    PatientProfileResponse,
    PatientProfileUpdateRequest,
    ProfessionalProfileResponse,
)
from app.modules.profiles.service import (
    build_me_response,
    onboard_identity,
    update_own_account,
    update_own_patient_profile,
)

router = APIRouter(
    tags=["Profiles"],
    route_class=ProfileRoute,
    dependencies=[Depends(require_authenticated_rate_limit)],
)

AccountAuditWriter = Callable[[AsyncSession, UUID, list[str]], Awaitable[None]]


@router.post("/me/onboard", response_model=MeResponse)
async def onboard(
    data: OnboardRequest,
    request: Request,
    identity: Annotated[dict[str, Any], Depends(get_verified_identity)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> MeResponse:
    """Reconcile a verified identity and its requested local profile."""
    try:
        user, created, response = await onboard_identity(session, identity, data)
        await append_event(
            session,
            actor_user_id=user.id,
            action="account.onboarded",
            target_type="user",
            target_id=user.id,
            outcome="success",
            request_id=request.state.request_id,
            metadata={"created": created},
        )
        await session.commit()
        return response
    except IntegrityError as exc:
        await session.rollback()
        cause = getattr(exc.orig, "__cause__", None)
        if getattr(cause, "constraint_name", None) == "uq_users_email_ci":
            raise HTTPException(status_code=409, detail="Account cannot be reconciled.") from None
        raise
    except Exception:
        await session.rollback()
        raise


def get_account_audit_writer(request: Request) -> AccountAuditWriter:
    """Write only field names and keep audit in the caller's transaction."""

    async def write(session: AsyncSession, user_id: UUID, fields: list[str]) -> None:
        await append_event(
            session,
            actor_user_id=user_id,
            action="account.updated",
            target_type="user",
            target_id=user_id,
            outcome="success",
            request_id=request.state.request_id,
            metadata={"changed_fields": fields},
        )

    return write


def get_patient_profile_audit_writer(request: Request) -> AccountAuditWriter:
    async def write(session: AsyncSession, user_id: UUID, fields: list[str]) -> None:
        await append_event(
            session,
            actor_user_id=user_id,
            action="patient_profile.updated",
            target_type="patient_profile",
            target_id=user_id,
            outcome="success",
            request_id=request.state.request_id,
            metadata={"changed_fields": fields},
        )

    return write


@router.get("/me", response_model=MeResponse)
async def get_me(
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> MeResponse:
    """Return account details for the authenticated user."""
    profile = await session.get(ProfessionalProfile, user.id)

    professional_profile = None
    if profile is not None:
        professional_profile = ProfessionalProfileResponse.model_validate(
            profile,
            from_attributes=True,
        )

    return await build_me_response(
        session,
        user,
        professional_profile=professional_profile,
    )


@router.patch("/me", response_model=MeResponse)
async def patch_me(
    data: AccountUpdateRequest,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    write_audit: Annotated[AccountAuditWriter, Depends(get_account_audit_writer)],
) -> MeResponse:
    """Update the authenticated user's account and record the change."""
    try:
        await update_own_account(session, user, data, write_audit=write_audit)
        response = await get_me(user=user, session=session)
        await session.commit()
    except Exception:
        await session.rollback()
        raise

    return response


@router.patch("/me/patient-profile", response_model=PatientProfileResponse)
async def patch_patient_profile(
    data: PatientProfileUpdateRequest,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    write_audit: Annotated[AccountAuditWriter, Depends(get_patient_profile_audit_writer)],
) -> PatientProfileResponse:
    """Update the authenticated patient's own profile."""
    await require_role(session, user, "patient")
    try:
        response = await update_own_patient_profile(session, user, data, write_audit=write_audit)
        await session.commit()
    except Exception:
        await session.rollback()
        raise

    return response
