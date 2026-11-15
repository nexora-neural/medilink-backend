
"""ML-102 regression checks for revoked measurement roles."""

import re
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from app.modules.measurements.router import active_actor


class MeasurementRevokedRoleTests(IsolatedAsyncioTestCase):
    async def test_revoked_roles_are_excluded(self):
        actor = {
            "id": "test-user-id",
            "status": "active",
            "is_patient": False,
            "is_doctor": False,
            "is_verified_doctor": False,
        }

        request = SimpleNamespace(state=SimpleNamespace())

        with patch(
            "app.modules.measurements.router.fetch_one",
            new_callable=AsyncMock,
            return_value=actor,
        ) as mock_fetch:
            result = await active_actor(
                db=None,
                uid="test-firebase-uid",
                request=request,
            )

        self.assertEqual(result, actor)

        sql = mock_fetch.await_args.args[1]

        role_checks = {
            name: query
            for query, name in re.findall(
                r"EXISTS\s*\((.*?)\)\s+AS\s+"
                r"(is_patient|is_doctor|is_verified_doctor)",
                sql,
                flags=re.DOTALL,
            )
        }

        for role in ("is_patient", "is_doctor", "is_verified_doctor"):
            with self.subTest(role=role):
                self.assertIn(role, role_checks)
                self.assertRegex(
                    role_checks[role],
                    r"(?i)\bur\.revoked_at\s+IS\s+NULL\b",
                )
