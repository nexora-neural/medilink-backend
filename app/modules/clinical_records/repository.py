"""Clinical typed-table mapping and current authorization evidence."""

from uuid import UUID

from sqlalchemy import Text, cast, func, literal, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.clinical_records.models import (
    AllergiesModel,
    ClinicalNotesModel,
    ConditionsModel,
    DiagnosesModel,
)
from app.modules.clinical_records.schemas import ClinicalInput, ClinicalListQuery
from app.modules.consent.models import AccessGrant, GrantCategory
from app.modules.consultations.models import Consultation
from app.modules.identity.models import Role, UserRole
from app.modules.organizations.models import Organization, OrganizationMembership
from app.modules.profiles.models import ProfessionalProfile

TABLES = {
    "condition": (ConditionsModel, "condition_name", "recorded_at", "description"),
    "diagnosis": (DiagnosesModel, "diagnosis_name", "diagnosed_at", "description"),
    "allergy": (AllergiesModel, "allergen", "recorded_at", "reaction"),
    "note": (ClinicalNotesModel, "note_type", "recorded_at", "note_text"),
}


def records_query():
    """Expose one bounded API shape without a new clinical_records table."""
    queries = []
    for kind, (model, title, timestamp, description) in TABLES.items():
        queries.append(
            select(
                model.id,
                model.patient_user_id,
                model.consultation_id,
                literal(kind).label("type"),
                getattr(model, title).label("title"),
                getattr(model, description).label("description"),
                model.clinical_code,
                cast(model.source, Text).label("source"),
                model.recorded_by,
                model.organization_id,
                cast(model.status, Text).label("status"),
                getattr(model, timestamp).label("recorded_at"),
                model.created_at,
                model.supersedes_id,
                model.correction_reason,
            )
        )
    return union_all(*queries).subquery()


async def find_record(db: AsyncSession, record_id: UUID):
    records = records_query()
    rows = (await db.execute(select(records).where(records.c.id == record_id))).mappings().all()
    # UUIDs are generated independently in the typed tables. Never guess if a
    # corrupt/imported fixture happens to reuse an ID across two record types.
    return rows[0] if len(rows) == 1 else None


async def list_records(db: AsyncSession, patient_id: UUID, query: ClinicalListQuery):
    records = records_query()
    filters = [records.c.patient_user_id == patient_id]
    if query.type is not None:
        filters.append(records.c.type == query.type)
    if query.start is not None:
        filters.extend([records.c.recorded_at >= query.start, records.c.recorded_at < query.end])
    total = await db.scalar(select(func.count()).select_from(records).where(*filters))
    result = await db.execute(
        select(records)
        .where(*filters)
        .order_by(records.c.created_at.desc(), records.c.id.desc(), records.c.type)
        .offset((query.page - 1) * query.page_size)
        .limit(query.page_size)
    )
    return {
        "items": result.mappings().all(),
        "page": query.page,
        "page_size": query.page_size,
        "total": total,
    }


async def insert_record(db, patient_id, actor_id, body: ClinicalInput, source, organization_id):
    model, title, timestamp, description = TABLES[body.type]
    values = {
        "patient_user_id": patient_id,
        "recorded_by": actor_id,
        "consultation_id": body.consultation_id,
        "organization_id": organization_id,
        "source": source,
        "status": "active",
        title: body.title,
        timestamp: body.recorded_at,
        description: body.description,
    }
    if body.type == "note":
        values[description] = body.description or ""
    values["clinical_code"] = body.clinical_code
    row = model(**values)
    db.add(row)
    await db.flush()
    return await find_record(db, row.id)


async def current_roles(db, actor_id):
    return set(
        (
            await db.scalars(
                select(Role.code)
                .join(UserRole, UserRole.role_id == Role.id)
                .where(
                    UserRole.user_id == actor_id,
                    UserRole.revoked_at.is_(None),
                )
                .with_for_update(read=True, of=UserRole)
            )
        ).all()
    )


async def verified_doctor(db, actor_id):
    return await db.scalar(
        select(ProfessionalProfile.user_id)
        .where(
            ProfessionalProfile.user_id == actor_id,
            ProfessionalProfile.role == "doctor",
            ProfessionalProfile.verification_status == "approved",
        )
        .with_for_update(read=True)
    )


async def approved_membership(db, actor_id, organization_id):
    return await db.scalar(
        select(OrganizationMembership.id)
        .join(
            Organization,
            Organization.id == OrganizationMembership.organization_id,
        )
        .where(
            OrganizationMembership.user_id == actor_id,
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.role == "doctor",
            OrganizationMembership.status == "approved",
            Organization.status == "active",
        )
        .with_for_update(read=True, of=(Organization, OrganizationMembership))
    )


async def grants(db, actor_id, patient_id, consultation_id=None):
    stmt = (
        select(AccessGrant)
        .join(
            GrantCategory,
            GrantCategory.grant_id == AccessGrant.id,
        )
        .where(
            AccessGrant.patient_user_id == patient_id,
            AccessGrant.professional_user_id == actor_id,
            GrantCategory.category == "history_allergies",
            AccessGrant.status == "active",
            AccessGrant.revoked_at.is_(None),
        )
    )
    locks = (AccessGrant, GrantCategory)
    if consultation_id is not None:
        stmt = stmt.join(Consultation, Consultation.grant_id == AccessGrant.id).where(
            Consultation.id == consultation_id,
            Consultation.patient_user_id == patient_id,
            Consultation.doctor_user_id == actor_id,
            Consultation.organization_id.is_not_distinct_from(AccessGrant.organization_id),
            Consultation.ended_at.is_(None),
            AccessGrant.allow_consultation.is_(True),
        )
        locks += (Consultation,)
    result = await db.scalars(stmt.order_by(AccessGrant.id).with_for_update(read=True, of=locks))
    return result.all()


async def consultation_started(db, consultation_id, now):
    return await db.scalar(
        select(Consultation.id).where(
            Consultation.id == consultation_id,
            Consultation.started_at <= now,
        )
    )
