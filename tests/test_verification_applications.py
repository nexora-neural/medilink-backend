
"""Regression tests for ML-020 professional verification applications."""

from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.main import app
from app.modules.verification import service
from app.modules.verification.schemas import (
    ProfessionalProfileInput,
    VerificationApplicationInput,
)


class VerificationSchemaTests(TestCase):
    """Validate professional verification request payloads."""

    def test_valid_professional_roles(self):
        for role in ("doctor", "pharmacist", "lab_staff"):
            payload = ProfessionalProfileInput(role=role)
            self.assertEqual(payload.role.value, role)

    def test_invalid_professional_role(self):
        with self.assertRaises(ValidationError):
            ProfessionalProfileInput(role="admin")

    def test_unknown_profile_fields_rejected(self):
        with self.assertRaises(ValidationError):
            ProfessionalProfileInput(
                role="doctor",
                verification_status="approved",
            )

    def test_duplicate_evidence_rejected(self):
        document_id = uuid4()

        with self.assertRaises(ValidationError):
            VerificationApplicationInput(
                document_ids=[document_id, document_id]
            )

    def test_unknown_application_fields_rejected(self):
        with self.assertRaises(ValidationError):
            VerificationApplicationInput(
                status="approved",
                reviewed_by=str(uuid4()),
            )


class VerificationRouteTests(IsolatedAsyncioTestCase):
    """Check route registration and authentication."""

    async def test_missing_authentication(self):
        endpoints = (
            ("PUT", "/api/v1/me/professional-profile"),
            ("POST", "/api/v1/verification-applications"),
            ("GET", "/api/v1/me/verification-applications"),
        )

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            for method, path in endpoints:
                with self.subTest(method=method, path=path):
                    response = await client.request(
                        method,
                        path,
                        json={} if method != "GET" else None,
                    )

                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(
                        response.json()["code"],
                        "AUTHENTICATION_REQUIRED",
                    )
                    self.assertEqual(
                        response.headers["cache-control"],
                        "no-store",
                    )
                    self.assertTrue(
                        response.json()["request_id"]
                    )


class VerificationServiceTests(IsolatedAsyncioTestCase):
    """Verify application business rules."""

    def setUp(self):
        self.user = SimpleNamespace(
            id=uuid4(),
            status="active",
        )

        self.request = SimpleNamespace(
            state=SimpleNamespace(
                request_id=str(uuid4())
            )
        )

        self.db = MagicMock()
        self.db.scalar = AsyncMock(return_value=self.user.id)
        self.db.commit = AsyncMock()
        self.db.rollback = AsyncMock()
        self.db.flush = AsyncMock()
        self.db.refresh = AsyncMock()

    async def test_suspended_account_cannot_submit(self):
        self.user.status = "suspended"

        payload = VerificationApplicationInput()

        with self.assertRaises(service.AuthenticationError) as context:
            await service.submit_application(
                self.db,
                self.user,
                self.request,
                payload,
            )

        self.assertEqual(context.exception.status_code, 403)
        self.db.commit.assert_not_awaited()

    async def test_disabled_account_cannot_submit(self):
        self.user.status = "disabled"

        payload = VerificationApplicationInput()

        with self.assertRaises(service.AuthenticationError) as context:
            await service.submit_application(
                self.db,
                self.user,
                self.request,
                payload,
            )

        self.assertEqual(context.exception.status_code, 403)
        self.db.commit.assert_not_awaited()

    async def test_missing_professional_profile_rejected(self):
        payload = VerificationApplicationInput()

        with patch.object(
            service.repository,
            "get_professional_profile",
            new_callable=AsyncMock,
            return_value=None,
        ):
            with self.assertRaises(
                service.AuthenticationError
            ) as context:
                await service.submit_application(
                    self.db,
                    self.user,
                    self.request,
                    payload,
                )

        self.assertEqual(context.exception.status_code, 409)
        self.db.rollback.assert_awaited_once()
        self.db.commit.assert_not_awaited()

    async def test_duplicate_pending_application_rejected(self):
        profile = SimpleNamespace(
            role="doctor",
            verification_status="not_submitted",
            registration_number="SLMC-12345",
        )

        existing = SimpleNamespace(
            status="pending"
        )

        with (
            patch.object(
                service.repository,
                "get_professional_profile",
                new_callable=AsyncMock,
                return_value=profile,
            ),
            patch.object(
                service.repository,
                "get_user_applications",
                new_callable=AsyncMock,
                return_value=[existing],
            ),
        ):
            with self.assertRaises(
                service.AuthenticationError
            ) as context:
                await service.submit_application(
                    self.db,
                    self.user,
                    self.request,
                    VerificationApplicationInput(),
                )

        self.assertEqual(context.exception.status_code, 409)
        self.db.commit.assert_not_awaited()

    async def test_another_users_evidence_rejected(self):
        profile = SimpleNamespace(
            role="doctor",
            verification_status="not_submitted",
            registration_number="SLMC-12345",
        )

        document_id = uuid4()

        with (
            patch.object(
                service.repository,
                "get_professional_profile",
                new_callable=AsyncMock,
                return_value=profile,
            ),
            patch.object(
                service.repository,
                "get_user_applications",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch.object(
                service.repository,
                "get_owned_evidence",
                new_callable=AsyncMock,
                return_value=None,
            ) as evidence_lookup,
        ):
            with self.assertRaises(
                service.AuthenticationError
            ) as context:
                await service.submit_application(
                    self.db,
                    self.user,
                    self.request,
                    VerificationApplicationInput(
                        document_ids=[document_id]
                    ),
                )

        self.assertEqual(context.exception.status_code, 404)
        evidence_lookup.assert_awaited_once_with(
            self.db,
            document_id,
            self.user.id,
        )
        self.db.commit.assert_not_awaited()

    async def test_list_only_current_users_applications(self):
        with patch.object(
            service.repository,
            "get_user_applications",
            new_callable=AsyncMock,
            return_value=[],
        ) as lookup:
            result = await service.list_own_applications(
                self.db,
                self.user,
                self.request,
            )

        self.assertEqual(result.items, [])
        lookup.assert_awaited_once_with(
            self.db,
            self.user.id,
        )

    async def test_pending_account_can_view_applications(self):
        self.user.status = "pending"

        with patch.object(
            service.repository,
            "get_user_applications",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await service.list_own_applications(
                self.db,
                self.user,
                self.request,
            )

        self.assertEqual(result.items, [])
