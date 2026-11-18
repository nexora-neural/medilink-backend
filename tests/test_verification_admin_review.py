"""ML-48 administrative verification review contract and service checks."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError

from app.main import app
from app.modules.identity.models import UserRole
from app.modules.verification import service
from app.modules.verification.models import VerificationDecision
from app.modules.verification.schemas import (
    VerificationDecisionInput,
    VerificationDecisionRead,
    VerificationDecisionsReadPage,
)


class VerificationAdminContractTests(TestCase):
    def test_decision_input_requires_a_valid_status_and_nonblank_reason(self):
        valid = VerificationDecisionInput(status="approved", reason="SLMC record reviewed")
        self.assertEqual(valid.status, "approved")
        self.assertEqual(valid.reason, "SLMC record reviewed")

        for payload in (
            {"status": "suspended", "reason": "Review complete"},
            {"status": "approved", "reason": "  "},
            {"status": "approved", "reason": "r" * 1001},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                VerificationDecisionInput(**payload)

    def test_decision_history_response_serializes_contract_fields(self):
        decision = VerificationDecisionRead(
            id=uuid4(),
            application_id=uuid4(),
            reviewer_user_id=uuid4(),
            decision="rejected",
            reason="Registration could not be confirmed",
            decided_at=datetime.now(UTC),
        )
        page = VerificationDecisionsReadPage(
            items=[decision], page=1, page_size=20, total=1
        ).model_dump(mode="json")
        self.assertEqual(page["items"][0]["decision"], "rejected")
        self.assertEqual(page["total"], 1)
        self.assertIn("reviewer_user_id", page["items"][0])

    def test_all_admin_review_contract_routes_are_registered(self):
        routes = {
            (path, method.upper())
            for path, operations in app.openapi()["paths"].items()
            for method in operations
        }
        expected = {
            ("/api/v1/admin/verification-applications", "GET"),
            ("/api/v1/admin/verification-applications/{application_id}", "GET"),
            ("/api/v1/admin/verification-applications/{application_id}/decision", "POST"),
            ("/api/v1/admin/verification-applications/{application_id}/decisions", "GET"),
        }
        self.assertTrue(expected <= routes)


class VerificationAdminServiceTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.reviewer = SimpleNamespace(id=uuid4(), status="active")
        self.applicant_id = uuid4()
        self.application_id = uuid4()
        self.request = SimpleNamespace(state=SimpleNamespace(request_id="ml48-test"))
        self.application = SimpleNamespace(
            id=self.application_id,
            user_id=self.applicant_id,
            organization_id=None,
            role="doctor",
            registration_number="SLMC-123",
            status="pending",
            submitted_at=datetime.now(UTC),
            reviewed_by=None,
            reviewed_at=None,
            review_reason=None,
        )
        self.profile = SimpleNamespace(
            role="doctor",
            registration_number="SLMC-123",
            verification_status="pending",
        )
        self.db = MagicMock()
        self.db.scalar = AsyncMock(return_value=1)
        self.db.get = AsyncMock(return_value=None)
        self.db.add = MagicMock()
        self.db.flush = AsyncMock()
        self.db.refresh = AsyncMock()
        self.db.commit = AsyncMock()
        self.db.rollback = AsyncMock()

    async def test_admin_guard_requires_an_active_account_and_current_admin_role(self):
        with patch.object(service, "select") as select:
            # Keep the SQL expression opaque while asserting both account and role checks.
            query = select.return_value.join.return_value.join.return_value.where.return_value
            query.limit.return_value = "admin-query"
            self.db.scalar.return_value = 1
            await service._require_admin(self.db, self.reviewer, self.request)
            self.db.scalar.assert_awaited_once_with("admin-query")

        inactive = SimpleNamespace(id=uuid4(), status="suspended")
        self.db.scalar.reset_mock()
        with self.assertRaises(HTTPException) as raised:
            await service._require_admin(self.db, inactive, self.request)
        self.assertEqual(raised.exception.status_code, 403)
        self.db.scalar.assert_not_awaited()

        self.db.scalar.return_value = None
        with self.assertRaises(HTTPException) as raised:
            await service._require_admin(self.db, self.reviewer, self.request)
        self.assertEqual(raised.exception.status_code, 403)

    async def test_decision_approval_persists_role_history_and_audit_atomically(self):
        payload = VerificationDecisionInput(
            status="approved", reason="SLMC record manually reviewed"
        )
        with (
            patch.object(service, "_require_admin", new_callable=AsyncMock),
            patch.object(
                service.repository,
                "get_application",
                new_callable=AsyncMock,
                return_value=self.application,
            ),
            patch.object(
                service.repository,
                "get_professional_profile",
                new_callable=AsyncMock,
                return_value=self.profile,
            ),
            patch.object(
                service.repository,
                "get_application_documents",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch.object(service, "_audit", new_callable=AsyncMock) as audit,
        ):
            result = await service.decide_application(
                self.db, self.reviewer, self.request, self.application_id, payload
            )

        self.assertEqual(result.status, "approved")
        self.assertEqual(result.reviewed_by, self.reviewer.id)
        self.assertEqual(result.review_reason, payload.reason)
        self.assertEqual(self.profile.verification_status, "approved")
        role_assignment = next(
            item for item in (call.args[0] for call in self.db.add.call_args_list)
            if isinstance(item, UserRole)
        )
        self.assertEqual(role_assignment.user_id, self.applicant_id)
        self.assertEqual(role_assignment.role_id, 1)
        self.assertEqual(role_assignment.assigned_by, self.reviewer.id)
        history = next(
            item for item in (call.args[0] for call in self.db.add.call_args_list)
            if isinstance(item, VerificationDecision)
        )
        self.assertEqual(history.reviewer_user_id, self.reviewer.id)
        self.assertEqual(history.reason, payload.reason)
        self.assertEqual(history.decision, "approved")
        audit.assert_awaited_once()
        self.assertNotIn("reason", audit.await_args.args[6])
        self.db.commit.assert_awaited_once()

    async def test_rejection_records_history_without_granting_professional_role(self):
        payload = VerificationDecisionInput(status="rejected", reason="Evidence did not match")
        with (
            patch.object(service, "_require_admin", new_callable=AsyncMock),
            patch.object(
                service.repository,
                "get_application",
                new_callable=AsyncMock,
                return_value=self.application,
            ),
            patch.object(
                service.repository,
                "get_professional_profile",
                new_callable=AsyncMock,
                return_value=self.profile,
            ),
            patch.object(
                service.repository,
                "get_application_documents",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch.object(service, "_audit", new_callable=AsyncMock),
        ):
            result = await service.decide_application(
                self.db, self.reviewer, self.request, self.application_id, payload
            )

        self.assertEqual(result.status, "rejected")
        self.assertEqual(self.profile.verification_status, "rejected")
        self.db.get.assert_not_awaited()
        self.db.commit.assert_awaited_once()
        self.assertTrue(
            any(
                isinstance(call.args[0], VerificationDecision)
                for call in self.db.add.call_args_list
            )
        )

    async def test_reviewer_cannot_decide_own_application(self):
        self.application.user_id = self.reviewer.id
        with (
            patch.object(service, "_require_admin", new_callable=AsyncMock),
            patch.object(
                service.repository,
                "get_application",
                new_callable=AsyncMock,
                return_value=self.application,
            ),
        ):
            with self.assertRaises(HTTPException) as raised:
                await service.decide_application(
                    self.db,
                    self.reviewer,
                    self.request,
                    self.application_id,
                    VerificationDecisionInput(status="approved", reason="Review"),
                )
        self.assertEqual(raised.exception.status_code, 403)
        self.db.rollback.assert_awaited_once()
        self.db.commit.assert_not_awaited()

    async def test_only_pending_applications_can_be_decided(self):
        self.application.status = "approved"
        with (
            patch.object(service, "_require_admin", new_callable=AsyncMock),
            patch.object(
                service.repository,
                "get_application",
                new_callable=AsyncMock,
                return_value=self.application,
            ),
        ):
            with self.assertRaises(HTTPException) as raised:
                await service.decide_application(
                    self.db,
                    self.reviewer,
                    self.request,
                    self.application_id,
                    VerificationDecisionInput(status="rejected", reason="Review"),
                )
        self.assertEqual(raised.exception.status_code, 409)
        self.db.rollback.assert_awaited_once()
        self.db.commit.assert_not_awaited()

    async def test_audit_failure_rolls_back_decision_and_history(self):
        with (
            patch.object(service, "_require_admin", new_callable=AsyncMock),
            patch.object(
                service.repository,
                "get_application",
                new_callable=AsyncMock,
                return_value=self.application,
            ),
            patch.object(
                service.repository,
                "get_professional_profile",
                new_callable=AsyncMock,
                return_value=self.profile,
            ),
            patch.object(
                service,
                "_audit",
                new_callable=AsyncMock,
                side_effect=RuntimeError("audit failure"),
            ),
        ):
            with self.assertRaises(RuntimeError):
                await service.decide_application(
                    self.db,
                    self.reviewer,
                    self.request,
                    self.application_id,
                    VerificationDecisionInput(status="rejected", reason="Review"),
                )
        self.db.rollback.assert_awaited_once()
        self.db.commit.assert_not_awaited()

    async def test_decision_history_is_bounded_and_returned_in_pages(self):
        history = SimpleNamespace(
            id=uuid4(),
            application_id=self.application_id,
            reviewer_user_id=self.reviewer.id,
            decision="more_info_requested",
            reason="Please provide a clearer certificate",
            decided_at=datetime.now(UTC),
        )
        with (
            patch.object(service, "_require_admin", new_callable=AsyncMock),
            patch.object(
                service.repository,
                "get_application",
                new_callable=AsyncMock,
                return_value=self.application,
            ),
            patch.object(
                service.repository,
                "get_application_decisions_page",
                new_callable=AsyncMock,
                return_value=([history], 1),
            ) as get_history,
        ):
            result = await service.list_application_decisions(
                self.db,
                self.reviewer,
                self.request,
                self.application_id,
                page=2,
                page_size=10,
            )
        self.assertEqual((result.page, result.page_size, result.total), (2, 10, 1))
        self.assertEqual(result.items[0].reason, history.reason)
        get_history.assert_awaited_once_with(
            self.db, self.application_id, page=2, page_size=10
        )
