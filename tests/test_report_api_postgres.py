"""ML-116 HTTP + PostgreSQL regression tests on an already migrated test database.

Only authentication/rate limits and physical blob storage are replaced. Role,
membership, report, request, notification, audit and idempotency SQL are real.
All synthetic database changes are isolated inside an outer rolled-back transaction.
"""

import hashlib
import os
import tempfile
import time
from datetime import UTC, date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, skipUnless
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID, uuid4

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pypdf import PdfWriter
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import settings
from app.database import models as _models  # noqa: F401 - register all ORM tables
from app.database.session import get_session
from app.modules.audit.models import AuditEvent
from app.modules.documents import service as document_service
from app.modules.documents.errors import DocumentError, document_error_handler
from app.modules.documents.models import Document
from app.modules.documents.reconcile import MIN_AGE_SECONDS, inventory, quarantine
from app.modules.documents.router import router as document_router
from app.modules.identity.auth import get_current_user
from app.modules.identity.models import Role, User, UserRole
from app.modules.identity.request_limits import (
    require_authenticated_rate_limit,
    require_upload_rate_limit,
)
from app.modules.laboratory import report_service
from app.modules.laboratory.models import LabUploadRequest, Report, ReportIdentity
from app.modules.laboratory.router import router as laboratory_router
from app.modules.notifications.models import Notification
from app.modules.organizations.models import Organization, OrganizationMembership
from app.modules.profiles.models import PatientProfile, ProfessionalProfile

TEST_DATABASE_URL = os.getenv("ML116_TEST_DATABASE_URL")


