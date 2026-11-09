from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock
from uuid import uuid4

from httpx import ASGITransport, AsyncClient

from app.database.session import get_session
from app.main import app
from app.modules.identity.auth import get_current_user
from app.modules.identity.request_limits import require_authenticated_rate_limit


class PatientProfileAuthorizationTests(IsolatedAsyncioTestCase):
    def setUp(self):
        app.dependency_overrides[require_authenticated_rate_limit] = lambda: None

    async def asyncTearDown(self):
        app.dependency_overrides.clear()

    async def test_patient_profile_rejects_non_patient(self):
        user = SimpleNamespace(id=uuid4(), status="active")
        session = SimpleNamespace(scalar=AsyncMock(return_value=None))
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_session] = lambda: session
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.patch(
                "/api/v1/me/patient-profile", json={"emergency_contact_name": "Test"}
            )
        self.assertEqual(response.status_code, 403)
        session.scalar.assert_awaited_once()

    async def test_patient_profile_rejects_pending_account(self):
        user = SimpleNamespace(id=uuid4(), status="pending")
        session = SimpleNamespace(scalar=AsyncMock())
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_session] = lambda: session
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.patch(
                "/api/v1/me/patient-profile", json={"emergency_contact_name": "Test"}
            )
        self.assertEqual(response.status_code, 403)
        session.scalar.assert_not_awaited()
