
"""ML-36: private document upload and permission-checked retrieval."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from app.modules.audit.models import AuditEvent
from app.modules.consent.models import AccessGrant, GrantCategory
from app.modules.documents.enum import DocumentStatus, RecordSource
from app.modules.documents.errors import DocumentError
from app.modules.documents.models import Document
from app.modules.identity.authorization import (
    require_doctor_grant,
    require_verified_membership,
)
from app.modules.identity.models import Role, User, UserRole
from app.modules.laboratory.models import (
    Report,
    ReportIdentity,
    ReportStatus,
    UploadRequestStatus,
)
from app.modules.profiles.models import PatientProfile, ProfessionalProfile
from app.modules.verification.models import VerificationApplicationDocument
from fastapi import HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.documents import repository, storage

logger = logging.getLogger(__name__)

ALLOWED_TYPES = {
    "prescription",
    "report",
    "verification_evidence",
}


def _reject(
    status: int = 404,
    code: str = "RESOURCE_NOT_FOUND",
    message: str = "Document not found.",
) -> None:
    """Raise a contract-compliant document error."""
    raise DocumentError(status, code, message)


async def _has_role(
    db: AsyncSession,
    user: User,
    *roles: str,
) -> bool:
    return bool(
        await db.scalar(
            select(Role.id)
            .join(UserRole, Role.id == UserRole.role_id)
            .where(
                UserRole.user_id == user.id,
                Role.code.in_(roles),
            )
            .limit(1)
        )
    )


def _audit(
    db: AsyncSession,
    user: User,
    action: str,
    doc_id: UUID,
    request_id: str,
) -> None:
    db.add(
        AuditEvent(
            actor_user_id=user.id,
            action=action,
            target_type="document",
            target_id=doc_id,
            outcome="success",
            request_id=request_id,
            metadata_={},
        )
    )


async def _require_lab_membership(
    db: AsyncSession,
    user: User,
    lab_id: UUID,
) -> None:
    try:
        await require_verified_membership(
            db,
            user,
            "lab_staff",
            lab_id,
        )

    except HTTPException as exc:
        if exc.status_code == 403:
            _reject(
                403,
                "ACCESS_DENIED",
                "Not permitted to upload this document.",
            )
        raise


async def _lab_allowed(
    db: AsyncSession,
    user: User,
    patient_id: UUID,
    request_id: UUID | None,
    supersedes_id: UUID | None,
) -> None:
    if request_id is not None and supersedes_id is not None:
        _reject(
            422,
            "VALIDATION_ERROR",
            "Use either an upload request or a correction reference.",
        )

    if request_id is not None:
        req = await repository.lab_request(db, request_id)

        if (
            req is None
            or req.patient_user_id != patient_id
            or req.status != "active"
            or req.expires_at <= datetime.now(UTC)
        ):
            _reject(
                403,
                "ACCESS_DENIED",
                "Not permitted to upload this document.",
            )

        await _require_lab_membership(
            db,
            user,
            req.laboratory_id,
        )
        return

    if supersedes_id is None:
        _reject(
            403,
            "ACCESS_DENIED",
            "Lab upload request is required.",
        )

    original_report = await db.scalar(
        select(Report)
        .where(Report.document_id == supersedes_id)
        .with_for_update()
    )

    if (
        original_report is None
        or original_report.patient_user_id != patient_id
        or original_report.laboratory_id is None
        or original_report.status != ReportStatus.ACTIVE
    ):
        _reject(
            403,
            "ACCESS_DENIED",
            "Not permitted to correct this report.",
        )

    await _require_lab_membership(
        db,
        user,
        original_report.laboratory_id,
    )

async def _persist_lab_report(
    db: AsyncSession,
    user: User,
    doc: Document,
    *,
    patient_id: UUID,
    upload_request_id: UUID | None,
    supersedes_id: UUID | None,
    correction_reason: str | None,
) -> None:
    """Persist laboratory report and its lineage in the upload transaction."""

    now = datetime.now(UTC)

    if upload_request_id is not None:
        req = await repository.lab_request(db, upload_request_id)

        if (
            req is None
            or req.patient_user_id != patient_id
            or req.status != UploadRequestStatus.ACTIVE
            or req.expires_at <= now
        ):
            _reject(
                409,
                "INVALID_STATE",
                "Upload request is no longer active.",
            )


        identity = ReportIdentity(
            id=uuid4(),
            patient_user_id=patient_id,
            laboratory_id=req.laboratory_id,
            lab_reference=f"upload-{req.id}",
            initial_upload_request_id=req.id,
        )
        db.add(identity)
        await db.flush()

        report = Report(
            id=uuid4(),
            patient_user_id=patient_id,
            laboratory_id=req.laboratory_id,
            report_identity_id=identity.id,
            upload_request_id=req.id,
            document_id=doc.id,
            report_type="report",
            report_date=None,
            notes=None,
            source=RecordSource.LAB_STAFF.value,
            uploaded_by=user.id,
            status=ReportStatus.ACTIVE,
            supersedes_id=None,
            correction_reason=None,
        )
        db.add(report)

        req.status = UploadRequestStatus.USED
        req.used_at = now
        return

    if supersedes_id is None:
        _reject(
            422,
            "VALIDATION_ERROR",
            "Lab upload request or correction reference is required.",
        )

    reason = (correction_reason or "").strip()
    if not reason:
        _reject(
            422,
            "VALIDATION_ERROR",
            "Correction reason is required.",
        )

    # Lock the previous report while checking and updating its state.
    previous_report = await db.scalar(
        select(Report)
        .where(Report.document_id == supersedes_id)
        .with_for_update()
    )

    if (
        previous_report is None
        or previous_report.patient_user_id != patient_id
        or previous_report.laboratory_id is None
        or previous_report.status != ReportStatus.ACTIVE
    ):
        _reject(
            409,
            "INVALID_STATE",
            "Previous report cannot be corrected.",
        )

    # _lab_allowed() has already verified membership for this laboratory.
    replacement = Report(
        id=uuid4(),
        patient_user_id=previous_report.patient_user_id,
        laboratory_id=previous_report.laboratory_id,
        report_identity_id=previous_report.report_identity_id,
        upload_request_id=None,
        document_id=doc.id,
        report_type=previous_report.report_type,
        report_date=previous_report.report_date,
        notes=previous_report.notes,
        source=RecordSource.LAB_STAFF.value,
        uploaded_by=user.id,
        status=ReportStatus.ACTIVE,
        supersedes_id=previous_report.id,
        correction_reason=reason,
    )

    db.add(replacement)
    previous_report.status = ReportStatus.SUPERSEDED


async def upload_document(
    db: AsyncSession,
    user: User,
    upload: UploadFile,
    media_type: str,
    document_type: str,
    request_id: str,
    *,
    patient_id: UUID | None = None,
    upload_request_id: UUID | None = None,
    verification_application_id: UUID | None = None,
    supersedes_id: UUID | None = None,
    correction_reason: str | None = None,
) -> Document:


    if document_type not in ALLOWED_TYPES:
        _reject(
            422,
            "VALIDATION_ERROR",
            "Invalid document type.",
        )

    if correction_reason is not None:
        if (
            document_type != "report"
            or patient_id is None
            or supersedes_id is None
            or upload_request_id is not None
        ):
            _reject(
                422,
                "VALIDATION_ERROR",
                "Invalid correction context.",
            )

    if user.status not in {"active", "pending"}:
        _reject(
            403,
            "ACCESS_DENIED",
            "Account is not eligible.",
        )


    if patient_id is None:
        if (
            document_type != "verification_evidence"
            or upload_request_id is not None
        ):
            _reject(
                422,
                "VALIDATION_ERROR",
                "Invalid verification document context.",
            )

        if not await _has_role(
            db,
            user,
            "doctor",
            "pharmacist",
            "lab_staff",
        ):
            _reject(
                403,
                "ACCESS_DENIED",
                "Professional role required.",
            )

        profile = await db.scalar(
            select(ProfessionalProfile).where(
                ProfessionalProfile.user_id == user.id
            )
        )

        if (
            profile is None
            or profile.role not in {
                "doctor",
                "pharmacist",
                "lab_staff",
            }
        ):
            _reject(
                403,
                "ACCESS_DENIED",
                "Professional profile required.",
            )

        source = RecordSource(profile.role)

    else:
        if (
            document_type == "verification_evidence"
            or verification_application_id is not None
        ):
            _reject(
                422,
                "VALIDATION_ERROR",
                "Invalid patient document context.",
            )

        patient_exists = await db.scalar(
            select(PatientProfile.user_id).where(
                PatientProfile.user_id == patient_id
            )
        )

        if not patient_exists:
            _reject()

        if user.id == patient_id:
            if (
                user.status != "active"
                or not await _has_role(db, user, "patient")
            ):
                _reject(
                    403,
                    "ACCESS_DENIED",
                    "Patient role required.",
                )

            if upload_request_id is not None:
                _reject(
                    422,
                    "VALIDATION_ERROR",
                    "Only laboratory staff may use upload requests.",
                )

            source = RecordSource.PATIENT

        else:
            if document_type != "report":
                _reject(
                    403,
                    "ACCESS_DENIED",
                    "Only laboratory reports may be uploaded for another patient.",
                )

            await _lab_allowed(
                db,
                user,
                patient_id,
                upload_request_id,
                supersedes_id,
            )

            source = RecordSource.LAB_STAFF

    parent = None

    if verification_application_id is not None:
        from app.modules.verification.models import VerificationApplication

        parent = await db.scalar(
            select(VerificationApplication).where(
                VerificationApplication.id == verification_application_id,
                VerificationApplication.user_id == user.id,
                VerificationApplication.status.in_(
                    [
                        "not_submitted",
                        "pending",
                        "more_info_requested",
                    ]
                ),
            )
        )

        if parent is None:
            _reject(
                403,
                "ACCESS_DENIED",
                "Verification application is not accessible.",
            )

    previous = None

    if supersedes_id is not None:
        previous = await repository.document_by_id(
            db,
            supersedes_id,
            lock=True,
        )

        # Corrected 409 API contract error code
        if (
            previous is None
            or previous.status != DocumentStatus.ACTIVE
            or previous.document_type != document_type
        ):
            _reject(
                409,
                "INVALID_STATE",
                "Document cannot be replaced.",
            )

        if previous.owner_user_id != (patient_id or user.id):
            _reject(
                403,
                "ACCESS_DENIED",
                "Document cannot be replaced.",
            )

        if patient_id is not None and user.id != patient_id:
            if not await repository.report_for_document(
                db,
                previous.id,
            ):
                _reject(
                    403,
                    "ACCESS_DENIED",
                    "Only the issuing lab may correct this report.",
                )

        elif previous.uploaded_by != user.id:
            _reject(
                403,
                "ACCESS_DENIED",
                "Document cannot be replaced.",
            )

        replacement_exists = await db.scalar(
            select(Document.id)
            .where(Document.supersedes_id == previous.id)
            .limit(1)
        )

        # Corrected 409 API contract error code
        if replacement_exists:
            _reject(
                409,
                "INVALID_STATE",
                "A replacement already exists.",
            )

    key = None

    try:
        key, byte_size, digest = await storage.save(upload)

        doc = Document(
            id=uuid4(),
            owner_user_id=patient_id or user.id,
            patient_user_id=patient_id,
            uploaded_by=user.id,
            source=source,
            document_type=document_type,
            original_filename=Path(
                upload.filename or "document"
            ).name[:255],
            storage_key=key,
            media_type=media_type,
            byte_size=byte_size,
            sha256=digest,
            status=DocumentStatus.ACTIVE,
            supersedes_id=supersedes_id,
        )

        db.add(doc)

        if (
                patient_id is not None
                and user.id != patient_id
                and document_type == "report"
        ):
            await _persist_lab_report(
                db,
                user,
                doc,
                patient_id=patient_id,
                upload_request_id=upload_request_id,
                supersedes_id=supersedes_id,
                correction_reason=correction_reason,
            )

        if previous is not None:
            previous.status = DocumentStatus.SUPERSEDED

        if parent is not None:
            db.add(
                VerificationApplicationDocument(
                    application_id=parent.id,
                    document_id=doc.id,
                    attached_by=user.id,
                )
            )

        _audit(
            db,
            user,
            "document.upload",
            doc.id,
            request_id,
        )

        await db.flush()
        await db.refresh(doc)
        await db.commit()

        return doc

    except Exception:
        await db.rollback()

        if key is not None:
            try:
                await storage.delete(key)
            except OSError:
                logger.exception(
                    "Could not clean up failed private document upload"
                )

        raise


async def _doctor_can_read(
    db: AsyncSession,
    user: User,
    patient_id: UUID,
    category: str,
) -> bool:

    grants = (
        await db.scalars(
            select(AccessGrant.organization_id)
            .join(
                GrantCategory,
                GrantCategory.grant_id == AccessGrant.id,
            )
            .where(
                AccessGrant.patient_user_id == patient_id,
                AccessGrant.professional_user_id == user.id,
                GrantCategory.category == category,
                AccessGrant.organization_id.is_not(None),
            )
        )
    ).all()

    for org in grants:
        try:
            await require_doctor_grant(
                db,
                user,
                patient_id,
                org,
                category,
            )
            return True

        except HTTPException as exc:
            if exc.status_code == 403:
                continue

            raise

    return False


async def _authorized(
    db: AsyncSession,
    user: User,
    doc: Document,
) -> bool:

    # Verification evidence cannot be read using patient grants.
    if doc.document_type == "verification_evidence":
        if doc.owner_user_id == user.id:
            return True

        parent = await repository.verification_for_document(
            db,
            doc.id,
        )

        return (
            parent is not None
            and await _has_role(
                db,
                user,
                "admin",
                "super_admin",
            )
            and user.status == "active"
        )

    report = await repository.report_for_document(
        db,
        doc.id,
    )

    prescription = await repository.prescription_for_document(
        db,
        doc.id,
    )

    if report is None and prescription is None:
        # No parent resource: uploader only
        return doc.uploaded_by == user.id

    if report is not None:
        if report.patient_user_id != doc.patient_user_id:
            return False

        if report.patient_user_id == user.id:
            return True

        if (
            report.uploaded_by == user.id
            and report.laboratory_id is not None
        ):
            try:
                await require_verified_membership(
                    db,
                    user,
                    "lab_staff",
                    report.laboratory_id,
                )
                return True

            except HTTPException as exc:
                if exc.status_code != 403:
                    raise

        return await _doctor_can_read(
            db,
            user,
            report.patient_user_id,
            "reports",
        )

    if prescription is not None:
        if prescription.patient_user_id != doc.patient_user_id:
            return False

        if prescription.patient_user_id == user.id:
            return True

        return await _doctor_can_read(
            db,
            user,
            prescription.patient_user_id,
            "prescriptions",
        )

    return False


async def permitted_document(
    db: AsyncSession,
    user: User,
    document_id: UUID,
    request_id: str,
    *,
    content: bool = False,
) -> Document:

    if user.status not in {"active", "pending"}:
        _reject()

    doc = await repository.document_by_id(
        db,
        document_id,
    )

    if (
        doc is None
        or doc.status in {
            DocumentStatus.QUARANTINED,
            DocumentStatus.WITHDRAWN,
        }
    ):
        _reject()

    if not await _authorized(db, user, doc):
        # Avoid revealing document existence.
        _reject()

    _audit(
        db,
        user,
        "document.download" if content else "document.metadata_read",
        doc.id,
        request_id,
    )

    await db.commit()

    return doc
