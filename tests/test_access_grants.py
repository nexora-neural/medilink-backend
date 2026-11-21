"""Regression coverage for ML-59 grant validation and lifecycle."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError

from app.modules.consent import service
from app.modules.consent.models import AccessGrant
from app.modules.consent.schemas import GrantInput, GrantRevokeInput


def grant_input(**overrides):
    return GrantInput.model_validate(
        {"doctor_reference": "opaque-example", "categories": ["reports"],
         "allow_consultation": False} | overrides
    )


class GrantValidationTests(TestCase):
    def test_duration_defaults_to_24_hours(self):
        self.assertEqual(grant_input().duration_hours, 24)

    def test_only_supported_integer_durations(self):
        for value in (0, 25, "24", 24.0, True):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                grant_input(duration_hours=value)

    def test_categories_must_be_unique_and_nonempty(self):
        for categories in ([], ["reports", "reports"], ["other"]):
            with self.subTest(categories=categories), self.assertRaises(ValidationError):
                grant_input(categories=categories)

    def test_blank_revocation_reason_is_rejected(self):
        with self.assertRaises(ValidationError):
            GrantRevokeInput(reason=" ")


class GrantServiceTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = AsyncMock()
        self.patient = SimpleNamespace(id=uuid4(), status="active")
        self.doctor = SimpleNamespace(id=uuid4(), status="active")
        self.now = datetime.now(UTC)

    def new_grant(self, *, expires_at):
        return AccessGrant(
            id=uuid4(), patient_user_id=self.patient.id, professional_user_id=self.doctor.id,
            organization_id=None, status="active", allow_consultation=True,
            created_at=self.now - timedelta(days=1), starts_at=self.now - timedelta(days=1),
            expires_at=expires_at,
        )

    async def test_create_uses_selected_duration_and_categories(self):
        async def insert(_db, grant, categories):
            grant.id = uuid4()
            grant.created_at = grant.starts_at
            self.assertEqual(categories, ["reports"])

        profile = SimpleNamespace(verification_status="approved")
        with (
            patch.object(service, "require_patient"),
            patch.object(service, "require_role"),
            patch.object(service, "find_active_doctor_by_reference",
                         return_value=(self.doctor, profile)),
            patch.object(service.repository, "insert_grant", side_effect=insert),
            patch.object(service, "append_event") as audit,
        ):
            detail = await service.create_grant(
                self.db, self.patient, grant_input(duration_hours=720), "test-request"
            )
        self.assertEqual(detail.grant.expires_at - detail.grant.starts_at, timedelta(days=30))
        self.assertEqual(detail.categories, ["reports"])
        self.assertEqual(audit.await_args.kwargs["target_id"], detail.grant.id)
        self.db.commit.assert_awaited_once()

    async def test_revocation_preserves_categories_and_keeps_reason_out_of_audit(self):
        grant = self.new_grant(expires_at=self.now + timedelta(hours=1))
        with (
            patch.object(service, "require_patient"),
            patch.object(service.repository, "lock_owned_grant", return_value=grant) as lock,
            patch.object(service.repository, "categories_by_grant",
                         return_value={grant.id: ["reports"]}),
            patch.object(service, "append_event") as audit,
        ):
            detail = await service.revoke_grant(
                self.db, self.patient, grant.id, "Patient requested revocation", "test-request"
            )
        lock.assert_awaited_once_with(self.db, self.patient.id, grant.id)
        self.assertEqual(detail.grant.status, "revoked")
        self.assertIsNotNone(detail.grant.revoked_at)
        self.assertEqual(detail.categories, ["reports"])
        self.assertEqual(grant.revocation_reason, "Patient requested revocation")
        self.assertNotIn("reason", audit.await_args.kwargs["metadata"])
        self.db.commit.assert_awaited_once()

    async def test_missing_or_expired_grant_does_not_commit(self):
        expired = self.new_grant(expires_at=self.now)
        for grant, expected_status in ((None, 404), (expired, 409)):
            with (
                self.subTest(status=expected_status),
                patch.object(service, "require_patient"),
                patch.object(service.repository, "lock_owned_grant", return_value=grant),
                patch.object(service, "append_event") as audit,
                self.assertRaises(HTTPException) as raised,
            ):
                await service.revoke_grant(
                    self.db, self.patient, uuid4(), "Revoking grant", "test-request"
                )
            self.assertEqual(raised.exception.status_code, expected_status)
            audit.assert_not_awaited()
        self.db.commit.assert_not_awaited()
