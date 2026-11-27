"""Confirmed reminder schedules; clinical instructions are never changed here."""

from datetime import datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.service import append_event
from app.modules.consent.models import AccessGrant, GrantCategory
from app.modules.identity.authorization import (
    require_verified_professional,
)
from app.modules.identity.models import Role, User, UserRole
from app.modules.medication import repository
from app.modules.medication.enum import ScheduleStatus
from app.modules.medication.models import MedicationSchedule, ScheduleStatusChange
from app.modules.medication.schemas import ScheduleDetail, ScheduleDetailPage, ScheduleInput
from app.modules.organizations.models import Organization, OrganizationMembership
from app.modules.profiles.models import PatientProfile, ProfessionalProfile

# Accept only explicit daily fixed-time frequencies, never interval/weekly/taper
# instructions. Mapping validates count; it does not suggest or infer dose times.
DAILY_COUNTS = {
    "daily": 1,
    "once daily": 1,
    "once a day": 1,
    "1/day": 1,
    "od": 1,
    "twice daily": 2,
    "twice a day": 2,
    "2/day": 2,
    "bd": 2,
    "bid": 2,
    "three times daily": 3,
    "three times a day": 3,
    "3/day": 3,
    "tds": 3,
    "tid": 3,
    "four times daily": 4,
    "four times a day": 4,
    "4/day": 4,
    "qid": 4,
}


class MedicationScheduleError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message
        super().__init__(message)


def error(status, code, message):
    raise MedicationScheduleError(status, code, message)


def missing():
    error(404, "RESOURCE_NOT_FOUND", "Medication resource was not found.")


async def active_roles(session, actor_id):
    status = await session.scalar(
        select(User.status).where(User.id == actor_id).with_for_update(read=True)
    )
    if status != "active":
        error(403, "ACCOUNT_INACTIVE", "An active account is required.")
    return set(
        (
            await session.scalars(
                select(Role.code)
                .join(
                    UserRole,
                    UserRole.role_id == Role.id,
                )
                .where(UserRole.user_id == actor_id, UserRole.revoked_at.is_(None))
                .with_for_update(read=True, of=UserRole)
            )
        ).all()
    )


async def patient_actor(session, actor_id, patient_id):
    roles = await active_roles(session, actor_id)
    if actor_id != patient_id:
        missing()
    if "patient" not in roles:
        error(403, "ACCESS_DENIED", "A current Patient role is required.")
    if await session.get(PatientProfile, patient_id) is None:
        missing()


async def read_access(session, actor, patient_id):
    roles = await active_roles(session, actor.id)
    if actor.id == patient_id and "patient" in roles:
        if await session.get(PatientProfile, patient_id) is None:
            missing()
        return
    if "doctor" not in roles:
        missing()
    await require_verified_professional(session, actor, "doctor")
    profile = await session.scalar(
        select(ProfessionalProfile.user_id)
        .where(
            ProfessionalProfile.user_id == actor.id,
            ProfessionalProfile.role == "doctor",
            ProfessionalProfile.verification_status == "approved",
        )
        .with_for_update(read=True)
    )
    if profile is None:
        error(403, "VERIFICATION_REQUIRED", "Doctor verification is required.")
    grants = (
        await session.scalars(
            select(AccessGrant)
            .join(
                GrantCategory,
                GrantCategory.grant_id == AccessGrant.id,
            )
            .where(
                AccessGrant.patient_user_id == patient_id,
                AccessGrant.professional_user_id == actor.id,
                AccessGrant.status == "active",
                AccessGrant.revoked_at.is_(None),
                GrantCategory.category == "medication_logs",
            )
            .order_by(AccessGrant.id)
            .with_for_update(read=True, of=(AccessGrant, GrantCategory))
        )
    ).all()
    for grant in grants:
        if grant.organization_id is not None:
            membership = await session.scalar(
                select(OrganizationMembership.id)
                .join(
                    Organization,
                    Organization.id == OrganizationMembership.organization_id,
                )
                .where(
                    OrganizationMembership.user_id == actor.id,
                    OrganizationMembership.organization_id == grant.organization_id,
                    OrganizationMembership.role == "doctor",
                    OrganizationMembership.status == "approved",
                    Organization.status == "active",
                )
                .with_for_update(read=True, of=(Organization, OrganizationMembership))
            )
            if membership is None:
                continue
        now = await session.scalar(select(func.clock_timestamp()))
        if grant.starts_at <= now < grant.expires_at:
            patient_status = await session.scalar(
                select(User.status).where(User.id == patient_id).with_for_update(read=True)
            )
            if patient_status != "active":
                error(403, "ACCOUNT_INACTIVE", "Patient account is inactive.")
            return
    missing()


