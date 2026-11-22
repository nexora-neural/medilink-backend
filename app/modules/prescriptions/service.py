"""Business rules and orchestration for prescriptions and OCR workflow."""

from datetime import UTC, datetime

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.service import append_event
from app.modules.identity.auth import AuthenticationError
from app.modules.identity.models import User
from app.modules.identity.repository import get_roles
from app.modules.organizations.models import (
    Organization,
    OrganizationMembership,
)
from app.modules.prescriptions import repository
from app.modules.prescriptions.core_models import (
    PrescriptionReviewStatus,
    PrescriptionStatus,
)
from app.modules.prescriptions.review_models import PrescriptionReview
from app.modules.prescriptions.schemas import (
    MedicineInput,
    MedicinesRead,
    MedicinesReadPage,
    PrescriptionDetail,
    PrescriptionInput,
)
from app.modules.profiles.models import ProfessionalProfile


def forbidden(request: Request) -> AuthenticationError:
    return AuthenticationError(
        status_code=403,
        detail={
            "code": "ACCESS_DENIED",
            "message": "Permission denied.",
            "field_errors": {},
            "request_id": request.state.request_id,
        },
    )


async def require_medicine_reader(
    session: AsyncSession,
    user: User,
    request: Request,
) -> None:
    """Allow active patients and verified active doctors."""

    if user.status != "active":
        raise forbidden(request)

    roles = await get_roles(session, user.id)

    if "patient" in roles:
        return

    if "doctor" not in roles:
        raise forbidden(request)

    professional = await session.get(ProfessionalProfile, user.id)

    if (
        professional is None
        or professional.role != "doctor"
        or professional.verification_status != "approved"
    ):
        raise forbidden(request)

    statement = (
        select(OrganizationMembership.id)
        .join(
            Organization,
            Organization.id == OrganizationMembership.organization_id,
        )
        .where(
            OrganizationMembership.user_id == user.id,
            OrganizationMembership.role == "doctor",
            OrganizationMembership.status == "approved",
            Organization.status == "active",
        )
        .limit(1)
    )

    membership_id = await session.scalar(statement)

    if membership_id is None:
        raise forbidden(request)


async def require_medicine_admin(
    session: AsyncSession,
    user: User,
    request: Request,
) -> None:
    """Check privileged catalogue management permission."""

    if user.status != "active":
        raise forbidden(request)

    roles = await get_roles(session, user.id)

    # Super Admin is the platform's privileged managing role.
    if "super_admin" in roles:
        return

    # A regular Admin requires an explicit delegated permission.
    # The existing repository does not yet implement the
    # delegated-permission policy/check.
    #
    # Deny regular Admin writes until that shared check exists.
    # Never grant catalogue management merely from a role
    # selected during public onboarding.
    raise forbidden(request)


async def _read_medicine_page(
    session: AsyncSession,
    q: str | None,
    page: int,
    page_size: int,
) -> MedicinesReadPage:
    medicines, total = await repository.get_medicines(
        session=session,
        q=q,
        page=page,
        page_size=page_size,
    )
    return MedicinesReadPage(
        items=[MedicinesRead.model_validate(medicine) for medicine in medicines],
        page=page,
        page_size=page_size,
        total=total,
    )


async def list_medicines(
    session: AsyncSession,
    user: User,
    request: Request,
    q: str | None,
    page: int,
    page_size: int,
) -> MedicinesReadPage:
    await require_medicine_reader(session=session, user=user, request=request)
    return await _read_medicine_page(session, q, page, page_size)


async def list_admin_medicines(
    session: AsyncSession,
    user: User,
    request: Request,
    page: int,
    page_size: int,
) -> MedicinesReadPage:
    await require_medicine_admin(session=session, user=user, request=request)
    return await _read_medicine_page(session, None, page, page_size)


async def add_medicine(
    session: AsyncSession,
    user: User,
    request: Request,
    payload: MedicineInput,
) -> MedicinesRead:
    """Create a medicine and append its audit event atomically."""

    await require_medicine_admin(
        session=session,
        user=user,
        request=request,
    )

    try:
        medicine = await repository.create_medicine(
            session=session,
            payload=payload,
        )

        await append_event(
            session,
            actor_user_id=user.id,
            action="medicine.created",
            target_type="medicines",
            target_id=medicine.id,
            outcome="success",
            request_id=request.state.request_id,
            metadata={"is_sample": True},
        )

        result = MedicinesRead.model_validate(medicine)

        await session.commit()
        return result

    except Exception:
        await session.rollback()
        raise


