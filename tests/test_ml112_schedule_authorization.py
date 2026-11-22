"""ML-112 regression tests for medication schedule authorization."""

from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from fastapi import HTTPException

from app.modules.identity.authorization import require_role


class ScheduleAuthorizationTests(IsolatedAsyncioTestCase):
    async def test_active_patient_role_is_allowed(self):
        user = SimpleNamespace(id="patient-123", status="active")
        session = SimpleNamespace(scalar=AsyncMock(return_value="patient-role-id"))

        await require_role(session, user, "patient")

        session.scalar.assert_awaited_once()

    async def test_revoked_or_missing_patient_role_is_denied(self):
        user = SimpleNamespace(id="patient-123", status="active")
        session = SimpleNamespace(scalar=AsyncMock(return_value=None))

        with self.assertRaises(HTTPException) as context:
            await require_role(session, user, "patient")

        self.assertEqual(context.exception.status_code, 403)

    async def test_inactive_account_is_denied(self):
        user = SimpleNamespace(id="patient-123", status="pending")
        session = SimpleNamespace(scalar=AsyncMock())

        with self.assertRaises(HTTPException) as context:
            await require_role(session, user, "patient")

        self.assertEqual(context.exception.status_code, 403)
        session.scalar.assert_not_awaited()
