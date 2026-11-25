"""Business rules and orchestration for medication schedules and adherence."""

from datetime import UTC, datetime, time
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.service import append_event
from app.modules.medication import repository
from app.modules.medication.enum import ScheduleStatus
from app.modules.medication.schemas import ScheduleInput


class MedicationScheduleError(Exception):
    """Medication schedule business-rule error."""

    def __init__(self, status: int, code: str, message: str) -> None:
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


async def replace_schedule(
    session: AsyncSession,
    *,
    schedule_id: UUID,
    actor_user_id: UUID,
    expected_version: int,
    payload: ScheduleInput,
    request_id: str,
):
    """Atomically replace reminder times for an existing schedule."""

    try:
        parent = await repository.lock_schedule_prescription(
            session,
            schedule_id,
        )

        if parent is None:
            raise MedicationScheduleError(
                404,
                "NOT_FOUND",
                "Medication schedule was not found.",
            )

        old_schedule = await repository.lock_schedule(
            session,
            schedule_id,
        )

        if old_schedule is None:
            raise MedicationScheduleError(404, "NOT_FOUND", "Medication schedule was not found.")

        if old_schedule.patient_user_id != actor_user_id:
            raise MedicationScheduleError(
                403, "ACCESS_DENIED", "You do not have access to this schedule."
            )

        if parent.patient_user_id != actor_user_id:
            raise MedicationScheduleError(
                403,
                "ACCESS_DENIED",
                "You do not have access to this prescription.",
            )

        item = await repository.get_prescription_item(
            session,
            old_schedule.prescription_item_id,
        )

        if item is None or item.prescription_id != parent.id:
            raise MedicationScheduleError(
                409,
                "INVALID_STATE",
                "The schedule prescription link has changed.",
            )

        if parent.status != "active" or parent.issued_at is None:
            raise MedicationScheduleError(
                409,
                "INVALID_STATE",
                "Only active prescriptions can have replacement schedules.",
            )

        if old_schedule.version != expected_version:
            raise MedicationScheduleError(
                409, "STALE_VERSION", "The schedule has changed. Refresh and try again."
            )

        if old_schedule.status != ScheduleStatus.ACTIVE:
            raise MedicationScheduleError(
                409, "INVALID_STATE", "Only active schedules can be replaced."
            )

        if payload.prescription_item_id != old_schedule.prescription_item_id:
            raise MedicationScheduleError(
                422, "VALIDATION_ERROR", "A replacement must use the same prescription item."
            )

        now = datetime.now(UTC)

        await repository.cancel_future_occurrences(session, old_schedule.id, now)
        await repository.cancel_schedule(session, old_schedule)

        reminder_times = [time.fromisoformat(value) for value in payload.times]

        new_schedule = await repository.create_schedule(
            session,
            prescription_item_id=old_schedule.prescription_item_id,
            patient_user_id=old_schedule.patient_user_id,
            timezone=payload.timezone,
            starts_at=payload.starts_at,
            ends_at=payload.ends_at,
            confirmed_at=now,
            times=reminder_times,
        )

        await append_event(
            session,
            action="medication_schedule.replaced",
            target_type="medication_schedule",
            target_id=new_schedule.id,
            outcome="success",
            request_id=request_id,
            actor_user_id=actor_user_id,
            metadata={
                "previous_schedule_id": str(old_schedule.id),
                "previous_version": expected_version,
                "new_version": new_schedule.version,
            },
        )

        times = await repository.get_schedule_times(session, new_schedule.id)
        await session.commit()
        return new_schedule, times

    except Exception:
        await session.rollback()
        raise
