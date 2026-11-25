"""Doctor prescription issue service orchestration tests."""

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from app.modules.identity.auth import AuthenticationError
from app.modules.prescriptions import service
from app.modules.prescriptions.schemas import PrescriptionIssueItemInput


class PrescriptionIssueServiceTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.user = SimpleNamespace(id=uuid4())
        self.request = SimpleNamespace(state=SimpleNamespace(request_id="synthetic-issue-test"))
        self.session = SimpleNamespace(
            flush=AsyncMock(),
            commit=AsyncMock(),
            rollback=AsyncMock(),
        )

        self.prescription = SimpleNamespace(
            id=uuid4(),
            patient_user_id=uuid4(),
            prescriber_user_id=self.user.id,
            consultation_id=uuid4(),
            source="doctor",
            status="draft",
            issued_at=None,
            previous_prescription_id=None,
            original_document_id=None,
            starts_on=date(2026, 11, 25),
            ends_on=date(2026, 11, 27),
            updated_at=None,
            version=1,
        )

        self.item = PrescriptionIssueItemInput(
            medicine_name_snapshot="Synthetic medicine",
            dose_text="Synthetic dose",
            route="Oral",
            frequency="Synthetic frequency",
            duration_days=3,
            quantity="3",
            quantity_unit="tablet",
            is_prn=False,
        )
        self.detail = SimpleNamespace(items=[self.item])

        self.lock = self.enterContext(
            patch.object(
                service.repository,
                "lock_prescription",
                AsyncMock(return_value=self.prescription),
            )
        )
        self.access = self.enterContext(
            patch.object(
                service,
                "require_prescription_access",
                AsyncMock(return_value="doctor"),
            )
        )
        self.build_detail = self.enterContext(
            patch.object(
                service,
                "build_prescription_detail",
                AsyncMock(return_value=self.detail),
            )
        )
        self.references = self.enterContext(
            patch.object(
                service,
                "validate_draft_references",
                AsyncMock(),
            )
        )
        self.audit = self.enterContext(patch.object(service, "append_event", AsyncMock()))

    async def issue(self):
        return await service.issue_prescription(
            session=self.session,
            user=self.user,
            request=self.request,
            prescription_id=self.prescription.id,
        )

    async def test_complete_owned_draft_is_issued_and_audited(self):
        result = await self.issue()

        self.assertIs(result, self.detail)
        self.assertEqual(self.prescription.status, "active")
        self.assertIsNotNone(self.prescription.issued_at)
        self.assertEqual(self.prescription.version, 2)

        self.access.assert_awaited_once_with(
            self.session,
            user=self.user,
            request=self.request,
            patient_id=self.prescription.patient_user_id,
            consultation_id=self.prescription.consultation_id,
            clinical_write=True,
        )
        self.references.assert_awaited_once()
        self.session.flush.assert_awaited_once()
        self.audit.assert_awaited_once()

        event = self.audit.await_args.kwargs
        self.assertEqual(event["action"], "prescription.issued")
        self.assertEqual(event["actor_user_id"], self.user.id)
        self.assertEqual(event["metadata"]["previous_version"], 1)
        self.assertEqual(event["metadata"]["new_version"], 2)

        self.session.commit.assert_awaited_once()
        self.session.rollback.assert_not_awaited()

    async def test_retry_does_not_issue_or_audit_twice(self):
        await self.issue()
        issued_at = self.prescription.issued_at

        await self.issue()

        self.assertEqual(self.prescription.version, 2)
        self.assertEqual(self.prescription.issued_at, issued_at)
        self.assertEqual(self.access.await_count, 2)
        self.audit.assert_awaited_once()
        self.session.flush.assert_awaited_once()

    async def test_another_doctor_cannot_issue(self):
        self.prescription.prescriber_user_id = uuid4()

        with self.assertRaises(AuthenticationError) as caught:
            await self.issue()

        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(self.prescription.status, "draft")
        self.audit.assert_not_awaited()
        self.session.commit.assert_not_awaited()
        self.session.rollback.assert_awaited_once()

    async def test_patient_source_cannot_be_doctor_issued(self):
        self.prescription.source = "patient"

        with self.assertRaises(AuthenticationError) as caught:
            await self.issue()

        self.assertEqual(caught.exception.status_code, 403)
        self.audit.assert_not_awaited()
        self.session.commit.assert_not_awaited()

    async def test_denied_permission_stops_issuance(self):
        self.access.side_effect = service.forbidden(self.request)

        with self.assertRaises(AuthenticationError) as caught:
            await self.issue()

        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(self.prescription.status, "draft")
        self.assertIsNone(self.prescription.issued_at)
        self.audit.assert_not_awaited()
        self.session.commit.assert_not_awaited()
        self.session.rollback.assert_awaited_once()

    async def test_incomplete_content_returns_422_without_mutation(self):
        self.prescription.starts_on = None

        with self.assertRaises(AuthenticationError) as caught:
            await self.issue()

        self.assertEqual(caught.exception.status_code, 422)
        self.assertIn(
            "starts_on",
            caught.exception.detail["field_errors"],
        )
        self.assertEqual(self.prescription.status, "draft")
        self.assertEqual(self.prescription.version, 1)
        self.session.flush.assert_not_awaited()
        self.audit.assert_not_awaited()
        self.session.commit.assert_not_awaited()
        self.session.rollback.assert_awaited_once()

    async def test_cancelled_prescription_cannot_be_issued(self):
        self.prescription.status = "cancelled"
        self.prescription.issued_at = datetime.now(UTC)

        with self.assertRaises(AuthenticationError) as caught:
            await self.issue()

        self.assertEqual(caught.exception.status_code, 409)
        self.audit.assert_not_awaited()
        self.session.commit.assert_not_awaited()

    async def test_audit_failure_prevents_commit(self):
        self.audit.side_effect = RuntimeError("Synthetic audit failure")

        with self.assertRaises(RuntimeError):
            await self.issue()

        self.session.commit.assert_not_awaited()
        self.session.rollback.assert_awaited_once()
