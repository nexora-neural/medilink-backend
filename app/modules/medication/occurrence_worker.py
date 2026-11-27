"""Idempotent scheduled-dose generation and in-app reminder delivery."""

from datetime import UTC, date, datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.service import append_event
from app.modules.medication.enum import DoseStatus, ScheduleStatus
from app.modules.medication.models import (
    DoseOccurrence,
    MedicationLog,
    MedicationSchedule,
    ScheduleStatusChange,
    ScheduleTime,
)
from app.modules.notifications.models import Notification

REMINDER_HORIZON = timedelta(hours=24)
MAX_CATCH_UP_DAYS = 31


def _local_instant(day: date, at: time, zone: ZoneInfo) -> datetime | None:
    """Resolve a wall-clock dose time; skip nonexistent DST times, choose fold 0."""
    local_naive = datetime.combine(day, at)
    local = local_naive.replace(tzinfo=zone, fold=0)
    instant = local.astimezone(UTC)
    if instant.astimezone(zone).replace(tzinfo=None) != local_naive:
        return None
    return instant


async def _deliver_due_reminders(session: AsyncSession, schedule, now, active_since):
    has_log = select(MedicationLog.id).where(
        MedicationLog.dose_occurrence_id == DoseOccurrence.id
    ).correlate(DoseOccurrence).exists()
    conditions = [
        DoseOccurrence.schedule_id == schedule.id,
        DoseOccurrence.status.in_([DoseStatus.DUE, DoseStatus.UNREPORTED]),
        DoseOccurrence.scheduled_for >= schedule.starts_at,
        DoseOccurrence.scheduled_for <= now,
        ~has_log,
        (DoseOccurrence.snoozed_until.is_(None)) | (DoseOccurrence.snoozed_until <= now),
    ]
    if schedule.ends_at is not None:
        conditions.append(DoseOccurrence.scheduled_for < schedule.ends_at)
    rows = (
        await session.scalars(
            select(DoseOccurrence)
            .where(*conditions)
            .order_by(DoseOccurrence.scheduled_for, DoseOccurrence.id)
            .with_for_update(skip_locked=True)
        )
    ).all()
    delivered = 0
    for occurrence in rows:
        snooze_time = occurrence.snoozed_until
        # Resume must not create a delayed notification for an old, unanswered
        # dose that occurred while the schedule was paused.
        if snooze_time is None and occurrence.scheduled_for < active_since:
            continue
        if snooze_time is None and await session.scalar(
            select(Notification.id).where(
                Notification.related_type == "dose_occurrence",
                Notification.related_id == occurrence.id,
                Notification.notification_type == "dose_reminder",
            ).limit(1)
        ):
            continue
        delivery_slot = (
            f"snooze:{int(snooze_time.timestamp() * 1_000_000)}"
            if snooze_time is not None
            else "scheduled"
        )
        result = await session.execute(
            insert(Notification)
            .values(
                user_id=schedule.patient_user_id,
                notification_type="dose_reminder",
                title="Medication reminder",
                body="A scheduled medicine dose is due.",
                related_type="dose_occurrence",
                related_id=occurrence.id,
                dedupe_key=f"dose_reminder:{occurrence.id}:{delivery_slot}",
            )
            .on_conflict_do_nothing(index_elements=[Notification.dedupe_key])
        )
        delivered += max(result.rowcount or 0, 0)
        if snooze_time is not None:
            # The notification row now retains the delivery evidence; this only
            # clears the pending reminder time and never touches dose status.
            occurrence.snoozed_until = None
    return delivered


