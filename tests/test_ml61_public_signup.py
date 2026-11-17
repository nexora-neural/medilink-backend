"""ML-61 regression checks: public onboarding never grants privileged roles.

These tests need no running PostgreSQL or Firebase account. They exercise the
real FastAPI route and Pydantic schema while overriding external dependencies.
"""

from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.database.session import get_session
from app.main import app
from app.modules.identity.auth import get_verified_identity
from app.modules.identity.request_limits import require_authenticated_rate_limit
from app.modules.profiles.schemas import OnboardRequest
from app.modules.profiles.service import onboard_identity

ONBOARD = "/api/v1/me/onboard"


class PublicRoleSchemaTests(TestCase):
    def test_rejects_privileged_roles_even_without_patient_fields(self):
        for role in ("admin", "super_admin"):
            with self.subTest(role=role), self.assertRaises(ValidationError):
                OnboardRequest.model_validate(
                    {"display_name": "Synthetic ML61 User", "requested_role": role}
                )

    def test_rejects_attempted_privilege_fields_in_body(self):
        valid_patient = {
            "display_name": "Synthetic ML61 Patient",
            "requested_role": "patient",
            "date_of_birth": "2000-01-01",
            "sex": "female",
        }
        for injected in ({"role": "super_admin"}, {"roles": ["admin"]},
                         {"is_super_admin": True}, {"firebase_uid": "forged-uid"}):
            with self.subTest(injected=injected), self.assertRaises(ValidationError):
                OnboardRequest.model_validate({**valid_patient, **injected})

    def test_public_roles_still_validate(self):
        for role in ("patient", "doctor", "pharmacist", "lab_staff"):
            with self.subTest(role=role):
                body = {"display_name": "Synthetic ML61 User", "requested_role": role}
                if role == "patient":
                    body.update(date_of_birth="2000-01-01", sex="female")
                self.assertEqual(OnboardRequest.model_validate(body).requested_role, role)


class PublicOnboardingRouteTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_overrides = app.dependency_overrides.copy()
        self.addCleanup(self.restore_overrides)

        async def verified_identity():
            return {"uid": "synthetic-ml61", "email": "synthetic@example.invalid"}

        async def isolated_session():
            # The test fails if an invalid payload reaches the service.
            yield AsyncMock()

        async def skip_rate_limit():
            return None

        app.dependency_overrides.update(
            {
                get_verified_identity: verified_identity,
                get_session: isolated_session,
                require_authenticated_rate_limit: skip_rate_limit,
            }
        )
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)

    def restore_overrides(self):
        app.dependency_overrides.clear()
        app.dependency_overrides.update(self.original_overrides)

    async def assert_rejected_before_persistence(self, body: dict):
        with (
            patch("app.modules.profiles.router.onboard_identity", new_callable=AsyncMock) as save,
            patch("app.modules.profiles.router.append_event", new_callable=AsyncMock) as audit,
            patch("app.main.record_api_request", new_callable=AsyncMock),
        ):
            response = await self.client.post(ONBOARD, json=body)

        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["code"], "VALIDATION_ERROR")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-request-id"], response.json()["request_id"])
        save.assert_not_awaited()
        audit.assert_not_awaited()

    async def test_http_rejects_admin_and_super_admin(self):
        for role in ("admin", "super_admin"):
            with self.subTest(role=role):
                await self.assert_rejected_before_persistence(
                    {"display_name": "Synthetic ML61 User", "requested_role": role}
                )

    async def test_http_rejects_forged_privileged_fields(self):
        patient = {
            "display_name": "Synthetic ML61 Patient",
            "requested_role": "patient",
            "date_of_birth": "2000-01-01",
            "sex": "female",
        }
        await self.assert_rejected_before_persistence(
            {**patient, "roles": ["patient", "super_admin"]}
        )

    async def test_service_defense_in_depth_blocks_bypassed_schema(self):
        # Pydantic's model_construct() deliberately bypasses normal validation.
        # Internal callers still must not be able to grant a privileged role.
        for role in ("admin", "super_admin"):
            with self.subTest(role=role):
                session = AsyncMock()
                untrusted = OnboardRequest.model_construct(
                    display_name="Synthetic ML61 User", requested_role=role
                )
                with self.assertRaises(HTTPException) as caught:
                    await onboard_identity(session, {"uid": "synthetic-ml61"}, untrusted)
                self.assertEqual(caught.exception.status_code, 403)
                session.execute.assert_not_awaited()
                session.scalar.assert_not_awaited()
