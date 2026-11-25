"""Report correction, withdrawal and version history."""

from uuid import UUID

from sqlalchemy import func, select
from starlette.concurrency import run_in_threadpool

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
from app.modules.documents import storage
from app.modules.documents.enum import DocumentStatus
from app.modules.documents.models import Document
from app.modules.identity.authorization import deny, require_verified_membership
from app.modules.laboratory.models import Report, ReportIdentity, ReportStatus
from app.modules.laboratory.report_notifications import notify_report_accepted
from app.modules.laboratory.report_schemas import ReportsRead, ReportsReadPage
from app.modules.laboratory.report_service import _fetch_one, get_report
from app.modules.laboratory.service import LabUploadRequestError, require_patient


async def _report(db, report_id, *, lock=False):
    statement = select(Report).where(Report.id == report_id)
    if lock:
        statement = statement.with_for_update()

    report = await db.scalar(statement)
    if report is None:
        raise LabUploadRequestError(404, "NOT_FOUND", "Report not found.")

    return report


async def _require_editor(db, actor, report):
    if report.source == RecordSource.PATIENT:
        if actor.id != report.patient_user_id or report.laboratory_id is not None:
            deny()
        await require_patient(db, actor)
    elif report.laboratory_id is not None and report.source in {
        RecordSource.LAB_STAFF,
        RecordSource.APPROVED_EXTERNAL_SYSTEM,
    }:
        await require_verified_membership(
            db, actor, "lab_staff", report.laboratory_id
        )
    else:
        deny()


def _require_active(report):
    if report.status != ReportStatus.ACTIVE:
        raise LabUploadRequestError(
            409, "INVALID_STATE", "Report is no longer active."
        )


def _check_replacement_file(storage_key: str) -> None:
    """Read the replacement before changing either report's status."""
    path = storage.get_download_path(storage_key)

    with path.open("rb") as stored_file:
        if not stored_file.read(storage.CHUNK_SIZE):
            raise OSError("Replacement document is empty.")

        while stored_file.read(storage.CHUNK_SIZE):
            pass


async def create_version(db, actor, report_id, body, key, request_id):
    try:
        previous = await _report(db, report_id, lock=True)
        await _require_editor(db, actor, previous)

        scope = IdempotencyScope(
            "user", actor.id, "reports.version", str(report_id), key
        )
        digest = request_digest(body.model_dump(mode="json"))

        try:
            replay = await lock_and_replay(db, scope, digest, _fetch_one)
        except IdempotencyConflict as exc:
            raise LabUploadRequestError(
                409, "IDEMPOTENCY_CONFLICT", str(exc)
            ) from exc

        if replay is not None:
            return ReportsRead.model_validate(replay["response_body"])

        _require_active(previous)

        if previous.laboratory_id is None:
            if body.upload_request_id is not None or body.lab_reference is not None:
                raise LabUploadRequestError(
                    422, "VALIDATION_ERROR", "Lab fields require a lab report."
                )
        else:
            identity = await db.get(ReportIdentity, previous.report_identity_id)
            if identity is None:
                raise LabUploadRequestError(
                    409, "INVALID_STATE", "Report identity is missing."
                )

            if (
                body.upload_request_id is not None
                and body.upload_request_id != identity.initial_upload_request_id
            ) or (
                body.lab_reference is not None
                and body.lab_reference != identity.lab_reference
            ):
                raise LabUploadRequestError(
                    422, "VALIDATION_ERROR", "Original laboratory identity required."
                )

        doc = await documents.document_by_id(db, body.document_id, lock=True)
        if (
            doc is None
            or doc.id == previous.document_id
            or doc.owner_user_id != previous.patient_user_id
            or doc.patient_user_id != previous.patient_user_id
            or doc.uploaded_by != actor.id
            or doc.document_type != "report"
            or doc.status != DocumentStatus.ACTIVE
            or doc.staged_report_request_id is not None
            or doc.source != (
                "patient" if previous.laboratory_id is None else "lab_staff"
            )
            or (
                previous.laboratory_id is not None
                and doc.supersedes_id != previous.document_id
            )
            or (
                previous.laboratory_id is None
                and doc.supersedes_id not in {None, previous.document_id}
            )
        ):
            raise LabUploadRequestError(
                404, "NOT_FOUND", "Correction document not found."
            )

        if await documents.report_for_document(db, doc.id) is not None:
            raise LabUploadRequestError(
                409, "INVALID_STATE", "Document is already published."
            )

        try:
            await run_in_threadpool(_check_replacement_file, doc.storage_key)
        except (OSError, ValueError) as exc:
            raise LabUploadRequestError(
                409,
                "INVALID_STATE",
                "Replacement document file is unavailable.",
            ) from exc

        old_doc = await documents.document_by_id(
            db, previous.document_id, lock=True
        )
        if old_doc is None or old_doc.status not in {
            DocumentStatus.ACTIVE,
            DocumentStatus.SUPERSEDED,
        }:
            raise LabUploadRequestError(
                409, "INVALID_STATE", "Original document is unavailable."
            )

        replacement = Report(
            patient_user_id=previous.patient_user_id,
            laboratory_id=previous.laboratory_id,
            report_identity_id=previous.report_identity_id,
            upload_request_id=previous.upload_request_id,
            document_id=doc.id,
            report_type=body.report_type,
            report_date=body.report_date,
            notes=body.notes,
            uploaded_by=actor.id,
            source=previous.source,
            status=ReportStatus.ACTIVE,
            supersedes_id=previous.id,
            correction_reason=body.reason,
        )

        previous.status = ReportStatus.SUPERSEDED
        old_doc.status = DocumentStatus.SUPERSEDED
        doc.supersedes_id = previous.document_id

        db.add(replacement)
        await db.flush()

        await notify_report_accepted(
            db,
            patient_id=previous.patient_user_id,
            report_id=replacement.id,
        )

        await append_event(
            db,
            actor_user_id=actor.id,
            action="report.corrected",
            target_type="report",
            target_id=replacement.id,
            outcome="success",
            request_id=request_id,
            metadata={"supersedes_id": str(previous.id)},
        )

        result = ReportsRead.model_validate(replacement)
        await save_success(db, scope, digest, 201, result.model_dump(mode="json"))
        await db.commit()
        return result
    except Exception:
        await db.rollback()
        raise


