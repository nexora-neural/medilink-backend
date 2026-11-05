from datetime import date, timedelta
from unittest import IsolatedAsyncioTestCase, TestCase

from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.main import app
from app.modules.profiles.schemas import (
    AccountUpdateRequest,
    OnboardRequest,
    PatientProfileUpdateRequest,
)


class ProfileValidationTests(TestCase):
    def test_patient_requires_birth_date_and_sex(self):
        with self.assertRaises(ValidationError):
            OnboardRequest(display_name="Test", requested_role="patient")
        value = OnboardRequest(
            display_name="Test", requested_role="patient", date_of_birth="2000-01-01", sex="female"
        )
        self.assertEqual(value.sex, "female")

    def test_rejects_future_birth_date_and_unsupported_sex(self):
        with self.assertRaises(ValidationError):
            PatientProfileUpdateRequest(date_of_birth=date.today() + timedelta(days=1))
        with self.assertRaises(ValidationError):
            PatientProfileUpdateRequest(sex="another")

    def test_empty_patches_rejected_and_nullable_phone_supported(self):
        with self.assertRaises(ValidationError):
            AccountUpdateRequest()
        with self.assertRaises(ValidationError):
            PatientProfileUpdateRequest()
        self.assertEqual(
            AccountUpdateRequest(phone=None).model_dump(exclude_unset=True), {"phone": None}
        )


class ProfileRouteTests(IsolatedAsyncioTestCase):
    async def test_missing_auth_is_safe_and_not_cached(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/v1/me")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "AUTHENTICATION_REQUIRED")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertTrue(response.json()["request_id"])
