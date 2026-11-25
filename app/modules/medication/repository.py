"""Schedule persistence with parent-before-schedule locking and retained history."""

from datetime import datetime, time
from uuid import UUID

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.medication.enum import DoseStatus, ScheduleStatus
from app.modules.medication.models import DoseOccurrence, MedicationSchedule, ScheduleTime
from app.modules.prescriptions.core_models import Prescription, PrescriptionItem


async def lock_schedule(session: AsyncSession, schedule_id: UUID):
    return await session.scalar(
        select(MedicationSchedule)
        .where(MedicationSchedule.id == schedule_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def get_prescription_item(session, item_id):
    return await session.get(PrescriptionItem, item_id)


async def lock_item_context(session, item_id, patient_id):
    # Match prescription mutation order: parent prescription, item, then schedule.
    prescription = await session.scalar(
        select(Prescription)
        .join(
            PrescriptionItem,
            PrescriptionItem.prescription_id == Prescription.id,
        )
        .where(PrescriptionItem.id == item_id, Prescription.patient_user_id == patient_id)
        .with_for_update(read=True, of=Prescription)
        .execution_options(populate_existing=True)
    )
    if prescription is None:
        return None
    item = await session.scalar(
        select(PrescriptionItem)
        .where(
            PrescriptionItem.id == item_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return (item, prescription) if item is not None else None


async def get_schedule_times(session, schedule_id):
    return list(
        (
            await session.scalars(
                select(ScheduleTime)
                .where(
                    ScheduleTime.schedule_id == schedule_id,
                )
                .order_by(ScheduleTime.time_of_day, ScheduleTime.id)
            )
        ).all()
    )


async def times_for_schedules(session, schedule_ids):
    rows = await session.scalars(
        select(ScheduleTime)
        .where(
            ScheduleTime.schedule_id.in_(schedule_ids),
        )
        .order_by(ScheduleTime.time_of_day, ScheduleTime.id)
    )
    groups = {}
    for row in rows:
        groups.setdefault(row.schedule_id, []).append(row)
    return groups


async def list_schedules(session, patient_id, query):
    conditions = [MedicationSchedule.patient_user_id == patient_id]
    if query.status is not None:
        conditions.append(MedicationSchedule.status == query.status)
    total = await session.scalar(
        select(func.count()).select_from(MedicationSchedule).where(*conditions)
    )
    rows = (
        await session.execute(
            select(MedicationSchedule, Prescription.source)
            .join(
                PrescriptionItem,
                PrescriptionItem.id == MedicationSchedule.prescription_item_id,
            )
            .join(Prescription, Prescription.id == PrescriptionItem.prescription_id)
            .where(*conditions)
            .order_by(MedicationSchedule.created_at.desc(), MedicationSchedule.id.desc())
            .offset((query.page - 1) * query.page_size)
            .limit(query.page_size)
        )
    ).all()
    return rows, total


async def overlapping_schedule(session, item_id, start, end, *, exclude_id=None):
    clauses = [
        MedicationSchedule.prescription_item_id == item_id,
        MedicationSchedule.status.in_([ScheduleStatus.ACTIVE, ScheduleStatus.PAUSED]),
        or_(MedicationSchedule.ends_at.is_(None), MedicationSchedule.ends_at > start),
    ]
    if end is not None:
        clauses.append(MedicationSchedule.starts_at < end)
    if exclude_id is not None:
        clauses.append(MedicationSchedule.id != exclude_id)
    return await session.scalar(select(MedicationSchedule.id).where(*clauses).limit(1))


async def cancel_future_occurrences(session, schedule_id, now):
    result = await session.execute(
        update(DoseOccurrence)
        .where(
            DoseOccurrence.schedule_id == schedule_id,
            DoseOccurrence.scheduled_for > now,
            DoseOccurrence.status.in_([DoseStatus.UPCOMING, DoseStatus.DUE]),
        )
        .values(status=DoseStatus.CANCELLED, snoozed_until=None)
        .execution_options(synchronize_session="fetch")
    )
    return result.rowcount or 0


async def cancel_schedule(session, schedule):
    schedule.status = ScheduleStatus.CANCELLED
    schedule.version += 1
    await session.flush()


async def create_schedule(
    session: AsyncSession,
    *,
    prescription_item_id: UUID,
    patient_user_id: UUID,
    timezone: str,
    starts_at: datetime,
    ends_at: datetime | None,
    confirmed_at: datetime,
    times: list[time],
    previous_schedule_id: UUID | None = None,
):
    schedule = MedicationSchedule(
        prescription_item_id=prescription_item_id,
        patient_user_id=patient_user_id,
        timezone=timezone,
        starts_at=starts_at,
        ends_at=ends_at,
        confirmed_at=confirmed_at,
        status=ScheduleStatus.ACTIVE,
        version=1,
        previous_schedule_id=previous_schedule_id,
    )
    session.add(schedule)
    await session.flush()
    for reminder_time in times:
        session.add(ScheduleTime(schedule_id=schedule.id, time_of_day=reminder_time))
    await session.flush()
    await session.refresh(schedule)
    return schedule
