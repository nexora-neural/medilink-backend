"""Authorized prescription versions, retained lifecycle actions and history."""

from datetime import datetime
from uuid import UUID

from fastapi import Request
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.service import append_event
from app.modules.identity.auth import AuthenticationError
from app.modules.identity.models import User
from app.modules.prescriptions import repository, service
from app.modules.prescriptions.access import require_prescription_access
from app.modules.prescriptions.core_models import Prescription
from app.modules.prescriptions.lifecycle_models import PrescriptionLifecycleEvent
from app.modules.prescriptions.models import PrescriptionSharesModel
from app.modules.prescriptions.schemas import PrescriptionDetailPage, PrescriptionVersionInput

STATES = {"cancel": "cancelled", "discontinue": "discontinued", "complete": "completed"}


def fail(
    request, status=409, code="INVALID_STATE", message="This prescription action is unavailable."
):
    raise service.confirmation_error(request, status, code, message)


async def authorize(db, actor, request, rx, *, write=False, consultation_id=None):
    try:
        return await require_prescription_access(
            db,
            user=actor,
            request=request,
            patient_id=rx.patient_user_id,
            consultation_id=consultation_id,
            clinical_write=write,
        )
    except AuthenticationError as exc:
        if exc.status_code == 403 and actor.id not in (rx.patient_user_id, rx.prescriber_user_id):
            fail(request, 404, "RESOURCE_NOT_FOUND", "Prescription not found.")
        raise


def check_version(request, rx, expected):
    if expected is not None and rx.version != expected:
        fail(request, 409, "STALE_VERSION", "The prescription has changed. Refresh and try again.")


async def revoke_shares(db, prescription_id, now):
    await db.execute(
        update(PrescriptionSharesModel)
        .where(
            PrescriptionSharesModel.prescription_id == prescription_id,
            PrescriptionSharesModel.status.in_(["created", "claimed", "confirmed"]),
        )
        .values(status="revoked", revoked_at=now)
        .execution_options(synchronize_session=False)
    )


async def record(db, rx, actor, action, reason, previous_status, previous_version, related_id=None):
    db.add(
        PrescriptionLifecycleEvent(
            prescription_id=rx.id,
            related_prescription_id=related_id,
            actor_user_id=actor.id,
            action=action,
            reason=reason,
            previous_status=previous_status,
            status=str(rx.status),
            previous_version=previous_version,
            new_version=rx.version,
        )
    )
    await db.flush()


async def supersede(db, predecessor, successor, actor, request, now):
    """Activate only a separate successor; callers hold both prescription locks."""
    previous_status, previous_version = str(predecessor.status), predecessor.version
    predecessor.status = "superseded"
    predecessor.updated_at = now
    predecessor.version += 1
    reason = (
        getattr(successor, "status_reason", None) or "Replaced by a separately issued prescription."
    )
    predecessor.status_reason = reason
    await repository.cancel_superseded_medication(
        db, prescription_id=predecessor.id, now=now, refresh_clock=True
    )
    await revoke_shares(db, predecessor.id, now)
    source = await require_prescription_access(
        db,
        user=actor,
        request=request,
        patient_id=successor.patient_user_id,
        consultation_id=successor.consultation_id,
        clinical_write=True,
    )
    if source != successor.source:
        raise service.forbidden(request)
    await record(
        db,
        predecessor,
        actor,
        "superseded",
        reason,
        previous_status,
        previous_version,
        successor.id,
    )
    await append_event(
        db,
        actor_user_id=actor.id,
        action="prescription.superseded",
        target_type="prescriptions",
        target_id=predecessor.id,
        outcome="success",
        request_id=request.state.request_id,
        metadata={
            "previous_status": previous_status,
            "status": "superseded",
            "previous_version": previous_version,
            "new_version": predecessor.version,
        },
    )


async def patient_replacement(db, rx, actor, request, now):
    if rx.previous_prescription_id is None:
        return
    old = await repository.lock_prescription(db, rx.previous_prescription_id)
    if (
        old is None
        or old.id == rx.id
        or old.patient_user_id != rx.patient_user_id
        or old.source != "patient"
        or old.patient_user_id != actor.id
        or old.status not in {"active", "completed"}
        or old.issued_at is None
    ):
        fail(request)
    await supersede(db, old, rx, actor, request, now)


