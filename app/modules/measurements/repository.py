from sqlalchemy import BigInteger, bindparam, func, select, text, true
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Measurement as MeasurementsModel

measurements = MeasurementsModel.__table__
columns = measurements.c
reading_columns = (
    columns.id, columns.patient_user_id, columns.type, columns.value,
    columns.secondary_value, columns.unit, columns.measured_at, columns.recorded_by,
    columns.source, columns.notes, columns.status, columns.supersedes_id,
    columns.correction_reason, columns.created_at,
)

GRANT_ACCESS = """
    g.patient_user_id = :patient AND g.professional_user_id = :actor
    AND g.status = 'active' AND g.revoked_at IS NULL
    AND g.starts_at <= statement_timestamp() AND g.expires_at > statement_timestamp()
    AND gc.category = 'measurements_vaccinations'
    AND (g.organization_id IS NULL OR EXISTS (
        SELECT 1 FROM organizations o JOIN organization_memberships om
          ON om.organization_id = o.id
        WHERE o.id = g.organization_id AND o.status = 'active'
          AND om.user_id = :actor AND om.role = 'doctor' AND om.status = 'approved'
    ))
"""


async def fetch_one(db: AsyncSession, sql: str, **params):
    return (await db.execute(text(sql), params)).mappings().one_or_none()


async def list_active(db: AsyncSession, patient, kind, start, end, page, page_size):
    eligible_rows = select(*reading_columns).where(
        columns.patient_user_id == patient,
        columns.status == "active",
    )
    if kind is not None:
        eligible_rows = eligible_rows.where(columns.type == kind)
    if start is not None:
        eligible_rows = eligible_rows.where(columns.measured_at >= start)
    if end is not None:
        eligible_rows = eligible_rows.where(columns.measured_at < end)

    # Use one statement snapshot for the count and page, even when the page is empty.
    eligible = eligible_rows.cte("eligible")
    totals = select(func.count().label("total")).select_from(eligible).subquery("totals")
    offset = min((page - 1) * page_size, 2**63 - 1)
    page_rows = (
        select(eligible)
        .order_by(eligible.c.measured_at.desc(), eligible.c.id.desc())
        .limit(page_size)
        .offset(bindparam("page_offset", offset, type_=BigInteger))
        .lateral("page_rows")
    )
    statement = (
        select(page_rows, totals.c.total)
        .select_from(totals.outerjoin(page_rows, true()))
        .order_by(page_rows.c.measured_at.desc(), page_rows.c.id.desc())
    )
    rows = (await db.execute(statement)).mappings().all()
    return {
        "items": [row for row in rows if row["id"] is not None],
        "page": page, "page_size": page_size, "total": rows[0]["total"],
    }


async def insert_reading(db, body, patient, actor, kind, source, previous=None, reason=None):
    statement = measurements.insert().values(
        patient_user_id=patient, type=kind, value=body.value,
        secondary_value=body.secondary_value, unit=body.unit,
        measured_at=body.measured_at, recorded_by=actor, source=source,
        notes=body.notes, supersedes_id=previous, correction_reason=reason,
    ).returning(*reading_columns)
    return (await db.execute(statement)).mappings().one()


async def lock_owned_reading(db, measurement_id, patient):
    statement = (
        select(columns.id, columns.patient_user_id, columns.type, columns.source, columns.status)
        .where(columns.id == measurement_id, columns.patient_user_id == patient)
        .with_for_update()
    )
    return (await db.execute(statement)).mappings().one_or_none()


async def mark_superseded(db, measurement_id):
    await db.execute(
        measurements.update().where(columns.id == measurement_id).values(status="superseded")
    )