def validate_course(item, prescription, payload, now):
    if prescription.status != "active" or prescription.issued_at is None:
        error(409, "INVALID_STATE", "An active confirmed or issued prescription is required.")
    frequency = " ".join(item.frequency.lower().split())
    count = DAILY_COUNTS.get(frequency)
    if item.is_prn or count is None:
        error(422, "VALIDATION_ERROR", "Only daily fixed-time, non-PRN instructions are supported.")
    if len(payload.times) != count:
        error(422, "VALIDATION_ERROR", "Reminder count must match the prescribed daily frequency.")
    zone = ZoneInfo(payload.timezone)
    course_start = prescription.starts_on or prescription.issued_at.astimezone(zone).date()
    lower = datetime.combine(course_start, time.min, zone)
    upper_dates = []
    if prescription.ends_on is not None:
        upper_dates.append(prescription.ends_on + timedelta(days=1))
    if item.duration_days is not None:
        upper_dates.append(course_start + timedelta(days=item.duration_days))
    upper = datetime.combine(min(upper_dates), time.min, zone) if upper_dates else None
    if payload.starts_at < lower or (upper is not None and payload.starts_at >= upper):
        error(422, "VALIDATION_ERROR", "Schedule start is outside the prescription course.")
    if upper is not None and (payload.ends_at is None or payload.ends_at > upper):
        error(422, "VALIDATION_ERROR", "Confirm an end within the prescription course.")
    if payload.ends_at is not None and payload.ends_at <= now:
        error(422, "VALIDATION_ERROR", "The confirmed reminder course has already ended.")


async def detail(session, schedule, source):
    return ScheduleDetail(
        schedule=schedule,
        times=await repository.get_schedule_times(session, schedule.id),
        prescription_source=source,
    )


async def list_schedules(session, actor, patient_id, query, request_id):
    try:
        await read_access(session, actor, patient_id)
        rows, total = await repository.list_schedules(session, patient_id, query)
        times = await repository.times_for_schedules(session, [row.id for row, _ in rows])
        result = ScheduleDetailPage(
            items=[
                ScheduleDetail(
                    schedule=row, times=times.get(row.id, []), prescription_source=source
                )
                for row, source in rows
            ],
            page=query.page,
            page_size=query.page_size,
            total=total,
        )
        await append_event(
            session,
            action="medication_schedule.listed",
            target_type="patient_record",
            target_id=patient_id,
            actor_user_id=actor.id,
            outcome="success",
            request_id=request_id,
            metadata={"result_count": len(rows)},
        )
        await session.commit()
        return result
    except Exception:
        await session.rollback()
        raise


async def create_schedule(session, actor, patient_id, payload, request_id):
    try:
        await patient_actor(session, actor.id, patient_id)
        context = await repository.lock_item_context(
            session, payload.prescription_item_id, patient_id
        )
        if context is None:
            missing()
        item, prescription = context
        now = await session.scalar(select(func.clock_timestamp()))
        validate_course(item, prescription, payload, now)
        if await repository.overlapping_schedule(
            session, item.id, payload.starts_at, payload.ends_at
        ):
            error(
                409, "INVALID_STATE", "A schedule already covers this prescription item and period."
            )
        row = await repository.create_schedule(
            session,
            prescription_item_id=item.id,
            patient_user_id=patient_id,
            timezone=payload.timezone,
            starts_at=payload.starts_at,
            ends_at=payload.ends_at,
            confirmed_at=now,
            times=[time.fromisoformat(v) for v in payload.times],
        )
        result = await detail(session, row, prescription.source)
        await append_event(
            session,
            action="medication_schedule.confirmed",
            target_type="medication_schedule",
            target_id=row.id,
            actor_user_id=actor.id,
            outcome="success",
            request_id=request_id,
            metadata={"source": prescription.source},
        )
        await session.commit()
        return result
    except Exception:
        await session.rollback()
        raise


async def owned_context(session, schedule_id, actor_id):
    await patient_actor(session, actor_id, actor_id)
    # Read ownership without locking, then acquire parent/item locks consistently.
    existing = await session.get(MedicationSchedule, schedule_id)
    if existing is None or existing.patient_user_id != actor_id:
        missing()
    context = await repository.lock_item_context(session, existing.prescription_item_id, actor_id)
    if context is None:
        missing()
    schedule = await repository.lock_schedule(session, schedule_id)
    return schedule, context[0], context[1]


