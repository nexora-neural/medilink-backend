"""Bodyless confirmation contract, authentication and quota checks without SQL."""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app.core.rate_limits import RateLimitExceeded, RateLimitUnavailable
from app.database.session import get_session
from app.main import app
from app.modules.identity.auth import get_current_user
from app.modules.identity.request_limits import require_authenticated_rate_limit
from app.modules.prescriptions import service


class ConfirmationContractTests(TestCase):
    def setUp(self):
        self.path = f"/api/v1/prescriptions/{uuid4()}/confirm"
        overrides = patch.dict(
            app.dependency_overrides,
            {
                get_current_user: lambda: SimpleNamespace(id=uuid4(), status="active"),
                require_authenticated_rate_limit: lambda: None,
                get_session: lambda: None,
            },
        )
        overrides.start()
        self.addCleanup(overrides.stop)
        self.client = TestClient(app, raise_server_exceptions=False)

    def test_body_and_forged_privileges_are_rejected_without_calling_service(self):
        with patch.object(service, "confirm_patient_draft", new_callable=AsyncMock) as confirm:
            for body in [
                b"{}",
                b"null",
                b"{",
                b" ",
                b'{"source":"doctor"}',
                b'{"review_status":"approved","issued_at":"FORGED"}',
            ]:
                with self.subTest(body=body):
                    response = self.client.post(self.path, content=body)
                    self.assertEqual(response.status_code, 400, response.text)
                    self.assertEqual(response.json()["code"], "INVALID_REQUEST")
                    self.assertNotIn("FORGED", response.text)
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                    self.assertTrue(response.json()["request_id"])
            confirm.assert_not_awaited()

    def test_uuid_and_query_validation_are_safe(self):
        with patch.object(service, "confirm_patient_draft", new_callable=AsyncMock) as confirm:
            for path in [
                self.path + "?status=active",
                self.path + "?items=FORGED",
                "/api/v1/prescriptions/invalid/confirm",
            ]:
                response = self.client.post(path)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["code"], "VALIDATION_ERROR")
                self.assertNotIn("FORGED", response.text)
            confirm.assert_not_awaited()

    def test_missing_identity_is_rejected_without_database(self):
        with patch.dict(app.dependency_overrides, clear=True):
            app.dependency_overrides[get_session] = lambda: None
            response = self.client.post(self.path)
        self.assertEqual(response.status_code, 401, response.text)

    def test_shared_quota_and_unavailable_store_fail_closed(self):
        for exception, code in [(RateLimitExceeded(17), 429), (RateLimitUnavailable(), 503)]:

            async def blocked(exc=exception):
                raise exc

            with patch.dict(app.dependency_overrides, {require_authenticated_rate_limit: blocked}):
                response = self.client.post(self.path)
            self.assertEqual(response.status_code, code, response.text)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            if code == 429:
                self.assertEqual(response.headers["Retry-After"], "17")

    def test_openapi_matches_bodyless_patient_confirmation_contract(self):
        spec = self.client.get("/openapi.json").json()
        op = spec["paths"]["/api/v1/prescriptions/{prescription_id}/confirm"]["post"]
        self.assertEqual(op["operationId"], "medilink_048")
        self.assertNotIn("requestBody", op)
        self.assertTrue(op["security"])
        self.assertIn("200", op["responses"])
        for code in ["400", "401", "403", "404", "409", "422", "429", "500", "503"]:
            self.assertIn(code, op["responses"])
