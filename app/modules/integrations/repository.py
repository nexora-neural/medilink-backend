"""Database access methods for integration administration."""

from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.models import User
from app.modules.integrations.models import IntegrationClient, IntegrationSubmission
from app.modules.organizations.models import Organization, OrganizationMembership
from app.modules.profiles.models import ProfessionalProfile
from app.modules.verification.models import VerificationApplication


def _approved_laboratories() -> Select[tuple[Organization]]:
    """Use one eligibility predicate for both selection and client creation."""
    approved_staff = (
        select(OrganizationMembership.id)
        .join(User, User.id == OrganizationMembership.user_id)
        .join(ProfessionalProfile, ProfessionalProfile.user_id == User.id)
        .join(VerificationApplication, VerificationApplication.user_id == User.id)
        .where(
            OrganizationMembership.organization_id == Organization.id,
            OrganizationMembership.role == "lab_staff",
            OrganizationMembership.status == "approved",
            ProfessionalProfile.role == "lab_staff",
            ProfessionalProfile.verification_status == "approved",
            VerificationApplication.organization_id == Organization.id,
            VerificationApplication.role == "lab_staff",
            VerificationApplication.status == "approved",
            User.status == "active",
        )
        .exists()
    )
    return select(Organization).where(
        Organization.type == "laboratory", Organization.status == "active", approved_staff
    )


async def organization_is_approved_lab(session: AsyncSession, organization_id: UUID) -> bool:
    return await session.scalar(
        _approved_laboratories().where(Organization.id == organization_id)
    ) is not None


async def list_approved_laboratories(
    session: AsyncSession, *, page: int, page_size: int
) -> tuple[list[Organization], int]:
    statement = _approved_laboratories()
    total = await session.scalar(select(func.count()).select_from(statement.subquery()))
    result = await session.scalars(
        statement.order_by(Organization.name, Organization.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(result.all()), int(total or 0)


async def get_client(
    session: AsyncSession, client_id: UUID, *, lock: bool = False
) -> IntegrationClient | None:
    statement = select(IntegrationClient).where(IntegrationClient.id == client_id)
    if lock:
        statement = statement.with_for_update()
    return await session.scalar(statement)


async def list_clients(
    session: AsyncSession, *, page: int, page_size: int
) -> tuple[list[IntegrationClient], int]:
    total = await session.scalar(select(func.count(IntegrationClient.id)))
    result = await session.scalars(
        select(IntegrationClient)
        .order_by(IntegrationClient.created_at.desc(), IntegrationClient.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(result.all()), int(total or 0)


async def list_submissions(
    session: AsyncSession, *, page: int, page_size: int, status: str | None
) -> tuple[list[IntegrationSubmission], int]:
    filters = [] if status is None else [IntegrationSubmission.status == status]
    total = await session.scalar(
        select(func.count(IntegrationSubmission.id)).where(*filters)
    )
    result = await session.scalars(
        select(IntegrationSubmission)
        .where(*filters)
        .order_by(IntegrationSubmission.received_at.desc(), IntegrationSubmission.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(result.all()), int(total or 0)