async def replace_prescription_draft(
    session: AsyncSession,
    user: User,
    request: Request,
    prescription_id,
    expected_version: int,
    payload: PrescriptionInput,
) -> PrescriptionDetail:
    """Replace a draft prescription using an atomic version check."""

    await require_medicine_reader(session=session, user=user, request=request)

    try:
        prescription = await repository.lock_prescription(
            session,
            prescription_id,
        )

        if prescription is None:
            raise AuthenticationError(
                status_code=404,
                detail={
                    "code": "NOT_FOUND",
                    "message": "Prescription was not found.",
                    "field_errors": {},
                    "request_id": request.state.request_id,
                },
            )

        roles = await get_roles(session, user.id)

        is_patient_owner = (
            "patient" in roles
            and prescription.patient_user_id == user.id
        )
        is_doctor_owner = (
            "doctor" in roles
            and prescription.prescriber_user_id == user.id
        )

        if not (is_patient_owner or is_doctor_owner):
            raise forbidden(request)

        if prescription.version != expected_version:
            raise AuthenticationError(
                status_code=409,
                detail={
                    "code": "STALE_VERSION",
                    "message": "The prescription has changed. Refresh and try again.",
                    "field_errors": {},
                    "request_id": request.state.request_id,
                },
            )

        if prescription.status != PrescriptionStatus.DRAFT:
            raise AuthenticationError(
                status_code=409,
                detail={
                    "code": "INVALID_STATE",
                    "message": "Only draft prescriptions can be edited.",
                    "field_errors": {},
                    "request_id": request.state.request_id,
                },
            )

        if (
            payload.starts_on is not None
            and payload.ends_on is not None
            and payload.ends_on < payload.starts_on
        ):
            raise AuthenticationError(
                status_code=422,
                detail={
                    "code": "VALIDATION_ERROR",
                    "message": "ends_on must be on or after starts_on.",
                    "field_errors": {
                        "ends_on": ["Must be on or after starts_on."]
                    },
                    "request_id": request.state.request_id,
                },
            )

        if await repository.prescription_has_scheduled_items(
            session,
            prescription.id,
        ):
            raise AuthenticationError(
                status_code=409,
                detail={
                    "code": "INVALID_STATE",
                    "message": "A prescription with scheduled medication items cannot be edited.",
                    "field_errors": {},
                    "request_id": request.state.request_id,
                },
            )

        prescription.consultation_id = payload.consultation_id
        prescription.claimed_prescriber = payload.claimed_prescriber
        prescription.starts_on = payload.starts_on
        prescription.ends_on = payload.ends_on
        prescription.original_document_id = payload.original_document_id
        prescription.version += 1
        prescription.updated_at = datetime.now(UTC)

        await repository.delete_prescription_items(
            session,
            prescription.id,
        )
        items = await repository.create_prescription_items(
            session,
            prescription.id,
            payload.items,
        )

        # Keep review evidence, but require a fresh review of edited content.
        reviews_result = await session.scalars(
            select(PrescriptionReview)
            .where(PrescriptionReview.prescription_id == prescription.id)
            .order_by(PrescriptionReview.reviewed_at)
        )
        reviews = list(reviews_result.all())

        if reviews:
            prescription.review_status = PrescriptionReviewStatus.NOT_REVIEWED
            prescription.reviewed_by = None
            prescription.reviewed_at = None

        await append_event(
            session,
            actor_user_id=user.id,
            action="prescription.draft.updated",
            target_type="prescriptions",
            target_id=prescription.id,
            outcome="success",
            request_id=request.state.request_id,
            metadata={
                "previous_version": expected_version,
                "new_version": prescription.version,
                "item_count": len(items),
            },
        )

        result = PrescriptionDetail(
            prescription=prescription,
            items=items,
            reviews=reviews,
        )

        await session.commit()
        return result

    except Exception:
        await session.rollback()
        raise
