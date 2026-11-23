from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.core.idempotency import (
    IdempotencyConflict,
    IdempotencyScope,
    lock_and_replay,
    request_digest,
    save_success,
)
from app.modules.audit.service import append_event
from app.modules.clinical_records.models import RecordSource
from app.modules.documents import repository as documents
from app.modules.documents.service import _doctor_can_read
from app.modules.identity.authorization import (
    deny,
    require_role,
    require_verified_membership,
)
from app.modules.laboratory.models import (
    Report,
    ReportIdentity,
    ReportStatus,
    UploadRequestStatus,
)
from app.modules.laboratory.report_notifications import notify_report_accepted
from app.modules.laboratory.report_schemas import ReportsRead, ReportsReadPage
from app.modules.laboratory.service import LabUploadRequestError, require_patient
from app.modules.organizations.models import Organization, OrganizationMembership
from app.modules.profiles.models import ProfessionalProfile


async def _fetch_one(db, sql, **params):
    result = await db.execute(text(sql), params)
    return result.mappings().one_or_none()


async def publish_report(db, actor, patient_id, body, key, request_id):
    """Publish atomically and roll back pending changes on failure."""
    try:
        return await _publish_report(
            db, actor, patient_id, body, key, request_id,
        )
    except Exception:
        await db.rollback()
        raise


async def _publish_report(db, actor, patient_id, body, key, request_id):
    upload_request = None

    if actor.id == patient_id:
        await require_patient(db, actor)

        if body.upload_request_id is not None or body.lab_reference is not None:
            raise LabUploadRequestError(
                422,
                "VALIDATION_ERROR",
                "Lab fields require lab staff.",
            )

        source = RecordSource.PATIENT
    else:
        await require_role(db, actor, "lab_staff")

        if body.upload_request_id is None or not body.lab_reference:
            raise LabUploadRequestError(
                422,
                "VALIDATION_ERROR",
                "Upload request and lab reference required.",
            )

        upload_request = await documents.lab_request(
            db, body.upload_request_id,
        )

        if (
            upload_request is None
            or upload_request.patient_user_id != patient_id
        ):
            deny()

        await require_verified_membership(
            db, actor, "lab_staff", upload_request.laboratory_id,
        )
        source = RecordSource.LAB_STAFF

    scope = IdempotencyScope(
        "user", actor.id, "reports.publish", str(patient_id), key,
    )
    digest = request_digest(body.model_dump(mode="json"))

    try:
        previous = await lock_and_replay(db, scope, digest, _fetch_one)
    except IdempotencyConflict as exc:
        raise LabUploadRequestError(
            409, "IDEMPOTENCY_CONFLICT", str(exc),
        ) from exc

    if previous is not None:
        return ReportsRead.model_validate(previous["response_body"])

    now = datetime.now(UTC)

    if upload_request is not None and (
        upload_request.status != UploadRequestStatus.ACTIVE
        or upload_request.expires_at <= now
    ):
        raise LabUploadRequestError(
            409,
            "INVALID_STATE",
            "Upload request is no longer active.",
        )

    document = await documents.document_by_id(
        db, body.document_id, lock=True,
    )

    if (
        document is None
        or document.owner_user_id != patient_id
        or document.patient_user_id != patient_id
        or document.uploaded_by != actor.id
        or document.document_type != "report"
        or document.status != "active"
        or document.source != source.value
        or document.staged_report_request_id != body.upload_request_id
    ):
        raise LabUploadRequestError(
            404, "NOT_FOUND", "Report document not found.",
        )

    if await documents.report_for_document(db, document.id) is not None:
        raise LabUploadRequestError(
            409, "INVALID_STATE", "Document is already published.",
        )

    identity = None

    if upload_request is not None:
        identity = ReportIdentity(
            patient_user_id=patient_id,
            laboratory_id=upload_request.laboratory_id,
            lab_reference=body.lab_reference,
            initial_upload_request_id=upload_request.id,
        )
        db.add(identity)

        try:
            await db.flush()
        except IntegrityError as exc:
            if getattr(exc.orig, "sqlstate", None) != "23505":
                raise

            raise LabUploadRequestError(
                409,
                "INVALID_STATE",
                "Report reference already exists.",
            ) from exc

        upload_request.status = UploadRequestStatus.USED
        upload_request.used_at = now

    report = Report(
        patient_user_id=patient_id,
        laboratory_id=identity.laboratory_id if identity else None,
        report_identity_id=identity.id if identity else None,
        upload_request_id=upload_request.id if upload_request else None,
        document_id=document.id,
        report_type=body.report_type,
        report_date=body.report_date,
        notes=body.notes,
        uploaded_by=actor.id,
        source=source,
        status=ReportStatus.ACTIVE,
    )
    db.add(report)
    await db.flush()

    if identity is not None:
        await notify_report_accepted(
            db, patient_id=patient_id, report_id=report.id,
        )

    await append_event(
        db,
        actor_user_id=actor.id,
        action="report.published",
        target_type="report",
        target_id=report.id,
        outcome="success",
        request_id=request_id,
    )

    result = ReportsRead.model_validate(report)

    await save_success(
        db, scope, digest, 201, result.model_dump(mode="json"),
    )
    await db.commit()

    return result


