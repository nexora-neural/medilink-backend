"""ML-40 contract and regression checks (no live database required)."""

from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from pydantic import ValidationError

from app.modules.verification import service
from app.modules.verification.schemas import (
    ProfessionalProfileInput,
    VerificationApplicationInput,
    VerificationApplicationsReadPage,
)


class ContractTests(TestCase):
    def test_application_required_fields(self):
        with self.assertRaises(ValidationError):
            VerificationApplicationInput()

    def test_application_role_and_evidence(self):
        data = VerificationApplicationInput(
            role="doctor",
            registration_number="SLMC-123",
            evidence_document_ids=[uuid4()],
        )
        self.assertEqual(data.role.value, "doctor")
        self.assertEqual(len(data.evidence_document_ids), 1)

    def test_duplicate_evidence_rejected(self):
        doc_id = uuid4()
        with self.assertRaises(ValidationError):
            VerificationApplicationInput(
                role="doctor",
                registration_number="SLMC-123",
                evidence_document_ids=[doc_id, doc_id],
            )

    def test_empty_evidence_rejected(self):
        with self.assertRaises(ValidationError):
            VerificationApplicationInput(
                role="doctor",
                registration_number="SLMC-123",
                evidence_document_ids=[],
            )

    def test_max_lengths(self):
        with self.assertRaises(ValidationError):
            ProfessionalProfileInput(role="doctor", registration_number="x" * 101)
        with self.assertRaises(ValidationError):
            ProfessionalProfileInput(role="doctor", specialty="x" * 151)

    def test_pagination_contract(self):
        result = VerificationApplicationsReadPage(items=[], page=1, page_size=20, total=0)
        self.assertEqual(result.total, 0)


class ServiceTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.user = SimpleNamespace(id=uuid4(), status="pending")
        self.request = SimpleNamespace(state=SimpleNamespace(request_id="test-request"))
        self.db = MagicMock()
        self.db.scalar = AsyncMock(return_value=self.user.id)
        self.db.commit = AsyncMock()
        self.db.rollback = AsyncMock()
        self.db.flush = AsyncMock()
        self.db.refresh = AsyncMock()

    async def test_pending_applicant_role_mismatch(self):
        profile = SimpleNamespace(role="doctor", verification_status="not_submitted")
        payload = VerificationApplicationInput(
            role="pharmacist",
            registration_number="REG-1",
            evidence_document_ids=[uuid4()],
        )
        with patch.object(
            service.repository, "get_professional_profile", AsyncMock(return_value=profile)
        ):
            with self.assertRaises(service.AuthenticationError) as ctx:
                await service.submit_application(self.db, self.user, self.request, payload)
        self.assertEqual(ctx.exception.status_code, 409)
        self.db.commit.assert_not_awaited()

    async def test_assigned_role_mismatch(self):
        with patch.object(
            service.repository,
            "get_assigned_professional_roles",
            AsyncMock(return_value={"doctor"}),
        ):
            with self.assertRaises(service.AuthenticationError) as ctx:
                await service._ensure_role_consistency(
                    self.db, self.user, self.request, "pharmacist"
                )
        self.assertEqual(ctx.exception.status_code, 409)

    async def test_pending_applicant_without_professional_assignment_is_allowed(self):
        with patch.object(
            service.repository, "get_assigned_professional_roles", AsyncMock(return_value=set())
        ):
            await service._ensure_role_consistency(self.db, self.user, self.request, "doctor")

    async def test_other_users_evidence_rejected(self):
        profile = SimpleNamespace(
            role="doctor", verification_status="not_submitted", registration_number="SLMC-1"
        )
        document_id = uuid4()
        payload = VerificationApplicationInput(
            role="doctor",
            registration_number="SLMC-1",
            evidence_document_ids=[document_id],
        )
        with (
            patch.object(
                service.repository, "get_professional_profile", AsyncMock(return_value=profile)
            ),
            patch.object(
                service.repository, "get_assigned_professional_roles", AsyncMock(return_value=set())
            ),
            patch.object(service.repository, "get_user_applications", AsyncMock(return_value=[])),
            patch.object(service.repository, "get_owned_evidence", AsyncMock(return_value=None)),
        ):
            with self.assertRaises(service.AuthenticationError) as ctx:
                await service.submit_application(self.db, self.user, self.request, payload)
        self.assertEqual(ctx.exception.status_code, 404)
        self.db.commit.assert_not_awaited()

    async def test_paged_list(self):
        with patch.object(
            service.repository, "get_user_applications_page", AsyncMock(return_value=([], 0))
        ) as get_page:
            result = await service.list_own_applications(
                self.db, self.user, self.request, page=2, page_size=10
            )
        self.assertEqual((result.page, result.page_size, result.total), (2, 10, 0))
        get_page.assert_awaited_once_with(self.db, self.user.id, page=2, page_size=10)
