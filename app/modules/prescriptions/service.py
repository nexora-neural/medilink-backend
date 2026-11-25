"""Business rules and orchestration for prescriptions and OCR workflow."""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import Request
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.service import append_event
from app.modules.documents.models import Document
from app.modules.identity.auth import AuthenticationError
from app.modules.identity.models import User
from app.modules.identity.repository import get_roles
from app.modules.organizations.models import (
    Organization,
    OrganizationMembership,
)
from app.modules.prescriptions import repository
from app.modules.prescriptions.access import require_prescription_access
from app.modules.prescriptions.core_models import (
    Medicine,
    Prescription,
    PrescriptionItem,
    PrescriptionReviewStatus,
    PrescriptionStatus,
)
from app.modules.prescriptions.review_models import PrescriptionReview
from app.modules.prescriptions.schemas import (
    MedicineInput,
    MedicinesRead,
    MedicinesReadPage,
    PrescriptionDetail,
    PrescriptionDetailPage,
    PrescriptionInput,
    PrescriptionIssueContent,
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
        if medicine is None:
            raise AuthenticationError(
                status_code=409,
                detail={
                    "code": "MEDICINE_ALREADY_EXISTS",
                    "message": "A sample medicine with these details already exists.",
                    "field_errors": {},
                    "request_id": request.state.request_id,
                },
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

    try:
        prescription = await repository.lock_prescription(
            session,
            prescription_id,
        )

        if prescription is None:
            raise AuthenticationError(
                status_code=404,
                detail={
                    "code": "RESOURCE_NOT_FOUND",
                    "message": "Prescription was not found.",
                    "field_errors": {},
                    "request_id": request.state.request_id,
                },
            )

        source = getattr(prescription.source, "value", prescription.source)

        is_patient_owner = source == "patient" and prescription.patient_user_id == user.id
        is_doctor_owner = source == "doctor" and prescription.prescriber_user_id == user.id

        if not (is_patient_owner or is_doctor_owner):
            raise forbidden(request)

        authorized_source = await require_prescription_access(
            session,
            user=user,
            request=request,
            patient_id=prescription.patient_user_id,
            consultation_id=prescription.consultation_id,
            clinical_write=True,
        )

        if authorized_source != source:
            raise forbidden(request)

        # Recheck permission for a replacement consultation, if supplied.
        if payload.consultation_id != prescription.consultation_id:
            replacement_source = await require_prescription_access(
                session,
                user=user,
                request=request,
                patient_id=prescription.patient_user_id,
                consultation_id=payload.consultation_id,
                clinical_write=True,
            )
            if replacement_source != source:
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

        if prescription.status != PrescriptionStatus.DRAFT or prescription.issued_at is not None:
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
                    "field_errors": {"ends_on": ["Must be on or after starts_on."]},
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

        await validate_draft_references(
            session,
            request=request,
            patient_id=prescription.patient_user_id,
            payload=payload,
        )

        prescription.consultation_id = payload.consultation_id if source == "doctor" else None
        prescription.claimed_prescriber = (
            payload.claimed_prescriber if source == "patient" else None
        )
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

        # Retain review rows, but invalidate approval of the previous content.
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


async def build_prescription_detail(
    session: AsyncSession,
    prescription: Prescription,
) -> PrescriptionDetail:
    items = list(
        (
            await session.scalars(
                select(PrescriptionItem)
                .where(PrescriptionItem.prescription_id == prescription.id)
                .order_by(
                    PrescriptionItem.created_at,
                    PrescriptionItem.id,
                )
            )
        ).all()
    )

    reviews = list(
        (
            await session.scalars(
                select(PrescriptionReview)
                .where(PrescriptionReview.prescription_id == prescription.id)
                .order_by(
                    PrescriptionReview.reviewed_at,
                    PrescriptionReview.id,
                )
            )
        ).all()
    )

    return PrescriptionDetail(
        prescription=prescription,
        items=items,
        reviews=reviews,
    )


async def read_prescription(
    *,
    session: AsyncSession,
    user: User,
    request: Request,
    prescription_id: UUID,
) -> PrescriptionDetail:
    prescription = await repository.find_prescription(
        session,
        prescription_id,
    )

    if prescription is None:
        raise AuthenticationError(
            status_code=404,
            detail={
                "code": "RESOURCE_NOT_FOUND",
                "message": "Prescription not found.",
                "field_errors": {},
                "request_id": getattr(request.state, "request_id", ""),
            },
        )

    await require_prescription_access(
        session,
        user=user,
        request=request,
        patient_id=prescription.patient_user_id,
    )

    return await build_prescription_detail(session, prescription)


async def list_prescriptions(
    *,
    session: AsyncSession,
    user: User,
    request: Request,
    patient_id: UUID,
    page: int = 1,
    page_size: int = 20,
    status: str | None = None,
    source: str | None = None,
) -> PrescriptionDetailPage:
    await require_prescription_access(
        session,
        user=user,
        request=request,
        patient_id=patient_id,
    )

    prescriptions, total = await repository.list_patient_prescriptions(
        session,
        patient_id=patient_id,
        page=page,
        page_size=page_size,
        status=status,
        source=source,
    )

    details = []
    for prescription in prescriptions:
        details.append(await build_prescription_detail(session, prescription))

    return PrescriptionDetailPage(
        items=details,
        page=page,
        page_size=page_size,
        total=total,
    )


async def validate_draft_references(
    session: AsyncSession,
    *,
    request: Request,
    patient_id: UUID,
    payload: PrescriptionInput,
) -> None:
    if payload.original_document_id is not None:
        document = await session.scalar(
            select(Document)
            .where(
                Document.id == payload.original_document_id,
                Document.owner_user_id == patient_id,
                Document.status == "active",
                Document.document_type == "prescription",
            )
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )

        if document is None or (
            document.patient_user_id is not None and document.patient_user_id != patient_id
        ):
            raise AuthenticationError(
                status_code=422,
                detail={
                    "code": "VALIDATION_ERROR",
                    "message": "Please check the highlighted fields.",
                    "field_errors": {
                        "original_document_id": [
                            "Select an active prescription document owned by this patient."
                        ]
                    },
                    "request_id": getattr(request.state, "request_id", ""),
                },
            )

    medicine_ids = {item.medicine_id for item in payload.items if item.medicine_id is not None}

    if medicine_ids:
        existing_ids = set(
            (
                await session.scalars(
                    select(Medicine.id)
                    .where(Medicine.id.in_(medicine_ids))
                    .order_by(Medicine.id)
                    .with_for_update(read=True)
                )
            ).all()
        )

        if existing_ids != medicine_ids:
            raise AuthenticationError(
                status_code=422,
                detail={
                    "code": "VALIDATION_ERROR",
                    "message": "Please check the highlighted fields.",
                    "field_errors": {"items": ["One or more selected medicines are unavailable."]},
                    "request_id": getattr(request.state, "request_id", ""),
                },
            )


async def create_draft(
    *,
    session: AsyncSession,
    user: User,
    request: Request,
    patient_id: UUID,
    payload: PrescriptionInput,
) -> PrescriptionDetail:
    try:
        source = await require_prescription_access(
            session,
            user=user,
            request=request,
            patient_id=patient_id,
            consultation_id=payload.consultation_id,
            clinical_write=True,
        )

        await validate_draft_references(
            session,
            request=request,
            patient_id=patient_id,
            payload=payload,
        )

        prescription = await repository.create_prescription_draft(
            session,
            patient_id=patient_id,
            actor_id=user.id,
            source=source,
            payload=payload,
        )

        await append_event(
            session,
            actor_user_id=user.id,
            action="prescription.draft.created",
            target_type="prescriptions",
            target_id=prescription.id,
            outcome="success",
            request_id=getattr(request.state, "request_id", ""),
            metadata={
                "source": source,
                "new_version": prescription.version,
                "item_count": len(payload.items),
            },
        )

        result = await build_prescription_detail(
            session,
            prescription,
        )

        await session.commit()
        return result

    except Exception:
        await session.rollback()
        raise


async def delete_draft(
    *,
    session: AsyncSession,
    user: User,
    request: Request,
    prescription_id: UUID,
) -> None:
    request_id = getattr(request.state, "request_id", "")

    try:
        prescription = await repository.lock_prescription(
            session,
            prescription_id,
        )

        if prescription is None:
            raise AuthenticationError(
                status_code=404,
                detail={
                    "code": "RESOURCE_NOT_FOUND",
                    "message": "Prescription not found.",
                    "field_errors": {},
                    "request_id": request_id,
                },
            )

        source = getattr(prescription.source, "value", prescription.source)

        patient_owner = source == "patient" and prescription.patient_user_id == user.id
        doctor_owner = source == "doctor" and prescription.prescriber_user_id == user.id

        if not (patient_owner or doctor_owner):
            raise forbidden(request)

        authorized_source = await require_prescription_access(
            session,
            user=user,
            request=request,
            patient_id=prescription.patient_user_id,
            consultation_id=prescription.consultation_id,
            clinical_write=True,
        )

        if authorized_source != source:
            raise forbidden(request)

        if prescription.status != PrescriptionStatus.DRAFT or prescription.issued_at is not None:
            raise AuthenticationError(
                status_code=409,
                detail={
                    "code": "INVALID_STATE",
                    "message": "Only never-issued drafts can be deleted.",
                    "field_errors": {},
                    "request_id": request_id,
                },
            )

        deleted_id = prescription.id
        deleted_version = prescription.version

        try:
            await repository.delete_draft_rows(
                session,
                deleted_id,
            )
        except IntegrityError as exc:
            sqlstate = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
            if sqlstate != "23503":
                raise

            raise AuthenticationError(
                status_code=409,
                detail={
                    "code": "INVALID_STATE",
                    "message": ("This draft has linked records and cannot be deleted."),
                    "field_errors": {},
                    "request_id": request_id,
                },
            ) from None

        await append_event(
            session,
            actor_user_id=user.id,
            action="prescription.draft.deleted",
            target_type="prescriptions",
            target_id=deleted_id,
            outcome="success",
            request_id=request_id,
            metadata={
                "source": source,
                "previous_version": deleted_version,
            },
        )

        await session.commit()

    except Exception:
        await session.rollback()
        raise


async def issue_prescription(
    *,
    session: AsyncSession,
    user: User,
    request: Request,
    prescription_id: UUID,
) -> PrescriptionDetail:
    """Explicitly issue a complete draft owned by the verified doctor."""

    request_id = getattr(request.state, "request_id", "")

    def issue_error(status_code, code, message, field_errors=None):
        return AuthenticationError(
            status_code=status_code,
            detail={
                "code": code,
                "message": message,
                "field_errors": field_errors or {},
                "request_id": request_id,
            },
        )

    try:
        # Prevent simultaneous edits or duplicate issue transitions.
        prescription = await repository.lock_prescription(
            session,
            prescription_id,
        )

        if prescription is None:
            raise issue_error(
                404,
                "RESOURCE_NOT_FOUND",
                "Prescription not found.",
            )

        source = getattr(prescription.source, "value", prescription.source)

        if source != "doctor" or prescription.prescriber_user_id != user.id:
            raise forbidden(request)

        authorized_source = await require_prescription_access(
            session,
            user=user,
            request=request,
            patient_id=prescription.patient_user_id,
            consultation_id=prescription.consultation_id,
            clinical_write=True,
        )

        if authorized_source != "doctor":
            raise forbidden(request)

        # A retry must not issue twice or create duplicate audit events.
        if prescription.status == PrescriptionStatus.ACTIVE:
            if prescription.issued_at is None:
                raise issue_error(
                    409,
                    "INVALID_STATE",
                    "Prescription state is inconsistent.",
                )

            result = await build_prescription_detail(session, prescription)
            await session.commit()
            return result

        if prescription.status != PrescriptionStatus.DRAFT or prescription.issued_at is not None:
            raise issue_error(
                409,
                "INVALID_STATE",
                "Only never-issued drafts can be issued.",
            )

        detail = await build_prescription_detail(session, prescription)

        item_fields = {
            "medicine_id",
            "medicine_name_snapshot",
            "dose_text",
            "route",
            "frequency",
            "duration_days",
            "instructions",
            "quantity",
            "quantity_unit",
            "is_prn",
        }

        try:
            content = PrescriptionIssueContent.model_validate(
                {
                    "consultation_id": prescription.consultation_id,
                    "starts_on": prescription.starts_on,
                    "ends_on": prescription.ends_on,
                    "original_document_id": prescription.original_document_id,
                    "items": [item.model_dump(include=item_fields) for item in detail.items],
                }
            )
        except ValidationError as exc:
            field_errors = {}
            for error in exc.errors(
                include_input=False,
                include_context=False,
                include_url=False,
            ):
                field = ".".join(str(part) for part in error["loc"])
                field_errors.setdefault(field, []).append(error["msg"])

            raise issue_error(
                422,
                "VALIDATION_ERROR",
                "Complete the prescription details before issuing.",
                field_errors,
            ) from None

        await validate_draft_references(
            session,
            request=request,
            patient_id=prescription.patient_user_id,
            payload=content,
        )

        predecessor = None

        if prescription.previous_prescription_id is not None:
            if prescription.previous_prescription_id == prescription.id:
                raise issue_error(
                    409,
                    "INVALID_STATE",
                    "A prescription cannot replace itself.",
                )

            predecessor = await repository.lock_prescription(
                session,
                prescription.previous_prescription_id,
            )

            if predecessor is None or predecessor.patient_user_id != prescription.patient_user_id:
                raise issue_error(
                    409,
                    "INVALID_STATE",
                    "The linked prescription is unavailable.",
                )

            predecessor_source = getattr(predecessor.source, "value", predecessor.source)

            if predecessor_source == "doctor":
                if predecessor.prescriber_user_id != user.id:
                    raise forbidden(request)
            elif predecessor_source != "patient":
                raise forbidden(request)

            if (
                predecessor.status
                not in {
                    PrescriptionStatus.ACTIVE,
                    PrescriptionStatus.COMPLETED,
                }
                or predecessor.issued_at is None
            ):
                raise issue_error(
                    409,
                    "INVALID_STATE",
                    "The linked prescription cannot be replaced.",
                )

        previous_version = prescription.version
        now = datetime.now(UTC)

        if predecessor is not None:
            predecessor_status = getattr(predecessor.status, "value", predecessor.status)
            predecessor_version = predecessor.version

            predecessor.status = PrescriptionStatus.SUPERSEDED
            predecessor.updated_at = now
            predecessor.version += 1

            await repository.cancel_superseded_medication(
                session,
                prescription_id=predecessor.id,
                now=now,
            )

            await append_event(
                session,
                actor_user_id=user.id,
                action="prescription.superseded",
                target_type="prescriptions",
                target_id=predecessor.id,
                outcome="success",
                request_id=request_id,
                metadata={
                    "previous_status": predecessor_status,
                    "status": "superseded",
                    "previous_version": predecessor_version,
                    "new_version": predecessor.version,
                },
            )

        prescription.status = PrescriptionStatus.ACTIVE
        prescription.issued_at = now
        prescription.updated_at = now
        prescription.version += 1

        await session.flush()

        await append_event(
            session,
            actor_user_id=user.id,
            action="prescription.issued",
            target_type="prescriptions",
            target_id=prescription.id,
            outcome="success",
            request_id=request_id,
            metadata={
                "source": "doctor",
                "previous_status": "draft",
                "status": "active",
                "previous_version": previous_version,
                "new_version": prescription.version,
                "item_count": len(content.items),
                **({"supersedes_id": str(predecessor.id)} if predecessor is not None else {}),
            },
        )

        result = await build_prescription_detail(session, prescription)

        await session.commit()
        return result

    except Exception:
        await session.rollback()
        raise