async def process_active_schedules(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    batch_size: int = 100,
) -> int:
    """Generate one row per scheduled time, evaluate status and enqueue reminders.

    The caller owns the transaction and commit. Re-running this function is safe:
    the schedule cursor bounds generation and the database unique key plus
    notification dedupe key protect retries and concurrent worker instances.
    """
    if now is None:
        now = await session.scalar(select(func.clock_timestamp()))
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    horizon = now + REMINDER_HORIZON
    schedules = (
        await session.scalars(
            select(MedicationSchedule)
            .where(
                MedicationSchedule.status == ScheduleStatus.ACTIVE,
                MedicationSchedule.starts_at <= horizon,
            )
            .order_by(
                MedicationSchedule.occurrences_worker_processed_at.asc().nulls_first(),
                MedicationSchedule.id,
            )
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
    ).all()

    changed = 0
    for schedule in schedules:
        schedule.occurrences_worker_processed_at = await session.scalar(
            select(func.clock_timestamp())
        )
        zone = ZoneInfo(schedule.timezone)
        cursor = schedule.occurrences_generated_through
        start = max(cursor, schedule.starts_at, schedule.confirmed_at)
        through = min(horizon, schedule.ends_at) if schedule.ends_at is not None else horizon
        if start <= through:
            start_day = start.astimezone(zone).date()
            end_day = through.astimezone(zone).date()
            if (end_day - start_day).days >= MAX_CATCH_UP_DAYS:
                end_day = start_day + timedelta(days=MAX_CATCH_UP_DAYS - 1)
                day_end = datetime.combine(end_day, time.max, zone).astimezone(UTC)
                through = min(through, day_end)

            times = await session.scalars(
                select(ScheduleTime.time_of_day).where(ScheduleTime.schedule_id == schedule.id)
            )
            schedule_times = list(times.all())
            occurrence_rows = []
            for offset in range((end_day - start_day).days + 1):
                day = start_day + timedelta(days=offset)
                for schedule_time in schedule_times:
                    instant = _local_instant(day, schedule_time, zone)
                    if instant is None or instant < start or instant > through:
                        continue
                    if schedule.ends_at is not None and instant >= schedule.ends_at:
                        continue
                    status = (
                        DoseStatus.UNREPORTED
                        if instant < now
                        else DoseStatus.DUE
                        if instant == now
                        else DoseStatus.UPCOMING
                    )
                    occurrence_rows.append(
                        {
                            "schedule_id": schedule.id,
                            "scheduled_for": instant,
                            "status": status,
                        }
                    )
            if occurrence_rows:
                inserted = await session.execute(
                    insert(DoseOccurrence)
                    .values(occurrence_rows)
                    .on_conflict_do_nothing(
                        index_elements=[DoseOccurrence.schedule_id, DoseOccurrence.scheduled_for]
                    )
                    .returning(DoseOccurrence.id)
                )
                changed += len(inserted.scalars().all())
            schedule.occurrences_generated_through = through

        has_log = select(MedicationLog.id).where(
            MedicationLog.dose_occurrence_id == DoseOccurrence.id
        ).correlate(DoseOccurrence).exists()
        due_result = await session.execute(
            update(DoseOccurrence)
            .where(
                DoseOccurrence.schedule_id == schedule.id,
                DoseOccurrence.status == DoseStatus.UPCOMING,
                DoseOccurrence.scheduled_for == now,
                ~has_log,
            )
            .values(status=DoseStatus.DUE)
            .execution_options(synchronize_session="fetch")
        )
        changed += due_result.rowcount or 0
        unreported_result = await session.execute(
            update(DoseOccurrence)
            .where(
                DoseOccurrence.schedule_id == schedule.id,
                DoseOccurrence.status.in_([DoseStatus.UPCOMING, DoseStatus.DUE]),
                DoseOccurrence.scheduled_for < now,
                ~has_log,
            )
            .values(status=DoseStatus.UNREPORTED)
            .execution_options(synchronize_session="fetch")
        )
        changed += unreported_result.rowcount or 0

        active_since = await session.scalar(
            select(func.max(ScheduleStatusChange.created_at)).where(
                ScheduleStatusChange.schedule_id == schedule.id,
                ScheduleStatusChange.status == ScheduleStatus.ACTIVE,
            )
        )
        if active_since is None or active_since < schedule.confirmed_at:
            active_since = schedule.confirmed_at
        if schedule.ends_at is None or now < schedule.ends_at:
            changed += await _deliver_due_reminders(session, schedule, now, active_since)

    if changed:
        await append_event(
            session,
            action="dose_occurrence.worker_processed",
            target_type="dose_occurrence_worker",
            outcome="success",
            request_id=f"dose-occurrence-worker:{uuid4()}",
            metadata={"result_count": changed},
        )
    return changed
