"""Business rules and orchestration for professional verification."""



from uuid import UUID, uuid4

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.models import AuditEvent
from app.modules.identity.auth import AuthenticationError
from app.modules.identity.models import User
from app.modules.organizations.models import Organization
from app.modules.profiles.models import ProfessionalProfile
from app.modules.verification import repository
from app.modules.verification.models import (
    VerificationApplication,
    VerificationApplicationDocument,
)
from app.modules.verification.schemas import (
    ProfessionalProfileInput,
    ProfessionalProfileRead,
    VerificationApplicationInput,
    VerificationApplicationList,
    VerificationApplicationRead,
)


def _error(
    request: Request,
    status: int,
    code: str,
    message: str,
) -> AuthenticationError:
    return AuthenticationError(
        status_code=status,
        detail={
            "code": code,
            "message": message,
            "field_errors": {},
            "request_id": getattr(request.state, "request_id", ""),
        },
    )


def _check_account(user: User, request: Request) -> None:
    # Applications are available to pending and active accounts.
    # They do not require an approved professional role.
    if user.status not in {"pending", "active"}:
        raise _error(
            request, 403, "ACCESS_DENIED",
            "Account is not eligible for verification.",
        )


def _audit(
    db: AsyncSession,
    request: Request,
    user_id: UUID,
    action: str,
    target_type: str,
    target_id: UUID,
    metadata: dict,
) -> None:
    db.add(
        AuditEvent(
            actor_user_id=user_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            outcome="success",
            request_id=request.state.request_id,
            metadata_=metadata,
        )
    )


async def update_professional_profile(
    db: AsyncSession,
    user: User,
    request: Request,
    payload: ProfessionalProfileInput,
) -> ProfessionalProfileRead:
    _check_account(user, request)

    try:
        # Serialize concurrent profile updates for this account.
        await db.scalar(
            select(User.id)
            .where(User.id == user.id)
            .with_for_update()
        )

        profile = await repository.get_professional_profile(
            db, user.id, lock=True
        )

        if profile is not None and profile.role != payload.role.value:
            raise _error(
                request, 409, "INVALID_STATE",
                "The professional role cannot be changed.",
            )

        if profile is not None and profile.verification_status == "approved":
            raise _error(
                request, 409, "INVALID_STATE",
                "An approved professional profile cannot be edited here.",
            )

        created = profile is None

        if profile is None:
            profile = ProfessionalProfile(
                user_id=user.id,
                role=payload.role.value,
                doctor_reference=(
                    uuid4().hex if payload.role.value == "doctor" else None
                ),
                verification_status="not_submitted",
            )
            db.add(profile)

        changed_fields = []

        for field in (
            "registration_number",
            "qualifications",
            "specialty",
        ):
            # PUT keeps the same role while updating supplied profile fields.
            if field not in payload.model_fields_set:
                continue

            value = getattr(payload, field)
            if getattr(profile, field) != value:
                setattr(profile, field, value)
                changed_fields.append(field)

        await db.flush()
        await db.refresh(profile)

        if created or changed_fields:
            _audit(
                db,
                request,
                user.id,
                "professional_profile.updated",
                "professional_profile",
                user.id,
                {
                    "created": created,
                    "changed_fields": changed_fields,
                },
            )

        response = ProfessionalProfileRead.model_validate(profile)
        await db.commit()
        return response

    except Exception:
        await db.rollback()
        raise


async def submit_application(
    db: AsyncSession,
    user: User,
    request: Request,
    payload: VerificationApplicationInput,
) -> VerificationApplicationRead:
    _check_account(user, request)

    try:
        # Lock the account to serialize submissions from this user.
        await db.scalar(
            select(User.id)
            .where(User.id == user.id)
            .with_for_update()
        )

        profile = await repository.get_professional_profile(
            db, user.id, lock=True
        )

        if profile is None:
            raise _error(
                request, 409, "INVALID_STATE",
                "Create a professional profile before applying.",
            )

        if profile.verification_status in {"approved", "suspended"}:
            raise _error(
                request, 409, "INVALID_STATE",
                "The professional profile is not eligible for submission.",
            )

        # Prevent duplicate live applications but retain older history.
        existing = await repository.get_user_applications(
            db, user.id
        )

        if any(
            application.status in {"pending", "not_submitted"}
            for application in existing
        ):
            raise _error(
                request, 409, "INVALID_STATE",
                "A verification application is already pending.",
            )

        if payload.organization_id is not None:
            organization = await db.get(
                Organization, payload.organization_id
            )
            if organization is None:
                raise _error(
                    request, 404, "RESOURCE_NOT_FOUND",
                    "Organization not found.",
                )

        # Verify evidence before inserting the application.
        for document_id in payload.document_ids:
            document = await repository.get_owned_evidence(
                db, document_id, user.id
            )

            if document is None:
                raise _error(
                    request, 404, "RESOURCE_NOT_FOUND",
                    "Verification evidence not found.",
                )

        registration_number = (
            payload.registration_number
            if "registration_number" in payload.model_fields_set
            else profile.registration_number
        )

        application = VerificationApplication(
            user_id=user.id,
            organization_id=payload.organization_id,
            role=profile.role,
            registration_number=registration_number,
            status="pending",
        )

        db.add(application)
        await db.flush()

        for document_id in payload.document_ids:
            db.add(
                VerificationApplicationDocument(
                    application_id=application.id,
                    document_id=document_id,
                    attached_by=user.id,
                )
            )

        # Submission does not create a role assignment or approval.
        # Retain the current profile status until the review workflow
        # performs a separate authorized status transition.
        if profile.verification_status in {
            "not_submitted",
            "rejected",
            "more_info_requested",
        }:
            profile.verification_status = "pending"

        _audit(
            db,
            request,
            user.id,
            "verification_application.submitted",
            "verification_application",
            application.id,
            {
                "role": profile.role,
                "evidence_count": len(payload.document_ids),
            },
        )

        await db.flush()
        await db.refresh(application)

        response = VerificationApplicationRead.model_validate(
            application
        ).model_copy(
            update={"document_ids": list(payload.document_ids)}
        )

        await db.commit()
        return response

    except Exception:
        await db.rollback()
        raise


async def list_own_applications(
    db: AsyncSession,
    user: User,
    request: Request,
) -> VerificationApplicationList:
    _check_account(user, request)

    applications = await repository.get_user_applications(
        db, user.id
    )

    items = []

    for application in applications:
        document_ids = await repository.get_application_documents(
            db, application.id
        )

        response = VerificationApplicationRead.model_validate(
            application
        ).model_copy(
            update={"document_ids": document_ids}
        )

        items.append(response)

    return VerificationApplicationList(items=items)
