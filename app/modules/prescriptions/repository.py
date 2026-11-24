"""Database access methods for prescriptions and OCR workflow."""

from uuid import UUID
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.medication.models import MedicationSchedule
from app.modules.prescriptions.core_models import Medicine, Prescription, PrescriptionItem
from app.modules.prescriptions.schemas import MedicineInput , PrescriptionInput


async def get_medicines(
    session: AsyncSession,
    q: str | None,
    page: int,
    page_size: int,
) -> tuple[list[Medicine], int]:
    """Search and paginate sample medicines."""

    statement = select(Medicine).where(Medicine.is_sample.is_(True))

    if q and q.strip():
        term = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{term}%"
        statement = statement.where(
            or_(
                Medicine.generic_name.ilike(pattern, escape="\\"),
                Medicine.brand_name.ilike(pattern, escape="\\"),
                Medicine.strength.ilike(pattern, escape="\\"),
                Medicine.form.ilike(pattern, escape="\\"),
            )
        )

    count_query = select(func.count()).select_from(statement.order_by(None).subquery())
    total = (await session.scalar(count_query)) or 0

    statement = (
        statement.order_by(Medicine.created_at.desc(), Medicine.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await session.scalars(statement)
    return list(result.all()), total


async def create_medicine(
    session: AsyncSession,
    payload: MedicineInput,
) -> Medicine | None:
    """Insert a sample medicine unless its identifying fields already exist."""

    # Catalogue writers share a transaction lock so the duplicate check sees
    # the previous writer's committed row, even across application workers.
    await session.execute(select(func.pg_advisory_xact_lock(717, 262)))
    duplicate = await session.scalar(
        select(Medicine.id)
        .where(
            Medicine.is_sample.is_(True),
            func.lower(func.btrim(Medicine.generic_name)) == func.lower(payload.generic_name),
            func.lower(func.btrim(Medicine.brand_name)).is_not_distinct_from(
                func.lower(payload.brand_name)
            ),
            func.lower(func.btrim(Medicine.strength)).is_not_distinct_from(
                func.lower(payload.strength)
            ),
            func.lower(func.btrim(Medicine.form)).is_not_distinct_from(func.lower(payload.form)),
        )
        .limit(1)
    )
    if duplicate is not None:
        return None

    medicine = Medicine(
        generic_name=payload.generic_name,
        brand_name=payload.brand_name,
        strength=payload.strength,
        form=payload.form,
        is_sample=True,
    )
    session.add(medicine)
    await session.flush()
    await session.refresh(medicine)
    return medicine


async def lock_prescription(
    session: AsyncSession,
    prescription_id,
) -> Prescription | None:
    """Lock a prescription row before checking its version."""

    statement = (
        select(Prescription)
        .where(Prescription.id == prescription_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return await session.scalar(statement)


async def prescription_has_scheduled_items(
    session: AsyncSession,
    prescription_id,
) -> bool:
    """Check whether any prescription item is referenced by a medication schedule."""

    statement = (
        select(MedicationSchedule.id)
        .join(
            PrescriptionItem,
            MedicationSchedule.prescription_item_id == PrescriptionItem.id,
        )
        .where(PrescriptionItem.prescription_id == prescription_id)
        .limit(1)
    )
    return await session.scalar(statement) is not None


async def delete_prescription_items(
    session: AsyncSession,
    prescription_id,
) -> None:
    """Delete draft items only after confirming that none are scheduled."""

    from sqlalchemy import delete

    await session.execute(
        delete(PrescriptionItem).where(
            PrescriptionItem.prescription_id == prescription_id
        )
    )


async def create_prescription_items(
    session: AsyncSession,
    prescription_id,
    items,
) -> list[PrescriptionItem]:
    """Insert replacement prescription items."""

    records = [
        PrescriptionItem(
            prescription_id=prescription_id,
            medicine_id=item.medicine_id,
            medicine_name_snapshot=item.medicine_name_snapshot,
            dose_text=item.dose_text,
            route=item.route,
            frequency=item.frequency,
            duration_days=item.duration_days,
            instructions=item.instructions,
            quantity=item.quantity,
            quantity_unit=item.quantity_unit,
            is_prn=item.is_prn,
        )
        for item in items
    ]
    session.add_all(records)
    await session.flush()
    for record in records:
        await session.refresh(record)
    return records

async def find_prescription(
    session: AsyncSession,
    prescription_id: UUID,
) -> Prescription | None:
    return await session.scalar(
        select(Prescription)
        .where(Prescription.id == prescription_id)
        .execution_options(populate_existing=True)
    )


async def list_patient_prescriptions(
    session: AsyncSession,
    *,
    patient_id: UUID,
    page: int,
    page_size: int,
    status: str | None = None,
    source: str | None = None,
) -> tuple[list[Prescription], int]:
    filters = [Prescription.patient_user_id == patient_id]

    if status is not None:
        filters.append(Prescription.status == status)

    if source is not None:
        filters.append(Prescription.source == source)

    total = await session.scalar(
        select(func.count())
        .select_from(Prescription)
        .where(*filters)
    )

    rows = await session.scalars(
        select(Prescription)
        .where(*filters)
        .order_by(
            Prescription.created_at.desc(),
            Prescription.id.desc(),
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    )

    return list(rows.all()), int(total or 0)


async def create_prescription_draft(
    session: AsyncSession,
    *,
    patient_id: UUID,
    actor_id: UUID,
    source: str,
    payload: PrescriptionInput,
) -> Prescription:
    if source not in {"patient", "doctor"}:
        raise ValueError("Unsupported prescription draft source.")

    prescription = Prescription(
        patient_user_id=patient_id,
        consultation_id=(
            payload.consultation_id if source == "doctor" else None
        ),
        prescriber_user_id=actor_id if source == "doctor" else None,
        source=source,
        claimed_prescriber=(
            payload.claimed_prescriber if source == "patient" else None
        ),
        review_status="not_reviewed",
        status="draft",
        starts_on=payload.starts_on,
        ends_on=payload.ends_on,
        original_document_id=payload.original_document_id,
        created_by=actor_id,
        version=1,
    )

    session.add(prescription)
    await session.flush()

    await create_prescription_items(
        session,
        prescription.id,
        payload.items,
    )

    await session.refresh(prescription)
    return prescription

async def delete_draft_rows(
    session: AsyncSession,
    prescription_id: UUID,
) -> None:
    """Delete items and parent; foreign keys protect retained evidence."""
    from sqlalchemy import delete

    await delete_prescription_items(session, prescription_id)

    await session.execute(
        delete(Prescription).where(
            Prescription.id == prescription_id
        )
    )
    await session.flush()