async def _lineage(db, report):
    # Walk to the root, then collect its descendants for patient and lab sources.
    root = report
    seen = set()

    while root.supersedes_id is not None:
        if root.id in seen:
            raise LabUploadRequestError(
                409, "INVALID_STATE", "Invalid report history."
            )
        seen.add(root.id)
        root = await _report(db, root.supersedes_id)

    chain = (
        select(Report.id)
        .where(Report.id == root.id)
        .cte("report_chain", recursive=True)
    )
    chain = chain.union_all(
        select(Report.id).join(chain, Report.supersedes_id == chain.c.id)
    )
    return select(Report).where(Report.id.in_(select(chain.c.id)))


async def withdraw_report(db, actor, report_id, reason, request_id):
    try:
        report = await _report(db, report_id, lock=True)
        await _require_editor(db, actor, report)
        _require_active(report)

        report.status = ReportStatus.WITHDRAWN
        report.withdrawal_reason = reason
        chain = list(await db.scalars(await _lineage(db, report)))

        # Preserve every blob but deny ordinary access to all files in this history.
        docs = await db.scalars(
            select(Document)
            .where(Document.id.in_([row.document_id for row in chain]))
            .with_for_update()
        )
        for doc in docs:
            doc.status = DocumentStatus.WITHDRAWN

        await append_event(
            db,
            actor_user_id=actor.id,
            action="report.withdrawn",
            target_type="report",
            target_id=report.id,
            outcome="success",
            request_id=request_id,
        )

        result = ReportsRead.model_validate(report)
        await db.commit()
        return result
    except Exception:
        await db.rollback()
        raise


async def get_history(db, actor, report_id: UUID, page, page_size):
    await get_report(db, actor, report_id)

    report = await _report(db, report_id)
    statement = await _lineage(db, report)

    total = await db.scalar(
        select(func.count()).select_from(statement.subquery())
    )
    rows = await db.scalars(
        statement
        .order_by(Report.created_at.desc(), Report.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )

    return ReportsReadPage(
        items=[ReportsRead.model_validate(row) for row in rows],
        page=page,
        page_size=page_size,
        total=total,
    )