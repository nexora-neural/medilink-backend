"""Database access methods for medication schedules and adherence."""

from datetime import datetime, time
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.medication.enum import DoseStatus, ScheduleStatus
from app.modules.medication.models import (
    DoseOccurrence,
    MedicationSchedule,
    ScheduleTime,
)
from app.modules.prescriptions.core_models import Prescription, PrescriptionItem


async def lock_schedule(
    session: AsyncSession,
    schedule_id: UUID,
) -> MedicationSchedule | None:
    """Load and lock a schedule for an atomic replacement."""

    statement = (
        select(MedicationSchedule)
        .where(MedicationSchedule.id == schedule_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return await session.scalar(statement)


async def get_prescription_item(
    session: AsyncSession,
    item_id: UUID,
) -> PrescriptionItem | None:
    """Get the prescription item associated with a schedule."""

    return await session.get(PrescriptionItem, item_id)


async def get_schedule_times(
    session: AsyncSession,
    schedule_id: UUID,
) -> list[ScheduleTime]:
    """Return reminder times for a schedule."""

    statement = (
        select(ScheduleTime)
        .where(ScheduleTime.schedule_id == schedule_id)
        .order_by(ScheduleTime.time_of_day)
    )
    result = await session.scalars(statement)
    return list(result.all())


async def cancel_future_occurrences(
    session: AsyncSession,
    schedule_id: UUID,
    now: datetime,
) -> int:
    """Cancel only future upcoming or due occurrences."""

    statement = (
        update(DoseOccurrence)
        .where(
            DoseOccurrence.schedule_id == schedule_id,
            DoseOccurrence.scheduled_for > now,
            DoseOccurrence.status.in_((DoseStatus.UPCOMING, DoseStatus.DUE)),
        )
        .values(
            status=DoseStatus.CANCELLED,
            snoozed_until=None,
        )
        .execution_options(synchronize_session="fetch")
    )
    result = await session.execute(statement)
    return result.rowcount or 0


async def cancel_schedule(
    session: AsyncSession,
    schedule: MedicationSchedule,
) -> None:
    """Cancel an existing schedule and increment its version."""

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
) -> MedicationSchedule:
    """Create a replacement schedule and its reminder times."""

    schedule = MedicationSchedule(
        prescription_item_id=prescription_item_id,
        patient_user_id=patient_user_id,
        timezone=timezone,
        starts_at=starts_at,
        ends_at=ends_at,
        confirmed_at=confirmed_at,
        status=ScheduleStatus.ACTIVE,
        version=1,
    )
    session.add(schedule)
    await session.flush()

    for reminder_time in times:
        session.add(
            ScheduleTime(
                schedule_id=schedule.id,
                time_of_day=reminder_time,
            )
        )

    await session.flush()
    await session.refresh(schedule)

    return schedule


async def lock_schedule_prescription(
    session: AsyncSession,
    schedule_id: UUID,
) -> Prescription | None:
    """Lock the parent prescription before any schedule row lock."""

    prescription_id = await session.scalar(
        select(PrescriptionItem.prescription_id)
        .join(
            MedicationSchedule,
            MedicationSchedule.prescription_item_id == PrescriptionItem.id,
        )
        .where(MedicationSchedule.id == schedule_id)
    )

    if prescription_id is None:
        return None

    return await session.scalar(
        select(Prescription)
        .where(Prescription.id == prescription_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
