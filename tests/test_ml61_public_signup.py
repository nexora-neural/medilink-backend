"""ML-61 regression checks: public onboarding never grants privileged roles.

These tests need no running PostgreSQL or Firebase account. They exercise the
real FastAPI route and Pydantic schema while overriding external dependencies.
"""

import os
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase, skipUnless
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.database.session import get_session
from app.main import app
from app.modules.identity.auth import get_verified_identity
from app.modules.identity.models import Role, User, UserRole
from app.modules.identity.request_limits import require_authenticated_rate_limit
from app.modules.profiles.models import ProfessionalProfile
from app.modules.profiles.schemas import OnboardRequest
from app.modules.profiles.service import onboard_identity

ONBOARD = "/api/v1/me/onboard"
POSTGRES_TEST_URL = os.getenv("ML61_PROFESSIONAL_TEST_DATABASE_URL")


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


class ProfessionalOnboardingSecurityTests(IsolatedAsyncioTestCase):
    async def test_professional_onboarding_does_not_assign_professional_role(self):
        for requested_role in ("doctor", "pharmacist", "lab_staff"):
            with self.subTest(role=requested_role):
                session = AsyncMock()
                session.add = MagicMock()

                user = SimpleNamespace(
                    id="synthetic-user-id",
                    status="active",
                )
                role = SimpleNamespace(
                    id="synthetic-role-id",
                    code=requested_role,
                )
                profile = SimpleNamespace(role=requested_role)

                insert_result = MagicMock()
                insert_result.scalar_one_or_none.return_value = None
                session.execute.return_value = insert_result

                session.scalar.side_effect = [user, role]
                session.get.side_effect = [None, profile]

                data = OnboardRequest.model_validate(
                    {
                        "display_name": "Synthetic Professional",
                        "requested_role": requested_role,
                        "professional_registration_number": "REG-123",
                        "workplace": "Demo Clinic",
                    }
                )

                with (
                    patch(
                        "app.modules.profiles.service.build_me_response",
                        new_callable=AsyncMock,
                    ) as build_response,
                    patch(
                        "app.modules.profiles.service.ProfessionalProfileResponse.model_validate",
                        return_value=MagicMock(),
                    ),
                ):
                    build_response.return_value = MagicMock()

                    await onboard_identity(
                        session,
                        {"uid": "synthetic-professional"},
                        data,
                    )

                # Only the User insert should execute.
                # No UserRole insert is allowed for pending applicants.
                session.execute.assert_awaited_once()

                # A professional profile must still be created.
                session.add.assert_called_once()

                created_profile = session.add.call_args.args[0]
                self.assertEqual(created_profile.role, requested_role)
                self.assertEqual(
                    created_profile.verification_status,
                    "not_submitted",
                )
                self.assertEqual(created_profile.registration_number, "REG-123")
                self.assertEqual(created_profile.workplace, "Demo Clinic")


@skipUnless(
    POSTGRES_TEST_URL,
    "Set ML61_PROFESSIONAL_TEST_DATABASE_URL to a dedicated migrated test database",
)
class ProfessionalOnboardingPostgreSQLTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine(POSTGRES_TEST_URL)
        self.addAsyncCleanup(self.engine.dispose)
        self.connection = await self.engine.connect()
        self.addAsyncCleanup(self.connection.close)
        self.transaction = await self.connection.begin()
        self.addAsyncCleanup(self.transaction.rollback)
        self.session = AsyncSession(
            bind=self.connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        self.addAsyncCleanup(self.session.close)
        self.uid = f"ml50-professional-{uuid4()}"
        role = await self.session.scalar(select(Role).where(Role.code == "doctor"))
        if role is None:
            self.session.add(Role(code="doctor", label="Doctor"))
            await self.session.flush()

        async def session_override():
            yield self.session

        async def identity_override():
            return {"uid": self.uid, "email": f"{self.uid}@example.invalid"}

        async def rate_limit_override():
            return None

        overrides = patch.dict(
            app.dependency_overrides,
            {
                get_session: session_override,
                get_verified_identity: identity_override,
                require_authenticated_rate_limit: rate_limit_override,
            },
        )
        overrides.start()
        self.addCleanup(overrides.stop)
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)

    async def test_professional_signup_persists_profile_without_professional_role(self):
        response = await self.client.post(
            ONBOARD,
            json={"display_name": "Synthetic pending applicant", "requested_role": "doctor"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["roles"], [])
        user = await self.session.scalar(select(User).where(User.firebase_uid == self.uid))
        self.assertIsNotNone(user)
        profile = await self.session.get(ProfessionalProfile, user.id)
        self.assertIsNotNone(profile)
        self.assertEqual(profile.role, "doctor")
        self.assertEqual(profile.verification_status, "not_submitted")
        assignments = await self.session.scalars(
            select(UserRole).where(UserRole.user_id == user.id)
        )
        self.assertEqual(assignments.all(), [])
