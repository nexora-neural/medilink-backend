"""Reusable authorization checks for protected API operations."""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.consent.models import AccessGrant, GrantCategory
from app.modules.consultations.models import Consultation
from app.modules.identity.models import Role, User, UserRole
from app.modules.organizations.models import Organization, OrganizationMembership
from app.modules.profiles.models import ProfessionalProfile


def deny() -> None:
    raise HTTPException(status_code=403, detail="Access denied.")


async def require_role(session: AsyncSession, user: User, role: str) -> None:
    """Check current account status and an explicitly required role."""
    if user.status != "active":
        deny()
    result = await session.scalar(
        select(Role.id).join(UserRole, UserRole.role_id == Role.id).where(
            UserRole.user_id == user.id, UserRole.revoked_at.is_(None), Role.code == role
        )
    )
    if result is None:
        deny()


async def require_patient_source_correction(
    session: AsyncSession,
    user: User,
    *,
    patient_id: UUID,
    author_id: UUID,
    source: str,
    status: str,
) -> None:
    """Allow correction only of one's own active patient-authored record.

    Callers must load and lock the record from the database before invoking this
    check; these values must never be taken from request payloads.
    """
    await require_role(session, user, "patient")
    if user.id != patient_id or user.id != author_id or source != "patient" or status != "active":
        deny()


async def require_verified_membership(
    session: AsyncSession, user: User, role: str, organization_id: UUID
) -> None:
    """Require an approved professional and approved membership of an active organization."""
    await require_role(session, user, role)
    profile = await session.scalar(
        select(ProfessionalProfile.user_id).where(
            ProfessionalProfile.user_id == user.id,
            ProfessionalProfile.role == role,
            ProfessionalProfile.verification_status == "approved",
        )
    )
    membership = await session.scalar(
        select(OrganizationMembership.id).join(
            Organization, Organization.id == OrganizationMembership.organization_id
        ).where(
            OrganizationMembership.user_id == user.id,
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.role == role,
            OrganizationMembership.status == "approved",
            Organization.status == "active",
        )
    )
    if profile is None or membership is None:
        deny()


async def require_doctor_grant(
    session: AsyncSession,
    user: User,
    patient_id: UUID,
    organization_id: UUID,
    category: str,
    *,
    clinical_write: bool = False,
) -> None:
    """Check the current grant and, for clinical writes, an active consultation."""
    if category not in {
        "history_allergies", "prescriptions", "reports",
        "measurements_vaccinations", "medication_logs",
    }:
        deny()
    await require_verified_membership(session, user, "doctor", organization_id)
    now = datetime.now(UTC)
    grant_ids = select(AccessGrant.id).join(
        GrantCategory, GrantCategory.grant_id == AccessGrant.id
    ).where(
        AccessGrant.patient_user_id == patient_id,
        AccessGrant.professional_user_id == user.id,
        AccessGrant.organization_id == organization_id,
        AccessGrant.status == "active",
        AccessGrant.revoked_at.is_(None),
        AccessGrant.starts_at <= now,
        AccessGrant.expires_at > now,
        GrantCategory.category == category,
    )
    if clinical_write:
        grant_ids = grant_ids.join(Consultation, Consultation.grant_id == AccessGrant.id).where(
            AccessGrant.allow_consultation.is_(True),
            Consultation.patient_user_id == patient_id,
            Consultation.doctor_user_id == user.id,
            Consultation.organization_id == organization_id,
            Consultation.ended_at.is_(None),
        )
    if await session.scalar(grant_ids.limit(1)) is None:
        deny()
