
"""Regression tests for ML-36 laboratory document persistence."""

import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from app.modules.documents.errors import DocumentError
from app.modules.documents.service import _persist_lab_report
from app.modules.laboratory.models import (
    Report,
    ReportIdentity,
    ReportStatus,
    UploadRequestStatus,
)


class TestLaboratoryReportPersistence(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.db = MagicMock()
        self.db.scalar = AsyncMock()
        self.db.flush = AsyncMock()

        self.user = SimpleNamespace(id=uuid4())
        self.patient_id = uuid4()
        self.doc = SimpleNamespace(id=uuid4())

    async def test_lab_upload_creates_report_and_consumes_request(self):
        request_id = uuid4()

        req = SimpleNamespace(
            id=request_id,
            patient_user_id=self.patient_id,
            laboratory_id=uuid4(),
            status=UploadRequestStatus.ACTIVE,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            used_at=None,
        )

        with patch(
            "app.modules.documents.service.repository.lab_request",
            new_callable=AsyncMock,
            return_value=req,
        ):
            await _persist_lab_report(
                self.db,
                self.user,
                self.doc,
                patient_id=self.patient_id,
                upload_request_id=request_id,
                supersedes_id=None,
                correction_reason=None,
            )

        records = [call.args[0] for call in self.db.add.call_args_list]

        identity = next(
            record for record in records if isinstance(record, ReportIdentity)
        )
        report = next(
            record for record in records if isinstance(record, Report)
        )

        self.assertEqual(report.document_id, self.doc.id)
        self.assertEqual(report.patient_user_id, self.patient_id)
        self.assertEqual(report.report_identity_id, identity.id)
        self.assertEqual(report.status, ReportStatus.ACTIVE)
        self.assertEqual(req.status, UploadRequestStatus.USED)
        self.assertIsNotNone(req.used_at)

    async def test_used_upload_request_is_rejected(self):
        req = SimpleNamespace(
            id=uuid4(),
            patient_user_id=self.patient_id,
            laboratory_id=uuid4(),
            status=UploadRequestStatus.USED,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        with patch(
            "app.modules.documents.service.repository.lab_request",
            new_callable=AsyncMock,
            return_value=req,
        ):
            with self.assertRaises(DocumentError) as context:
                await _persist_lab_report(
                    self.db,
                    self.user,
                    self.doc,
                    patient_id=self.patient_id,
                    upload_request_id=req.id,
                    supersedes_id=None,
                    correction_reason=None,
                )

        self.assertEqual(context.exception.status_code, 409)
        self.assertEqual(context.exception.code, "INVALID_STATE")
        self.db.add.assert_not_called()

    async def test_correction_preserves_report_lineage(self):
        previous_document_id = uuid4()

        previous = SimpleNamespace(
            id=uuid4(),
            patient_user_id=self.patient_id,
            laboratory_id=uuid4(),
            report_identity_id=uuid4(),
            report_type="report",
            report_date=None,
            notes=None,
            status=ReportStatus.ACTIVE,
        )

        self.db.scalar.return_value = previous

        await _persist_lab_report(
            self.db,
            self.user,
            self.doc,
            patient_id=self.patient_id,
            upload_request_id=None,
            supersedes_id=previous_document_id,
            correction_reason="Corrected laboratory result",
        )

        report = self.db.add.call_args.args[0]

        self.assertIsInstance(report, Report)
        self.assertEqual(report.document_id, self.doc.id)
        self.assertEqual(report.supersedes_id, previous.id)
        self.assertEqual(
            report.report_identity_id,
            previous.report_identity_id,
        )
        self.assertEqual(report.status, ReportStatus.ACTIVE)
        self.assertEqual(previous.status, ReportStatus.SUPERSEDED)

    async def test_correction_without_reason_is_rejected(self):
        with self.assertRaises(DocumentError) as context:
            await _persist_lab_report(
                self.db,
                self.user,
                self.doc,
                patient_id=self.patient_id,
                upload_request_id=None,
                supersedes_id=uuid4(),
                correction_reason=None,
            )

        self.assertEqual(context.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