async def _require_patient_report_access(db, actor, patient_id):
    if actor.id == patient_id:
        await require_patient(db, actor)
    elif not await _doctor_can_read(db, actor, patient_id, "reports"):
        deny()


async def get_report(db, actor, report_id: UUID):
    report = await db.get(Report, report_id)

    if report is None:
        raise LabUploadRequestError(
            404, "NOT_FOUND", "Report not found.",
        )

    if (
        actor.id != report.patient_user_id
        and report.laboratory_id is not None
        and report.laboratory_id in await _lab_ids(db, actor)
    ):
        await require_role(db, actor, "lab_staff")
    else:
        await _require_patient_report_access(
            db, actor, report.patient_user_id,
        )

    return ReportsRead.model_validate(report)


async def _lab_ids(db, actor):
    result = await db.scalars(
        select(OrganizationMembership.organization_id)
        .join(
            Organization,
            Organization.id == OrganizationMembership.organization_id,
        )
        .join(
            ProfessionalProfile,
            ProfessionalProfile.user_id == OrganizationMembership.user_id,
        )
        .where(
            OrganizationMembership.user_id == actor.id,
            OrganizationMembership.role == "lab_staff",
            OrganizationMembership.status == "approved",
            Organization.status == "active",
            Organization.type == "laboratory",
            ProfessionalProfile.role == "lab_staff",
            ProfessionalProfile.verification_status == "approved",
        )
    )
    return result.all()


async def list_patient_reports(db, actor, patient_id, **filters):
    await _require_patient_report_access(db, actor, patient_id)

    return await _list_reports(
        db, Report.patient_user_id == patient_id, **filters,
    )


async def list_laboratory_reports(db, actor, **filters):
    await require_role(db, actor, "lab_staff")
    laboratories = await _lab_ids(db, actor)

    if not laboratories:
        deny()

    return await _list_reports(
        db, Report.laboratory_id.in_(laboratories), **filters,
    )


async def _list_reports(
    db, scope, *, page, page_size, from_at, to_at, status=None,
):
    if from_at is not None and from_at.tzinfo is None:
        from_at = from_at.replace(tzinfo=UTC)

    if to_at is not None and to_at.tzinfo is None:
        to_at = to_at.replace(tzinfo=UTC)

    if from_at is not None and to_at is not None and from_at > to_at:
        raise LabUploadRequestError(
            422, "VALIDATION_ERROR", "Invalid report date range.",
        )

    filters = [scope]

    if from_at is not None:
        filters.append(Report.created_at >= from_at)

    if to_at is not None:
        filters.append(Report.created_at <= to_at)

    if status is not None:
        filters.append(Report.status == status)

    total = await db.scalar(
        select(func.count()).select_from(Report).where(*filters)
    )
    rows = await db.scalars(
        select(Report)
        .where(*filters)
        .order_by(Report.created_at.desc(), Report.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )

    return ReportsReadPage(
        items=[ReportsRead.model_validate(row) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
    )