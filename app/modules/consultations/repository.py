"""Database access methods for consultations and patient queue."""

from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy import BigInteger, bindparam, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.consent.models import AccessGrant, GrantCategory
from app.modules.consultations.models import Consultation
from app.modules.consultations.schemas import ConsultationListQuery
from app.modules.identity.models import User
from app.modules.organizations.models import Organization, OrganizationMembership
from app.modules.profiles.models import PatientProfile


async def lock_doctor_grant(
    db: AsyncSession,
    doctor_id: UUID,
    grant_id: UUID,
) -> AccessGrant | None:
    """Lock the selected grant only if it belongs to this doctor."""
    return await db.scalar(
        select(AccessGrant)
        .where(
            AccessGrant.id == grant_id,
            AccessGrant.professional_user_id == doctor_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def insert_consultation(
    db: AsyncSession,
    consultation: Consultation,
) -> None:
    db.add(consultation)
    await db.flush()


async def find_owned_consultation(
    db: AsyncSession,
    doctor_id: UUID,
    consultation_id: UUID,
    *,
    lock: bool = False,
) -> Consultation | None:
    statement = select(Consultation).where(
        Consultation.id == consultation_id,
        Consultation.doctor_user_id == doctor_id,
    )

    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return await db.scalar(statement)


async def list_patient_consultations(
    db: AsyncSession,
    patient_id: UUID,
    query: ConsultationListQuery,
    *,
    doctor_id: UUID | None,
) -> tuple[list[Consultation], int]:
    """Caller must authorize the patient and the requested viewing scope."""
    conditions = [Consultation.patient_user_id == patient_id]

    if doctor_id is not None:
        now = datetime.now(UTC)

        approved_membership = (
            select(OrganizationMembership.id)
            .join(
                Organization,
                Organization.id == OrganizationMembership.organization_id,
            )
            .where(
                OrganizationMembership.user_id == doctor_id,
                OrganizationMembership.organization_id == AccessGrant.organization_id,
                OrganizationMembership.role == "doctor",
                OrganizationMembership.status == "approved",
                Organization.status == "active",
            )
            .correlate(AccessGrant)
            .exists()
        )

        has_categories = (
            select(GrantCategory.grant_id)
            .where(GrantCategory.grant_id == AccessGrant.id)
            .correlate(AccessGrant)
            .exists()
        )

        current_grant = (
            select(AccessGrant.id)
            .where(
                AccessGrant.patient_user_id == Consultation.patient_user_id,
                AccessGrant.professional_user_id == doctor_id,
                AccessGrant.organization_id.is_not_distinct_from(Consultation.organization_id),
                AccessGrant.status == "active",
                AccessGrant.revoked_at.is_(None),
                AccessGrant.starts_at <= now,
                AccessGrant.expires_at > now,
                has_categories,
                or_(
                    AccessGrant.organization_id.is_(None),
                    approved_membership,
                ),
            )
            .correlate(Consultation)
            .exists()
        )

        conditions.extend(
            [
                Consultation.doctor_user_id == doctor_id,
                current_grant,
            ]
        )

    total = await db.scalar(select(func.count()).select_from(Consultation).where(*conditions))

    offset = min(
        (query.page - 1) * query.page_size,
        2**63 - 1,
    )

    result = await db.scalars(
        select(Consultation)
        .where(*conditions)
        .order_by(
            Consultation.started_at.desc(),
            Consultation.id.desc(),
        )
        .limit(query.page_size)
        .offset(
            bindparam(
                "consultation_offset",
                offset,
                type_=BigInteger,
            )
        )
    )

    return list(result.all()), int(total or 0)


async def list_doctor_queue(
    db: AsyncSession,
    doctor_id: UUID,
    query: ConsultationListQuery,
    now: datetime,
) -> tuple[list[tuple[AccessGrant, str, date]], int]:
    """Return current grants to this doctor, not a patient directory."""
    approved_membership = (
        select(OrganizationMembership.id)
        .join(
            Organization,
            Organization.id == OrganizationMembership.organization_id,
        )
        .where(
            OrganizationMembership.user_id == doctor_id,
            OrganizationMembership.organization_id == AccessGrant.organization_id,
            OrganizationMembership.role == "doctor",
            OrganizationMembership.status == "approved",
            Organization.status == "active",
        )
        .correlate(AccessGrant)
        .exists()
    )

    has_categories = (
        select(GrantCategory.grant_id)
        .where(GrantCategory.grant_id == AccessGrant.id)
        .correlate(AccessGrant)
        .exists()
    )

    eligible = (
        select(
            AccessGrant,
            User.display_name,
            PatientProfile.date_of_birth,
        )
        .join(User, User.id == AccessGrant.patient_user_id)
        .join(
            PatientProfile,
            PatientProfile.user_id == AccessGrant.patient_user_id,
        )
        .where(
            AccessGrant.professional_user_id == doctor_id,
            AccessGrant.status == "active",
            AccessGrant.revoked_at.is_(None),
            AccessGrant.starts_at <= now,
            AccessGrant.expires_at > now,
            User.status == "active",
            has_categories,
            or_(
                AccessGrant.organization_id.is_(None),
                approved_membership,
            ),
        )
    )

    total = await db.scalar(select(func.count()).select_from(eligible.subquery()))

    offset = min(
        (query.page - 1) * query.page_size,
        2**63 - 1,
    )

    result = await db.execute(
        eligible.order_by(
            AccessGrant.created_at.desc(),
            AccessGrant.id.desc(),
        )
        .limit(query.page_size)
        .offset(
            bindparam(
                "queue_offset",
                offset,
                type_=BigInteger,
            )
        )
    )

    items = [
        (grant, display_name, date_of_birth) for grant, display_name, date_of_birth in result.all()
    ]

    return items, int(total or 0)
