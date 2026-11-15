"""Business rules and orchestration for professional verification."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.service import append_event
from app.modules.identity.auth import AuthenticationError
from app.modules.identity.models import Role, User, UserRole
from app.modules.organizations.models import Organization, OrganizationMembership
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
    VerificationApplicationRead,
    VerificationApplicationsReadPage,
)

PROFESSIONAL_ROLES = ("doctor", "pharmacist", "lab_staff")


def _error(request: Request, status: int, code: str, message: str) -> AuthenticationError:
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
    # Pending applicants can apply; they do NOT receive clinical permissions here.
    if user.status not in {"pending", "active"}:
        raise _error(request, 403, "ACCESS_DENIED", "Account is not eligible for verification.")


async def _audit(
    db: AsyncSession,
    request: Request,
    user_id: UUID,
    action: str,
    target_type: str,
    target_id: UUID,
    metadata: dict,
) -> None:
    await append_event(
        db,
        actor_user_id=user_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        outcome="success",
        request_id=getattr(request.state, "request_id", ""),
        metadata=metadata,
    )


async def _ensure_role_consistency(
    db: AsyncSession,
    user: User,
    request: Request,
    requested_role: str,
) -> None:
    # An applicant with no assigned professional role is allowed to apply.
    # If roles already exist, they must agree with the requested profile role.
    assigned = await repository.get_assigned_professional_roles(db, user.id)
    if assigned and assigned != {requested_role}:
        raise _error(
            request,
            409,
            "INVALID_STATE",
            "Assigned professional role does not match the requested role.",
        )


async def _revoke_professional_access(db: AsyncSession, user_id: UUID) -> None:
    """Revoke derived professional access atomically before re-verification."""
    now = datetime.now(UTC)
    await db.execute(
        update(UserRole)
        .where(
            UserRole.user_id == user_id,
            UserRole.revoked_at.is_(None),
            UserRole.role_id.in_(select(Role.id).where(Role.code.in_(PROFESSIONAL_ROLES))),
        )
        .values(revoked_at=now)
    )
    await db.execute(
        update(OrganizationMembership)
        .where(
            OrganizationMembership.user_id == user_id,
            OrganizationMembership.role.in_(PROFESSIONAL_ROLES),
            OrganizationMembership.status == "approved",
        )
        .values(status="pending", verified_at=None)
    )


async def update_professional_profile(
    db: AsyncSession,
    user: User,
    request: Request,
    payload: ProfessionalProfileInput,
) -> ProfessionalProfileRead:
    _check_account(user, request)
    try:
        # Lock the account so profile updates and submissions cannot race.
        await db.scalar(select(User.id).where(User.id == user.id).with_for_update())
        profile = await repository.get_professional_profile(db, user.id, lock=True)
        existing = await repository.get_user_applications(db, user.id)
        role_changed = profile is not None and profile.role != payload.role.value
        registration_changed = (
            profile is not None
            and "registration_number" in payload.model_fields_set
            and profile.registration_number != payload.registration_number
        )
        if role_changed and any(a.status in {"pending", "not_submitted"} for a in existing):
            raise _error(
                request,
                409,
                "INVALID_STATE",
                "A pending application must be resolved before changing roles.",
            )

        # An approved role/registration change invalidates verified access.
        reverify = (
            profile is not None
            and profile.verification_status == "approved"
            and (role_changed or registration_changed)
        )
        if reverify:
            await _revoke_professional_access(db, user.id)
        else:
            await _ensure_role_consistency(db, user, request, payload.role.value)

        created = profile is None
        if created:
            profile = ProfessionalProfile(
                user_id=user.id,
                role=payload.role.value,
                doctor_reference=uuid4().hex if payload.role.value == "doctor" else None,
                verification_status="not_submitted",
            )
            db.add(profile)

        changed_fields: list[str] = []
        if role_changed:
            profile.role = payload.role.value
            profile.doctor_reference = uuid4().hex if payload.role.value == "doctor" else None
            changed_fields.append("role")

        for field in ("registration_number", "qualifications", "specialty"):
            if field not in payload.model_fields_set:
                continue
            value = getattr(payload, field)
            if getattr(profile, field) != value:
                setattr(profile, field, value)
                changed_fields.append(field)

        if reverify:
            profile.verification_status = "not_submitted"
            changed_fields.append("verification_status")
        elif role_changed and profile.verification_status != "not_submitted":
            profile.verification_status = "not_submitted"
            changed_fields.append("verification_status")

        await db.flush()
        await db.refresh(profile)
        if created or changed_fields:
            await _audit(
                db,
                request,
                user.id,
                "professional_profile.updated",
                "professional_profile",
                user.id,
                {
                    "created": created,
                    "changed_fields": changed_fields,
                    "reverification_required": reverify,
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
        await db.scalar(select(User.id).where(User.id == user.id).with_for_update())
        profile = await repository.get_professional_profile(db, user.id, lock=True)
        if profile is None:
            raise _error(
                request, 409, "INVALID_STATE", "Create a professional profile before applying."
            )
        if profile.role != payload.role.value:
            raise _error(
                request,
                409,
                "INVALID_STATE",
                "Application role must match the professional profile.",
            )
        await _ensure_role_consistency(db, user, request, payload.role.value)
        if profile.verification_status in {"approved", "suspended"}:
            raise _error(
                request,
                409,
                "INVALID_STATE",
                "The professional profile is not eligible for submission.",
            )

        existing = await repository.get_user_applications(db, user.id)
        if any(a.status in {"pending", "not_submitted"} for a in existing):
            raise _error(
                request, 409, "INVALID_STATE", "A verification application is already pending."
            )
        if payload.organization_id is not None:
            organization = await db.get(Organization, payload.organization_id)
            if organization is None:
                raise _error(request, 404, "RESOURCE_NOT_FOUND", "Organization not found.")

        for document_id in payload.evidence_document_ids:
            evidence = await repository.get_owned_evidence(db, document_id, user.id)
            if evidence is None:
                raise _error(request, 404, "RESOURCE_NOT_FOUND", "Verification evidence not found.")

        application = VerificationApplication(
            user_id=user.id,
            organization_id=payload.organization_id,
            role=payload.role.value,
            registration_number=payload.registration_number,
            status="pending",
        )
        db.add(application)
        await db.flush()
        for document_id in payload.evidence_document_ids:
            db.add(
                VerificationApplicationDocument(
                    application_id=application.id,
                    document_id=document_id,
                    attached_by=user.id,
                )
            )
        if profile.verification_status in {"not_submitted",
                                           "rejected",
                                           "more_info_requested"
                                           }:
            profile.verification_status = "pending"

        await _audit(
            db,
            request,
            user.id,
            "verification_application.submitted",
            "verification_application",
            application.id,
            {"role": payload.role.value, "evidence_count": len(payload.evidence_document_ids)},
        )
        await db.flush()
        await db.refresh(application)
        response = VerificationApplicationRead.model_validate(application).model_copy(
            update={"document_ids": list(payload.evidence_document_ids)}
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
    *,
    page: int = 1,
    page_size: int = 20,
) -> VerificationApplicationsReadPage:
    _check_account(user, request)
    applications, total = await repository.get_user_applications_page(
        db, user.id, page=page, page_size=page_size
    )
    items = []
    for application in applications:
        ids = await repository.get_application_documents(db, application.id)
        items.append(
            VerificationApplicationRead.model_validate(application).model_copy(
                update={"document_ids": ids}
            )
        )
    return VerificationApplicationsReadPage(
        items=items, page=page, page_size=page_size, total=total
    )
