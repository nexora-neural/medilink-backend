"""ML-124 permissions, validation and transaction regression tests."""

from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError

from app.modules.laboratory import report_corrections as service
from app.modules.laboratory.report_schemas import ReportReasonInput, ReportVersionInput
from app.modules.laboratory.service import LabUploadRequestError


class ReportCorrectionSchemaTests(TestCase):
    def test_reason_required_and_bounded(self):
        for reason in ("", "   ", "x" * 1001, "bad\x00reason"):
            with self.subTest(reason=reason[:20]), self.assertRaises(ValidationError):
                ReportReasonInput(reason=reason)
        self.assertEqual(ReportReasonInput(reason=" corrected ").reason, "corrected")

    def test_version_requires_document_type_and_reason(self):
        with self.assertRaises(ValidationError):
            ReportVersionInput(document_id=uuid4(), report_type="FBC")


class ReportCorrectionPermissions(IsolatedAsyncioTestCase):
    def setUp(self):
        self.actor = SimpleNamespace(id=uuid4())
        self.report = SimpleNamespace(
            id=uuid4(),
            patient_user_id=uuid4(),
            laboratory_id=None,
            source="patient",
            status="active",
        )
        self.db = SimpleNamespace(rollback=AsyncMock(), commit=AsyncMock())

    async def test_patient_cannot_correct_another_patient(self):
        with self.assertRaises(HTTPException) as exc:
            await service._require_editor(self.db, self.actor, self.report)
        self.assertEqual(exc.exception.status_code, 403)

    async def test_owner_patient_role_is_rechecked(self):
        self.report.patient_user_id = self.actor.id
        with patch.object(service, "require_patient", new_callable=AsyncMock) as check:
            await service._require_editor(self.db, self.actor, self.report)
        check.assert_awaited_once_with(self.db, self.actor)

    async def test_original_lab_membership_is_rechecked(self):
        self.report.source = "lab_staff"
        self.report.laboratory_id = uuid4()
        with patch.object(service, "require_verified_membership", new_callable=AsyncMock) as check:
            await service._require_editor(self.db, self.actor, self.report)
        check.assert_awaited_once_with(self.db, self.actor, "lab_staff", self.report.laboratory_id)

    async def test_patient_cannot_edit_lab_report_using_ownership(self):
        self.actor.id = self.report.patient_user_id
        self.report.source = "lab_staff"
        self.report.laboratory_id = uuid4()
        with patch.object(
            service,
            "require_verified_membership",
            new_callable=AsyncMock,
            side_effect=HTTPException(403),
        ):
            with self.assertRaises(HTTPException):
                await service._require_editor(self.db, self.actor, self.report)

    async def test_doctor_source_never_grants_editor_access(self):
        self.report.source = "doctor"
        with self.assertRaises(HTTPException):
            await service._require_editor(self.db, self.actor, self.report)

    async def test_withdrawal_failure_rolls_back(self):
        with patch.object(service, "_report", new_callable=AsyncMock, return_value=self.report):
            with self.assertRaises(HTTPException):
                await service.withdraw_report(self.db, self.actor, self.report.id, "wrong", "test")
        self.db.rollback.assert_awaited_once()
        self.db.commit.assert_not_awaited()

    def test_only_active_reports_can_change(self):
        for status in ("withdrawn", "superseded"):
            self.report.status = status
            with self.assertRaises(LabUploadRequestError) as exc:
                service._require_active(self.report)
            self.assertEqual(exc.exception.status_code, 409)

    async def test_replay_rechecks_permission_before_returning(self):
        body = ReportVersionInput(document_id=uuid4(), report_type="FBC", reason="Correction")
        with (
            patch.object(service, "_report", new_callable=AsyncMock, return_value=self.report),
            patch.object(service, "lock_and_replay", new_callable=AsyncMock) as replay,
        ):
            with self.assertRaises(HTTPException):
                await service.create_version(
                    self.db, self.actor, self.report.id, body, "retry-key", "test"
                )
        replay.assert_not_awaited()
        self.db.rollback.assert_awaited_once()
