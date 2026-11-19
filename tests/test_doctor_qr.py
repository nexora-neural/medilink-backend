import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError

from app.modules.profiles.doctor_qr import service
from app.modules.profiles.doctor_qr.schemas import DoctorQrResolveRequest


class DoctorQrTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_returns_opaque_reference_as_qr_payload(self):
        db = object()
        actor = SimpleNamespace(id=uuid4())
        profile = SimpleNamespace(
            verification_status="approved",
            doctor_reference="TEST-opaque-reference",
        )

        with (
            patch.object(service, "require_role", new_callable=AsyncMock) as require_role,
            patch.object(service.repository, "get_doctor_profile", return_value=profile),
        ):
            result = await service.get_my_doctor_qr(db, actor)

        require_role.assert_awaited_once_with(db, actor, "doctor")
        self.assertEqual(
            result.model_dump(),
            {
                "doctor_reference": "TEST-opaque-reference",
                "qr_payload": "TEST-opaque-reference",
            },
        )

    async def test_resolve_denies_active_caller_without_patient_role(self):
        db = AsyncMock()
        db.scalar.return_value = None
        actor = SimpleNamespace(id=uuid4(), status="active")

        with patch.object(service.repository, "find_active_doctor_by_reference") as find:
            with self.assertRaises(HTTPException) as raised:
                await service.resolve_doctor_qr(db, actor, "TEST-reference")

        self.assertEqual(raised.exception.status_code, 403)
        db.scalar.assert_awaited_once()
        find.assert_not_awaited()

    async def test_resolve_returns_doctor_identity_to_patient(self):
        db = AsyncMock()
        db.scalar.return_value = 1
        actor = SimpleNamespace(id=uuid4(), status="active")
        doctor = SimpleNamespace(id=uuid4(), display_name="Test Doctor")
        profile = SimpleNamespace(verification_status="approved")
        clinic = SimpleNamespace(id=uuid4(), name="Test Clinic")

        with (
            patch.object(
                service.repository,
                "find_active_doctor_by_reference",
                return_value=(doctor, profile),
            ),
            patch.object(
                service.repository,
                "list_approved_doctor_clinics",
                return_value=[clinic],
            ),
        ):
            result = await service.resolve_doctor_qr(db, actor, "TEST-reference")

        self.assertEqual(
            result.model_dump(),
            {
                "user_id": doctor.id,
                "role": "doctor",
                "display_name": "Test Doctor",
                "verification_status": "approved",
                "clinics": [{"id": clinic.id, "name": "Test Clinic"}],
            },
        )

    async def test_unknown_reference_returns_404(self):
        db = AsyncMock()
        db.scalar.return_value = 1
        actor = SimpleNamespace(id=uuid4(), status="active")

        with patch.object(service.repository, "find_active_doctor_by_reference",
                          return_value=None):
            with self.assertRaises(HTTPException) as raised:
                await service.resolve_doctor_qr(db, actor, "unknown")

        self.assertEqual(raised.exception.status_code, 404)

    def test_reference_length_limits(self):
        DoctorQrResolveRequest(doctor_reference="x" * 256)

        for reference in ("", "x" * 257):
            with self.subTest(length=len(reference)), self.assertRaises(ValidationError):
                DoctorQrResolveRequest(doctor_reference=reference)