def current_version(schedule, expected):
    if schedule.version != expected:
        error(409, "STALE_VERSION", "The schedule has changed. Refresh and try again.")


async def replace_schedule(
    session: AsyncSession,
    *,
    schedule_id: UUID,
    actor_user_id: UUID,
    expected_version: int,
    payload: ScheduleInput,
    request_id: str,
):
    try:
        old, item, prescription = await owned_context(session, schedule_id, actor_user_id)
        current_version(old, expected_version)
        if old.status != ScheduleStatus.ACTIVE:
            error(409, "INVALID_STATE", "Only active schedules can be replaced.")
        if payload.prescription_item_id != old.prescription_item_id:
            error(422, "VALIDATION_ERROR", "A replacement must use the same prescription item.")
        now = await session.scalar(select(func.clock_timestamp()))
        validate_course(item, prescription, payload, now)
        if await repository.overlapping_schedule(
            session, item.id, payload.starts_at, payload.ends_at, exclude_id=old.id
        ):
            error(409, "INVALID_STATE", "Another schedule covers this item and period.")
        await repository.cancel_future_occurrences(session, old.id, now)
        await repository.cancel_schedule(session, old)
        new = await repository.create_schedule(
            session,
            prescription_item_id=item.id,
            patient_user_id=actor_user_id,
            timezone=payload.timezone,
            starts_at=payload.starts_at,
            ends_at=payload.ends_at,
            confirmed_at=now,
            times=[time.fromisoformat(v) for v in payload.times],
            previous_schedule_id=old.id,
        )
        await append_event(
            session,
            action="medication_schedule.replaced",
            target_type="medication_schedule",
            target_id=new.id,
            outcome="success",
            request_id=request_id,
            actor_user_id=actor_user_id,
            metadata={
                "previous_schedule_id": str(old.id),
                "previous_version": expected_version,
                "new_version": new.version,
            },
        )
        result = await detail(session, new, prescription.source)
        await session.commit()
        return result
    except Exception:
        await session.rollback()
        raise


async def change_status(session, actor, schedule_id, expected_version, payload, request_id):
    try:
        row, item, prescription = await owned_context(session, schedule_id, actor.id)
        current_version(row, expected_version)
        if row.status == payload.status:
            result = await detail(session, row, prescription.source)
            await session.commit()
            return result
        if row.status in {ScheduleStatus.CANCELLED, ScheduleStatus.COMPLETED}:
            error(409, "INVALID_STATE", "Ended schedules cannot be reactivated or changed.")
        now = await session.scalar(select(func.clock_timestamp()))
        if payload.status == ScheduleStatus.ACTIVE:
            times = await repository.get_schedule_times(session, row.id)
            # Same context and bounds; resume does not infer new dates/times.
            validate_course(
                item,
                prescription,
                ScheduleInput(
                    prescription_item_id=item.id,
                    timezone=row.timezone,
                    starts_at=row.starts_at,
                    ends_at=row.ends_at,
                    times=[v.time_of_day.isoformat() for v in times],
                    confirmed=True,
                ),
                now,
            )
            if await repository.overlapping_schedule(
                session, item.id, row.starts_at, row.ends_at, exclude_id=row.id
            ):
                error(409, "INVALID_STATE", "Another schedule covers this item and period.")
        previous = row.status
        if payload.status in {ScheduleStatus.CANCELLED, ScheduleStatus.COMPLETED}:
            await repository.cancel_future_occurrences(session, row.id, now)
        row.status = payload.status
        row.version += 1
        session.add(
            ScheduleStatusChange(
                schedule_id=row.id,
                recorded_by=actor.id,
                previous_status=previous,
                status=row.status,
                reason=payload.reason,
                version=row.version,
            )
        )
        await session.flush()
        await append_event(
            session,
            action="medication_schedule.status_changed",
            target_type="medication_schedule",
            target_id=row.id,
            actor_user_id=actor.id,
            outcome="success",
            request_id=request_id,
            metadata={
                "previous_status": previous,
                "status": row.status,
                "previous_version": expected_version,
                "new_version": row.version,
            },
        )
        result = await detail(session, row, prescription.source)
        await session.commit()
        return result
    except Exception:
        await session.rollback()
        raise
