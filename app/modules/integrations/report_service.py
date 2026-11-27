"""Atomic publication of reports received from an approved laboratory client."""
import hashlib
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.core.idempotency import IdempotencyConflict, IdempotencyScope, lock_and_replay, request_digest, save_success
from app.modules.audit.service import append_event
from app.modules.clinical_records.models import RecordSource
from app.modules.documents import storage
from app.modules.documents.models import Document
from app.modules.integrations.models import IntegrationSubmission, SubmissionStatus
from app.modules.integrations.schemas import IntegrationSubmissionRead
from app.modules.laboratory.models import LabUploadRequest, Report, ReportIdentity, ReportStatus, UploadRequestStatus
from app.modules.laboratory.report_notifications import notify_report_accepted
from app.modules.laboratory.service import LabUploadRequestError, hash_token
from app.modules.organizations.models import Organization


async def _fetch_one(db, sql, **params):
    return (await db.execute(text(sql), params)).mappings().one_or_none()


def _fail(status, code, message):
    raise LabUploadRequestError(status, code, message)


async def submit(db, client, upload, media_type, fields, key, request_id, report_id=None):
    """A database transaction covers the report, submission, audit and notification."""
    storage_key = None
    try:
        org = await db.get(Organization, client.organization_id)
        if org is None or org.status != 'active' or org.type != 'laboratory':
            _fail(403, 'FORBIDDEN', 'Laboratory is not active.')

        # Hash the file itself, never a client-supplied hash or remote URL.
        h = hashlib.sha256()
        await upload.seek(0)
        while chunk := await upload.read(65536):
            h.update(chunk)
        await upload.seek(0)
        digest = request_digest({**fields, 'report_id': str(report_id) if report_id else None}, files={'file': h.hexdigest()})
        scope = IdempotencyScope('client', client.id, 'integration.reports', str(client.id), key)
        try:
            replay = await lock_and_replay(db, scope, digest, _fetch_one)
        except IdempotencyConflict as exc:
            raise LabUploadRequestError(409, 'IDEMPOTENCY_CONFLICT', str(exc)) from exc
        if replay:
            result = IntegrationSubmissionRead.model_validate(replay['response_body'])
            await append_event(
                db,
                actor_client_id=client.id,
                action='integration.report_replayed',
                target_type='integration_submission',
                target_id=result.id,
                outcome='success',
                request_id=request_id,
            )
            await db.commit()
            return result

        # Serialize both new submissions and external IDs, including across different keys.
        lock_value = int.from_bytes(hashlib.sha256((str(client.id) + ':' + fields['external_report_id']).encode()).digest()[:8], 'big', signed=True)
        await db.execute(text('SELECT pg_advisory_xact_lock(:lock_id)'), {'lock_id': lock_value})
        existing = await db.scalar(select(IntegrationSubmission).where(
            IntegrationSubmission.client_id == client.id,
            IntegrationSubmission.external_report_id == fields['external_report_id']))
        if existing:
            previous_scope = IdempotencyScope('client', client.id, 'integration.reports', str(client.id), existing.idempotency_key)
            previous = await _fetch_one(db, '''SELECT request_digest, response_body FROM api_idempotency_records
                WHERE actor_type=:actor_type AND actor_id=:actor AND operation_key=:operation
                AND target_key=:target AND idempotency_key=:key''', **previous_scope.params())
            if not previous or previous['request_digest'] != digest:
                _fail(409, 'IDEMPOTENCY_CONFLICT', 'External report ID is already in use.')
            # Replay is audited in the same transaction; preserve the original response.
            await append_event(db, actor_client_id=client.id, action='integration.report_replayed', target_type='integration_submission', target_id=existing.id, outcome='success', request_id=request_id)
            await save_success(db, scope, digest, 201, previous['response_body'])
            await db.commit()
            return IntegrationSubmissionRead.model_validate(previous['response_body'])

        lab_request = await db.scalar(select(LabUploadRequest).where(
            LabUploadRequest.request_token_hash == hash_token(fields['request_token'])).with_for_update())
        if lab_request is None or lab_request.laboratory_id != client.organization_id:
            _fail(403, 'FORBIDDEN', 'Invalid upload request.')
        previous_report = None
        if report_id is None:
            if lab_request.status != UploadRequestStatus.ACTIVE or lab_request.expires_at <= datetime.now(UTC):
                _fail(409, 'INVALID_STATE', 'Upload request is no longer active.')
            identity = ReportIdentity(id=uuid4(), patient_user_id=lab_request.patient_user_id,
                laboratory_id=client.organization_id, lab_reference=fields['lab_reference'], initial_upload_request_id=lab_request.id)
            db.add(identity)
            await db.flush()
            lab_request.status = UploadRequestStatus.USED
            lab_request.used_at = datetime.now(UTC)
        else:
            previous_report = await db.scalar(select(Report).where(Report.id == report_id).with_for_update())
            owned = await db.scalar(select(IntegrationSubmission.id).where(IntegrationSubmission.client_id == client.id, IntegrationSubmission.report_id == report_id))
            if not previous_report or not owned or previous_report.laboratory_id != client.organization_id:
                _fail(404, 'NOT_FOUND', 'Report not found.')
            identity = await db.get(ReportIdentity, previous_report.report_identity_id)
            if not identity or identity.initial_upload_request_id != lab_request.id or identity.patient_user_id != lab_request.patient_user_id or identity.lab_reference != fields['lab_reference']:
                _fail(403, 'FORBIDDEN', 'Request does not match the report.')
            if previous_report.status != ReportStatus.ACTIVE:
                _fail(409, 'INVALID_STATE', 'Report is no longer active.')
            previous_report.status = ReportStatus.SUPERSEDED

        storage_key, size, sha = await storage.save(upload)
        doc = Document(id=uuid4(), owner_user_id=lab_request.patient_user_id,
            patient_user_id=lab_request.patient_user_id, uploaded_by=None, source=RecordSource.APPROVED_EXTERNAL_SYSTEM.value,
            document_type='report', original_filename=Path(upload.filename or 'report').name[:255],
            storage_key=storage_key, media_type=media_type, byte_size=size, sha256=sha,
            status='active', supersedes_id=previous_report.document_id if previous_report else None)
        db.add(doc)
        await db.flush()
        report = Report(id=uuid4(), patient_user_id=lab_request.patient_user_id, laboratory_id=client.organization_id,
            report_identity_id=identity.id, upload_request_id=lab_request.id if not previous_report else None,
            document_id=doc.id, report_type=fields['report_type'], report_date=date.fromisoformat(fields['report_date']) if fields['report_date'] else None, notes=fields['notes'],
            source=RecordSource.APPROVED_EXTERNAL_SYSTEM, uploaded_by=None, status=ReportStatus.ACTIVE,
            supersedes_id=previous_report.id if previous_report else None, correction_reason=fields.get('reason'))
        db.add(report)
        await db.flush()
        submission = IntegrationSubmission(id=uuid4(), client_id=client.id, upload_request_id=lab_request.id,
            external_report_id=fields['external_report_id'], idempotency_key=key,
            status=SubmissionStatus.ACCEPTED, report_id=report.id)
        db.add(submission)
        await db.flush()
        await db.refresh(submission)
        await notify_report_accepted(db, patient_id=lab_request.patient_user_id, report_id=report.id)
        await append_event(db, actor_client_id=client.id, action='integration.report_submitted',
            target_type='report', target_id=report.id, outcome='success', request_id=request_id)
        result = IntegrationSubmissionRead.model_validate(submission)
        await save_success(db, scope, digest, 201, result.model_dump(mode='json'))
        await db.commit()
        return result
    except IntegrityError as exc:
        await db.rollback()
        if storage_key:
            await storage.delete(storage_key)
        if getattr(exc.orig, 'sqlstate', None) == '23505':
            raise LabUploadRequestError(409, 'INVALID_STATE', 'Report or request already exists.') from exc
        raise
    except Exception:
        await db.rollback()
        if storage_key:
            await storage.delete(storage_key)
        raise
