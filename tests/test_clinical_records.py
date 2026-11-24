"""ML-68 route contract tests that do not require PostgreSQL or Firebase."""

from unittest import TestCase
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app.core.rate_limits import RateLimitExceeded, RateLimitUnavailable
from app.database.session import get_session
from app.main import app
from app.modules.clinical_records import service
from app.modules.identity.auth import get_current_user
from app.modules.identity.models import User
from app.modules.identity.request_limits import require_authenticated_rate_limit


class ClinicalContractTests(TestCase):
    def setUp(self):
        self.patient = uuid4()
        self.actor = User(id=self.patient, firebase_uid="synthetic", status="active")
        override = patch.dict(
            app.dependency_overrides,
            {
                get_current_user: lambda: self.actor,
                require_authenticated_rate_limit: lambda: None,
            },
        )
        override.start()
        self.addCleanup(override.stop)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.path = f"/api/v1/patients/{self.patient}/clinical-records"
        self.body = {
            "type": "condition",
            "title": "Synthetic condition",
            "recorded_at": "2026-11-23T10:00:00Z",
        }

    def test_validation_never_calls_write_or_echoes_submitted_values(self):
        bad = [
            {"recorded_at": "2026-11-23"},
            {"recorded_at": "2026-11-23T10:00:00"},
            {"recorded_at": 12345},
            {"title": " "},
            {"title": "x" * 201},
            {"description": "x" * 8001},
            {"clinical_code": "x" * 101},
            {"source": "doctor"},
            {"recorded_by": str(uuid4())},
            {"organization_id": str(uuid4())},
            {"status": "resolved"},
            {"supersedes_id": str(uuid4())},
            {"id": str(uuid4())},
            {"type": "vaccination"},
        ]
        with patch.object(service, "create_record", new_callable=AsyncMock) as create:
            for changes in bad:
                with self.subTest(changes=list(changes)):
                    response = self.client.post(self.path, json=self.body | changes)
                    self.assertEqual(response.status_code, 422, response.text)
                    data = response.json()
                    self.assertEqual(data["code"], "VALIDATION_ERROR")
                    self.assertEqual(set(data), {"code", "message", "field_errors", "request_id"})
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                    self.assertEqual(data["request_id"], response.headers["X-Request-ID"])
            create.assert_not_awaited()

    def test_list_filters_are_bounded_and_unknown_fields_rejected(self):
        bad = [
            "?page=0",
            "?page_size=101",
            "?type=invalid",
            "?unknown=secret",
            "?from=2026-11-23T00:00:00Z",
            "?from=2026-11-23&to=2026-11-24",
            "?from=2026-11-24T00:00:00Z&to=2026-11-23T00:00:00Z",
        ]
        with patch.object(service, "list_patient_records", new_callable=AsyncMock) as listing:
            for query in bad:
                with self.subTest(query=query):
                    self.assertEqual(self.client.get(self.path + query).status_code, 422)
            listing.assert_not_awaited()

    def test_malformed_json_is_safe_400(self):
        response = self.client.post(
            self.path, content="{bad", headers={"Content-Type": "application/json"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "BAD_REQUEST")

    def test_authentication_is_required(self):
        with patch.dict(app.dependency_overrides, {get_session: lambda: None}, clear=True):
            response = self.client.get(self.path)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "AUTHENTICATION_REQUIRED")

    def test_no_overwrite_routes_exist(self):
        for method in (self.client.patch, self.client.put, self.client.delete):
            self.assertEqual(method(f"/api/v1/clinical-records/{uuid4()}").status_code, 405)

    def test_contract_operations_and_provenance_are_in_openapi(self):
        schema = app.openapi()
        for path, method, operation in [
            ("/api/v1/patients/{patient_id}/clinical-records", "get", "medilink_027"),
            ("/api/v1/patients/{patient_id}/clinical-records", "post", "medilink_028"),
            ("/api/v1/clinical-records/{record_id}", "get", "medilink_037"),
        ]:
            self.assertEqual(schema["paths"][path][method]["operationId"], operation)
        properties = schema["components"]["schemas"]["ClinicalRecordRead"]["properties"]
        for field in ("source", "recorded_by", "organization_id", "recorded_at", "status"):
            self.assertIn(field, properties)

    def test_shared_rate_limit_failures_keep_safe_contract(self):
        for error, status, code in [
            (RateLimitExceeded(12), 429, "RATE_LIMITED"),
            (RateLimitUnavailable(), 503, "SERVICE_UNAVAILABLE"),
        ]:

            async def fail(error=error):
                raise error

            with patch.dict(app.dependency_overrides, {require_authenticated_rate_limit: fail}):
                response = self.client.get(self.path)
            self.assertEqual(response.status_code, status)
            self.assertEqual(response.json()["code"], code)
            if status == 429:
                self.assertEqual(response.headers["Retry-After"], "12")