async def create_version(
    *,
    session: AsyncSession,
    user: User,
    request: Request,
    prescription_id: UUID,
    payload: PrescriptionVersionInput,
    action: str,
    expected_version=None,
):
    try:
        rx = await repository.lock_prescription(session, prescription_id)
        if rx is None:
            fail(request, 404, "RESOURCE_NOT_FOUND", "Prescription not found.")
        source = await authorize(
            session, user, request, rx, write=True, consultation_id=payload.consultation_id
        )
        # A patient can only version their personal record. Doctors can change
        # their own orders, or explicitly reissue patient evidence separately.
        if source == "patient":
            if rx.source != "patient":
                raise service.forbidden(request)
        elif not (
            rx.source == "doctor"
            and rx.prescriber_user_id == user.id
            or rx.source == "patient"
            and action == "reissue"
        ):
            raise service.forbidden(request)
        check_version(request, rx, expected_version)
        if rx.status not in {"active", "completed"} or rx.issued_at is None:
            fail(request)
        pending = await session.scalar(
            select(Prescription.id)
            .where(Prescription.previous_prescription_id == rx.id, Prescription.status == "draft")
            .limit(1)
        )
        if pending is not None:
            fail(request, message="A linked draft already exists. Review that draft first.")
        await service.validate_draft_references(
            session, request=request, patient_id=rx.patient_user_id, payload=payload
        )
        await authorize(
            session, user, request, rx, write=True, consultation_id=payload.consultation_id
        )
        child = await repository.create_prescription_draft(
            session, patient_id=rx.patient_user_id, actor_id=user.id, source=source, payload=payload
        )
        child.previous_prescription_id = rx.id
        child.status_reason = payload.reason
        await record(session, child, user, action, payload.reason, None, 0, rx.id)
        await append_event(
            session,
            actor_user_id=user.id,
            action=f"prescription.{action}.created",
            target_type="prescriptions",
            target_id=child.id,
            outcome="success",
            request_id=request.state.request_id,
            metadata={
                "source": source,
                "supersedes_id": str(rx.id),
                "new_version": 1,
                "item_count": len(payload.items),
            },
        )
        result = await service.build_prescription_detail(session, child)
        await session.commit()
        return result
    except Exception:
        await session.rollback()
        raise


async def change_status(
    *,
    session: AsyncSession,
    user: User,
    request: Request,
    prescription_id: UUID,
    reason: str,
    action: str,
    expected_version=None,
):
    try:
        rx = await repository.lock_prescription(session, prescription_id)
        if rx is None:
            fail(request, 404, "RESOURCE_NOT_FOUND", "Prescription not found.")
        # A patient reads their own doctor order, but cannot terminate it.
        consultation = rx.consultation_id if rx.source == "doctor" else None
        source = await authorize(
            session, user, request, rx, write=True, consultation_id=consultation
        )
        if source != rx.source or (source == "doctor" and rx.prescriber_user_id != user.id):
            raise service.forbidden(request)
        check_version(request, rx, expected_version)
        target = STATES[action]
        if rx.status == target:
            latest = await session.scalar(
                select(PrescriptionLifecycleEvent)
                .where(PrescriptionLifecycleEvent.prescription_id == rx.id)
                .order_by(
                    PrescriptionLifecycleEvent.created_at.desc(),
                    PrescriptionLifecycleEvent.id.desc(),
                )
                .limit(1)
            )
            if (
                latest
                and latest.action == action
                and latest.reason == reason
                and latest.actor_user_id == user.id
            ):
                result = await service.build_prescription_detail(session, rx)
                await session.commit()
                return result
            fail(request)
        if rx.status != "active" or rx.issued_at is None:
            fail(request)
        now: datetime = await session.scalar(select(func.clock_timestamp()))
        previous_status, previous_version = str(rx.status), rx.version
        rx.status, rx.status_reason, rx.updated_at = target, reason, now
        rx.version += 1
        await repository.cancel_superseded_medication(
            session, prescription_id=rx.id, now=now, refresh_clock=True
        )
        await revoke_shares(session, rx.id, now)
        await authorize(session, user, request, rx, write=True, consultation_id=consultation)
        await record(session, rx, user, action, reason, previous_status, previous_version)
        await append_event(
            session,
            actor_user_id=user.id,
            action=f"prescription.{action}",
            target_type="prescriptions",
            target_id=rx.id,
            outcome="success",
            request_id=request.state.request_id,
            metadata={
                "source": str(rx.source),
                "previous_status": previous_status,
                "status": target,
                "previous_version": previous_version,
                "new_version": rx.version,
            },
        )
        result = await service.build_prescription_detail(session, rx)
        await session.commit()
        return result
    except Exception:
        await session.rollback()
        raise


async def history(
    *,
    session: AsyncSession,
    user: User,
    request: Request,
    prescription_id: UUID,
    page=1,
    page_size=20,
):
    rx = await repository.find_prescription(session, prescription_id)
    if rx is None:
        fail(request, 404, "RESOURCE_NOT_FOUND", "Prescription not found.")
    await authorize(session, user, request, rx)
    # UNION, not UNION ALL, terminates even malformed legacy cycles. Patient
    # predicates on every recursive edge prevent cross-patient history leaks.
    ancestors = (
        select(Prescription.id, Prescription.previous_prescription_id)
        .where(Prescription.id == rx.id, Prescription.patient_user_id == rx.patient_user_id)
        .cte("rx_ancestors", recursive=True)
    )
    ancestors = ancestors.union(
        select(Prescription.id, Prescription.previous_prescription_id)
        .join(ancestors, Prescription.id == ancestors.c.previous_prescription_id)
        .where(Prescription.patient_user_id == rx.patient_user_id)
    )
    family = select(ancestors.c.id).cte("rx_family", recursive=True)
    family = family.union(
        select(Prescription.id)
        .join(family, Prescription.previous_prescription_id == family.c.id)
        .where(Prescription.patient_user_id == rx.patient_user_id)
    )
    query = select(Prescription).where(Prescription.id.in_(select(family.c.id)))
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    rows = (
        await session.scalars(
            query.order_by(Prescription.created_at.desc(), Prescription.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    details = [await service.build_prescription_detail(session, row) for row in rows]
    return PrescriptionDetailPage(items=details, page=page, page_size=page_size, total=total)
