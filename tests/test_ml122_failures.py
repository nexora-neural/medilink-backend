from io import BytesIO
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from fastapi import UploadFile

from app.modules.documents import service
from app.modules.laboratory import report_service
from app.modules.laboratory.report_schemas import ReportInput


class UploadFailureTests(IsolatedAsyncioTestCase):
    async def test_flush_failure(self):
        await self.check_failure("flush")

    async def test_ambiguous_commit(self):
        await self.check_failure("commit")

    async def check_failure(self, failure_stage):
        patient = uuid4()
        actor = SimpleNamespace(id=patient, status="active")
        db = AsyncMock()
        db.add = Mock()
        db.scalar.return_value = patient
        getattr(db, failure_stage).side_effect = RuntimeError("injected failure")
        file = UploadFile(file=BytesIO(b"file"), filename="report.pdf")
        with (
            patch.object(service, "_has_role", new=AsyncMock(return_value=True)),
            patch.object(service, "_audit", new=AsyncMock()),
            patch.object(
                service.storage, "save", new=AsyncMock(return_value=("a" * 32, 4, "b" * 64))
            ),
            patch.object(service.storage, "delete", new=AsyncMock()) as delete,
        ):
            with self.assertRaisesRegex(RuntimeError, "injected failure"):
                await service.upload_document(
                    db,
                    actor,
                    file,
                    "application/pdf",
                    "report",
                    "request",
                    patient_id=patient,
                )
        db.rollback.assert_awaited_once()
        if failure_stage == "flush":
            delete.assert_awaited_once_with("a" * 32)
        else:
            delete.assert_not_awaited()


class ReportFailureTests(IsolatedAsyncioTestCase):
    async def test_missing_file_cannot_be_published(self):
        patient = uuid4()
        document_id = uuid4()
        db = AsyncMock()
        doc = SimpleNamespace(
            id=document_id,
            owner_user_id=patient,
            patient_user_id=patient,
            uploaded_by=patient,
            document_type="report",
            status="active",
            source="patient",
            staged_report_request_id=None,
            storage_key="a" * 32,
        )
        with (
            patch.object(report_service, "require_patient", new=AsyncMock()),
            patch.object(report_service, "lock_and_replay", new=AsyncMock(return_value=None)),
            patch.object(
                report_service.documents, "document_by_id", new=AsyncMock(return_value=doc)
            ),
            patch.object(
                report_service.documents, "report_for_document", new=AsyncMock(return_value=None)
            ),
            patch.object(
                report_service.storage, "get_download_path", side_effect=FileNotFoundError
            ),
        ):
            with self.assertRaises(report_service.LabUploadRequestError) as error:
                await report_service.publish_report(
                    db,
                    SimpleNamespace(id=patient),
                    patient,
                    ReportInput(document_id=document_id, report_type="FBC"),
                    "12345678-1234-4234-8234-123456789012",
                    "request",
                )
        assert error.exception.status_code == 409
        db.commit.assert_not_awaited()
        db.rollback.assert_awaited_once()