@skipUnless(TEST_DATABASE_URL, "Set ML116_TEST_DATABASE_URL to a migrated test database")
class ReportPostgresTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        engine = create_async_engine(TEST_DATABASE_URL, hide_parameters=True)
        self.addAsyncCleanup(engine.dispose)

        connection = await engine.connect()
        self.addAsyncCleanup(connection.close)

        transaction = await connection.begin()
        self.addAsyncCleanup(transaction.rollback)

        self.db = AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        self.addAsyncCleanup(self.db.close)

        self.patient_id = await self.create_user("patient")
        self.other_patient_id = await self.create_user("patient")
        self.lab_user_id = await self.create_user("lab_staff")
        self.other_lab_user_id = await self.create_user("lab_staff")

        self.lab_id, self.membership_id = await self.create_lab(self.lab_user_id)
        self.other_lab_id, _ = await self.create_lab(self.other_lab_user_id)

        await self.db.commit()
        self.actor_id = self.lab_user_id

        app = FastAPI()
        app.include_router(document_router, prefix="/api/v1")
        app.include_router(laboratory_router, prefix="/api/v1")
        app.add_exception_handler(DocumentError, document_error_handler)

        @app.middleware("http")
        async def request_id(request, call_next):
            request.state.request_id = str(uuid4())
            return await call_next(request)

        async def session_override():
            try:
                yield self.db
            finally:
                if self.db.in_transaction():
                    await self.db.rollback()

        async def actor_override():
            return await self.db.get(User, self.actor_id)

        app.dependency_overrides.update({
            get_session: session_override,
            get_current_user: actor_override,
            require_authenticated_rate_limit: lambda: None,
            require_upload_rate_limit: lambda: None,
        })

        self.client = AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        )
        self.addAsyncCleanup(self.client.aclose)

        pdf = PdfWriter()
        pdf.add_blank_page(width=72, height=72)
        stream = BytesIO()
        pdf.write(stream)
        self.pdf = stream.getvalue()

        async def save(upload):
            content = await upload.read()
            digest = hashlib.sha256(content).hexdigest()
            return f"ml116-synthetic/{uuid4()}.pdf", len(content), digest

        self.storage = {
            "save": AsyncMock(side_effect=save),
            "delete": AsyncMock(),
        }
        storage_patch = patch.multiple(
            document_service.storage,
            save=self.storage["save"],
            delete=self.storage["delete"],
            get_download_path=Mock(return_value="synthetic-private-file"),
        )
        storage_patch.start()
        self.addCleanup(storage_patch.stop)
        self.storage_patch = storage_patch

    async def create_user(self, code):
        role = await self.db.scalar(select(Role).where(Role.code == code))

        if role is None:
            role = Role(code=code, label=f"ML116 synthetic {code}")
            self.db.add(role)
            await self.db.flush()

        user = User(
            firebase_uid=f"ml116-{uuid4()}",
            display_name="ML116 synthetic user",
            status="active",
        )
        self.db.add(user)
        await self.db.flush()

        self.db.add(UserRole(user_id=user.id, role_id=role.id))

        if code == "patient":
            self.db.add(PatientProfile(
                user_id=user.id,
                date_of_birth=date(1990, 1, 1),
                sex="female",
            ))
        else:
            self.db.add(ProfessionalProfile(
                user_id=user.id,
                role="lab_staff",
                verification_status="approved",
            ))

        await self.db.flush()
        return user.id

    async def create_lab(self, user_id):
        lab = Organization(
            name="ML116 synthetic laboratory",
            type="laboratory",
            status="active",
        )
        self.db.add(lab)
        await self.db.flush()

        membership = OrganizationMembership(
            organization_id=lab.id,
            user_id=user_id,
            role="lab_staff",
            status="approved",
        )
        self.db.add(membership)
        await self.db.flush()

        return lab.id, membership.id

    async def create_request(self):
        request = LabUploadRequest(
            patient_user_id=self.patient_id,
            laboratory_id=self.lab_id,
            request_token_hash=hashlib.sha256(uuid4().bytes).hexdigest(),
            status="active",
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
        self.db.add(request)
        await self.db.flush()

        result = request.id
        await self.db.commit()
        return result

    async def upload(self, *, request_id=None, stage=False, patient_id=None):
        data = {
            "document_type": "report",
            "stage_for_report": str(stage).lower(),
        }

        if request_id is not None:
            data["upload_request_id"] = str(request_id)

        return await self.client.post(
            f"/api/v1/patients/{patient_id or self.patient_id}/documents",
            data=data,
            files={"file": ("synthetic.pdf", self.pdf, "application/pdf")},
        )

    async def staged(self):
        request_id = await self.create_request()
        response = await self.upload(request_id=request_id, stage=True)
        self.assertEqual(response.status_code, 201, response.text)
        return request_id, UUID(response.json()["id"])

    async def publish(self, document_id, request_id=None, *, key=None, **changes):
        body = {
            "document_id": str(document_id),
            "report_type": "FBC",
        }

        if request_id is not None:
            body.update(
                upload_request_id=str(request_id),
                lab_reference=f"ML116-{request_id}",
            )

        body.update(changes)

        return await self.client.post(
            f"/api/v1/patients/{self.patient_id}/reports",
            json=body,
            headers={"Idempotency-Key": key or f"ml116-{uuid4()}"},
        )

    async def count(self, model, *conditions):
        return await self.db.scalar(
            select(func.count()).select_from(model).where(*conditions)
        )

    async def assert_unpublished(self, request_id, document_id):
        self.assertEqual(
            await self.count(Report, Report.document_id == document_id),
            0,
        )
        self.assertEqual(
            await self.count(
                ReportIdentity,
                ReportIdentity.initial_upload_request_id == request_id,
            ),
            0,
        )
        self.assertEqual(
            await self.count(Notification, Notification.user_id == self.patient_id),
            0,
        )

    async def test_missing_content_does_not_publish_report(self):
        self.actor_id = self.patient_id
        upload = await self.upload()
        self.assertEqual(upload.status_code, 201, upload.text)
        document_id = UUID(upload.json()["id"])
        with patch.object(
            document_service.storage, "get_download_path", side_effect=FileNotFoundError
        ):
            response = await self.publish(document_id)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(await self.count(Report, Report.document_id == document_id), 0)

    async def test_real_private_file_loss_prevents_publish(self):
        self.storage_patch.stop()
        self.actor_id = self.patient_id
        with (
            tempfile.TemporaryDirectory() as root,
            patch.object(settings, "private_upload_dir", root),
        ):
            upload = await self.upload()
            self.assertEqual(upload.status_code, 201, upload.text)
            document_id = UUID(upload.json()["id"])
            doc = await self.db.get(Document, document_id)
            path = document_service.storage.get_download_path(doc.storage_key)
            self.assertEqual(path.read_bytes(), self.pdf)
            path.unlink()
            response = await self.publish(document_id)
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(await self.count(Report, Report.document_id == document_id), 0)

    async def test_commit_failure_retains_orphan_without_publishing(self):
        self.storage_patch.stop()
        self.actor_id = self.patient_id
        with (
            tempfile.TemporaryDirectory() as root,
            patch.object(settings, "private_upload_dir", root),
        ):
            with patch.object(self.db, "commit", side_effect=RuntimeError("commit failed")):
                response = await self.upload()
            self.assertEqual(response.status_code, 500, response.text)
            self.assertEqual(
                await self.count(Document, Document.patient_user_id == self.patient_id), 0,
            )
            self.assertEqual(await self.count(Report, Report.patient_user_id == self.patient_id), 0)
            files = list(Path(root).iterdir())
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].read_bytes(), self.pdf)
            old = time.time() - MIN_AGE_SECONDS - 5
            os.utime(files[0], (old, old))
            rows = await self.db.execute(text("SELECT storage_key, id FROM documents"))
            refs = {row.storage_key: str(row.id) for row in rows}
            orphans, missing = inventory(Path(root), refs, time.time())
            self.assertEqual(orphans, [files[0].name])
            self.assertEqual(missing, [])
            quarantine(Path(root), orphans)
            self.assertEqual((Path(root) / ".quarantine" / files[0].name).read_bytes(), self.pdf)
            self.assertEqual(inventory(Path(root), refs, time.time()), ([], []))

    async def test_private_document_content_rejects_other_patient(self):
        self.storage_patch.stop()
        self.actor_id = self.patient_id
        with (
            tempfile.TemporaryDirectory() as root,
            patch.object(settings, "private_upload_dir", root),
        ):
            upload = await self.upload()
            self.assertEqual(upload.status_code, 201, upload.text)
            document_id = UUID(upload.json()["id"])
            self.actor_id = self.other_patient_id
            response = await self.client.get(f"/api/v1/documents/{document_id}/content")
            self.assertIn(response.status_code, (403, 404), response.text)
            self.assertNotEqual(response.content, self.pdf)

    async def test_lost_commit_response_keeps_committed_file(self):
        self.storage_patch.stop()
        self.actor_id = self.patient_id
        real_commit = self.db.commit

        async def commit_then_disconnect():
            await real_commit()
            raise OSError("simulated lost commit response")

        with (
            tempfile.TemporaryDirectory() as root,
            patch.object(settings, "private_upload_dir", root),
            patch.object(self.db, "commit", side_effect=commit_then_disconnect),
        ):
            response = await self.upload()
            self.assertEqual(response.status_code, 500, response.text)
            docs = (await self.db.scalars(
                select(Document).where(Document.patient_user_id == self.patient_id)
            )).all()
            self.assertEqual(len(docs), 1)
            path = document_service.storage.get_download_path(docs[0].storage_key)
            self.assertEqual(path.read_bytes(), self.pdf)
            old = time.time() - MIN_AGE_SECONDS - 5
            os.utime(path, (old, old))
            rows = await self.db.execute(text("SELECT storage_key, id FROM documents"))
            refs = {row.storage_key: str(row.id) for row in rows}
            self.assertEqual(inventory(Path(root), refs, time.time()), ([], []))

    async def test_file_save_failure_does_not_publish_metadata(self):
        self.actor_id = self.patient_id
        with patch.object(document_service.storage, "save", side_effect=OSError("disk failure")):
            response = await self.upload()
        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(
            await self.count(Document, Document.patient_user_id == self.patient_id), 0,
        )
        self.assertEqual(await self.count(Report, Report.patient_user_id == self.patient_id), 0)

    async def test_patient_upload_publish_and_own_list(self):
        self.actor_id = self.patient_id

        upload = await self.upload()
        self.assertEqual(upload.status_code, 201, upload.text)

        response = await self.publish(UUID(upload.json()["id"]))
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["source"], "patient")
        self.assertIsNone(response.json()["laboratory_id"])

        listing = await self.client.get(
            f"/api/v1/patients/{self.patient_id}/reports"
        )
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual(
            [row["id"] for row in listing.json()["items"]],
            [response.json()["id"]],
        )

    async def test_staging_does_not_publish_or_consume_or_notify(self):
        request_id, document_id = await self.staged()

        await self.assert_unpublished(request_id, document_id)

        request = await self.db.get(LabUploadRequest, request_id)
        self.assertEqual(request.status, "active")
        self.assertIsNone(request.used_at)

    async def test_verified_lab_publish_notifies_once_and_replays(self):
        request_id, document_id = await self.staged()
        key = f"ml116-{uuid4()}"

        response = await self.publish(document_id, request_id, key=key)
        self.assertEqual(response.status_code, 201, response.text)

        replay = await self.publish(document_id, request_id, key=key)
        self.assertEqual(replay.status_code, 201, replay.text)
        self.assertEqual(replay.json(), response.json())

        request = await self.db.get(LabUploadRequest, request_id)
        self.assertEqual(request.status, "used")
        self.assertIsNotNone(request.used_at)

        report_id = UUID(response.json()["id"])

        self.assertEqual(
            await self.count(Report, Report.document_id == document_id),
            1,
        )
        self.assertEqual(
            await self.count(Notification, Notification.related_id == report_id),
            1,
        )
        self.assertEqual(
            await self.count(
                AuditEvent,
                AuditEvent.target_id == report_id,
                AuditEvent.action == "report.published",
            ),
            1,
        )

        own_lab = await self.client.get("/api/v1/me/laboratory-reports")
        self.assertEqual(own_lab.status_code, 200, own_lab.text)
        self.assertEqual(own_lab.json()["total"], 1)

        self.actor_id = self.patient_id
        metadata = await self.client.get(f"/api/v1/reports/{report_id}")
        self.assertEqual(metadata.status_code, 200, metadata.text)

    async def test_immediate_lab_upload_publishes_and_notifies_once(self):
        request_id = await self.create_request()

        response = await self.upload(request_id=request_id)
        self.assertEqual(response.status_code, 201, response.text)

        document_id = UUID(response.json()["id"])
        report = await self.db.scalar(
            select(Report).where(Report.document_id == document_id)
        )
        self.assertIsNotNone(report)

        self.assertEqual(
            await self.count(Notification, Notification.related_id == report.id),
            1,
        )
        self.assertEqual(
            (await self.db.get(LabUploadRequest, request_id)).status,
            "used",
        )

        repeat = await self.publish(document_id, request_id)
        self.assertEqual(repeat.status_code, 409, repeat.text)

        self.assertEqual(
            await self.count(Notification, Notification.user_id == self.patient_id),
            1,
        )

    async def test_changed_payload_same_key_conflicts(self):
        request_id, document_id = await self.staged()
        key = f"ml116-{uuid4()}"

        first = await self.publish(document_id, request_id, key=key)
        self.assertEqual(first.status_code, 201, first.text)

        response = await self.publish(
            document_id, request_id, key=key, report_type="LFT",
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["code"], "IDEMPOTENCY_CONFLICT")

    async def test_original_lab_correction_notifies_and_preserves_lineage(self):
        request_id = await self.create_request()

        uploaded = await self.upload(request_id=request_id)
        self.assertEqual(uploaded.status_code, 201, uploaded.text)

        document_id = UUID(uploaded.json()["id"])
        previous = await self.db.scalar(
            select(Report).where(Report.document_id == document_id)
        )
        previous_id = previous.id
        identity_id = previous.report_identity_id

        request = await self.db.get(LabUploadRequest, request_id)
        request.expires_at = datetime.now(UTC) - timedelta(days=1)
        await self.db.commit()

        corrected = await self.client.post(
            f"/api/v1/patients/{self.patient_id}/documents",
            data={
                "document_type": "report",
                "supersedes_id": str(document_id),
                "correction_reason": "Synthetic correction",
            },
            files={"file": ("corrected.pdf", self.pdf, "application/pdf")},
        )
        self.assertEqual(corrected.status_code, 201, corrected.text)

        replacement = await self.db.scalar(
            select(Report).where(
                Report.document_id == UUID(corrected.json()["id"]),
            )
        )
        self.assertEqual(replacement.supersedes_id, previous_id)
        self.assertEqual(replacement.report_identity_id, identity_id)

        previous = await self.db.get(Report, previous_id)
        self.assertEqual(previous.status, "superseded")

        self.assertEqual(
            await self.count(
                Notification, Notification.related_id == replacement.id,
            ),
            1,
        )
        self.assertEqual(
            await self.count(Notification, Notification.related_id == previous_id),
            1,
        )

    async def test_duplicate_publish_with_new_key_is_rejected(self):
        request_id, document_id = await self.staged()

        response = await self.publish(document_id, request_id)
        self.assertEqual(response.status_code, 201, response.text)

        repeat = await self.publish(document_id, request_id)
        self.assertEqual(repeat.status_code, 409, repeat.text)

        self.assertEqual(
            await self.count(Report, Report.document_id == document_id),
            1,
        )
        self.assertEqual(
            await self.count(Notification, Notification.user_id == self.patient_id),
            1,
        )

    async def test_expired_revoked_and_used_requests_are_rejected(self):
        for status in ("expired", "revoked", "used"):
            with self.subTest(status=status):
                request_id, document_id = await self.staged()
                request = await self.db.get(LabUploadRequest, request_id)

                if status == "expired":
                    request.expires_at = datetime.now(UTC) - timedelta(seconds=1)
                else:
                    request.status = status

                await self.db.commit()

                response = await self.publish(document_id, request_id)
                self.assertEqual(response.status_code, 409, response.text)

                await self.assert_unpublished(request_id, document_id)

    async def test_wrong_lab_and_wrong_patient_requests_are_denied(self):
        request_id, document_id = await self.staged()

        self.actor_id = self.other_lab_user_id
        response = await self.publish(document_id, request_id)
        self.assertEqual(response.status_code, 403, response.text)

        self.actor_id = self.lab_user_id
        request = await self.db.get(LabUploadRequest, request_id)
        request.patient_user_id = self.other_patient_id
        await self.db.commit()

        response = await self.publish(document_id, request_id)
        self.assertEqual(response.status_code, 403, response.text)

        await self.assert_unpublished(request_id, document_id)

    async def test_pending_and_suspended_memberships_are_denied(self):
        request_id, document_id = await self.staged()

        for status in ("pending", "suspended"):
            with self.subTest(status=status):
                membership = await self.db.get(
                    OrganizationMembership, self.membership_id,
                )
                membership.status = status
                await self.db.commit()

                response = await self.publish(document_id, request_id)
                self.assertEqual(response.status_code, 403, response.text)

                await self.assert_unpublished(request_id, document_id)

    async def test_pending_profile_and_inactive_lab_are_denied(self):
        request_id, document_id = await self.staged()

        profile = await self.db.get(ProfessionalProfile, self.lab_user_id)
        profile.verification_status = "pending"
        await self.db.commit()

        response = await self.publish(document_id, request_id)
        self.assertEqual(response.status_code, 403, response.text)

        profile = await self.db.get(ProfessionalProfile, self.lab_user_id)
        profile.verification_status = "approved"

        lab = await self.db.get(Organization, self.lab_id)
        lab.status = "suspended"
        await self.db.commit()

        response = await self.publish(document_id, request_id)
        self.assertEqual(response.status_code, 403, response.text)

        await self.assert_unpublished(request_id, document_id)

    async def test_other_patient_cannot_read_or_list(self):
        request_id, document_id = await self.staged()

        response = await self.publish(document_id, request_id)
        self.assertEqual(response.status_code, 201, response.text)

        self.actor_id = self.other_patient_id

        for path in (
            f"/api/v1/reports/{response.json()['id']}",
            f"/api/v1/patients/{self.patient_id}/reports",
        ):
            denied = await self.client.get(path)
            self.assertEqual(denied.status_code, 403, denied.text)

    async def test_document_bound_to_another_request_is_rejected(self):
        original_request, document_id = await self.staged()
        another_request = await self.create_request()

        response = await self.publish(document_id, another_request)
        self.assertEqual(response.status_code, 404, response.text)

        await self.assert_unpublished(original_request, document_id)

    async def test_patient_cannot_publish_same_document_twice(self):
        self.actor_id = self.patient_id

        uploaded = await self.upload()
        self.assertEqual(uploaded.status_code, 201, uploaded.text)

        document_id = UUID(uploaded.json()["id"])

        first = await self.publish(document_id)
        self.assertEqual(first.status_code, 201, first.text)

        repeat = await self.publish(document_id)
        self.assertEqual(repeat.status_code, 409, repeat.text)

        self.assertEqual(
            await self.count(Report, Report.document_id == document_id),
            1,
        )

    async def test_unknown_request_is_denied(self):
        request_id, document_id = await self.staged()

        response = await self.publish(document_id, uuid4())
        self.assertEqual(response.status_code, 403, response.text)

        await self.assert_unpublished(request_id, document_id)

    async def test_publish_audit_failure_rolls_back_and_retry_succeeds(self):
        request_id, document_id = await self.staged()
        key = f"ml116-{uuid4()}"

        with patch.object(
            report_service,
            "append_event",
            side_effect=RuntimeError("synthetic"),
        ):
            response = await self.publish(document_id, request_id, key=key)

        self.assertEqual(response.status_code, 500, response.text)

        await self.assert_unpublished(request_id, document_id)

        self.assertEqual(
            (await self.db.get(LabUploadRequest, request_id)).status,
            "active",
        )

        count = await self.db.scalar(
            text(
                "SELECT count(*) FROM api_idempotency_records "
                "WHERE idempotency_key = :key"
            ),
            {"key": key},
        )
        self.assertEqual(count, 0)

        retry = await self.publish(document_id, request_id, key=key)
        self.assertEqual(retry.status_code, 201, retry.text)

    async def test_immediate_upload_audit_failure_rolls_back_and_cleans_blob(self):
        request_id = await self.create_request()

        with patch.object(
            document_service,
            "_audit",
            side_effect=RuntimeError("synthetic"),
        ):
            response = await self.upload(request_id=request_id)

        self.assertEqual(response.status_code, 500, response.text)

        self.assertEqual(
            await self.count(Document, Document.patient_user_id == self.patient_id),
            0,
        )
        self.assertEqual(
            await self.count(Report, Report.patient_user_id == self.patient_id),
            0,
        )
        self.assertEqual(
            await self.count(Notification, Notification.user_id == self.patient_id),
            0,
        )
        self.assertEqual(
            (await self.db.get(LabUploadRequest, request_id)).status,
            "active",
        )

        self.storage["delete"].assert_awaited_once()
