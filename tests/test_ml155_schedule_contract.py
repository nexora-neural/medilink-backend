"""ML-155 contract, confirmation and private-error tests without a database."""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app.core.rate_limits import RateLimitExceeded
from app.database.session import get_session
from app.main import app
from app.modules.identity.auth import get_current_user
from app.modules.identity.request_limits import require_authenticated_rate_limit
from app.modules.medication import service


class ScheduleContractTests(TestCase):
    def setUp(self):
        self.patient = uuid4()
        overrides = patch.dict(
            app.dependency_overrides,
            {
                get_current_user: lambda: SimpleNamespace(id=self.patient, status="active"),
                require_authenticated_rate_limit: lambda: None,
                get_session: lambda: None,
            },
        )
        overrides.start()
        self.addCleanup(overrides.stop)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.path = f"/api/v1/patients/{self.patient}/medication-schedules"
        self.payload = {
            "prescription_item_id": str(uuid4()),
            "timezone": "Asia/Colombo",
            "starts_at": "2026-11-25T08:00:00+05:30",
            "ends_at": None,
            "times": ["08:00:00"],
            "confirmed": True,
        }

    def test_input_requires_explicit_confirmation_and_bounded_fixed_times(self):
        cases = [
            {"confirmed": False},
            {"confirmed": 1},
            {"confirmed": "true"},
            {"times": []},
            {"times": ["08:00:00", "08:00:00"]},
            {"times": ["25:00:00"]},
            {"times": ["08:00"]},
            {"timezone": "not/a/timezone"},
            {"starts_at": "2026-11-25"},
            {"starts_at": "2026-11-25T08:00:00"},
            {"starts_at": 1732500000},
            {"ends_at": "2026-11-24T00:00:00Z"},
            {"status": "active"},
            {"dose_text": "FORGED"},
            {"patient_user_id": str(uuid4())},
            {"confirmed_at": "2026-11-25T08:00:00Z"},
            {"times": [f"{i:02d}:00:00" for i in range(24)] + ["00:30:00"]},
        ]
        with patch.object(service, "create_schedule", new_callable=AsyncMock) as create:
            for changes in cases:
                with self.subTest(fields=list(changes)):
                    response = self.client.post(self.path, json=self.payload | changes)
                    self.assertEqual(response.status_code, 422, response.text)
                    self.assertEqual(response.json()["code"], "VALIDATION_ERROR")
                    self.assertNotIn("FORGED", response.text)
            create.assert_not_awaited()

    def test_list_rejects_unknown_queries_status_and_unbounded_paging(self):
        with patch.object(service, "list_schedules", new_callable=AsyncMock) as listing:
            for query in ("?page=0", "?page_size=101", "?status=issued", "?unexpected=value"):
                self.assertEqual(self.client.get(self.path + query).status_code, 422)
            listing.assert_not_awaited()

    def test_status_requires_reason_and_current_version(self):
        path = f"/api/v1/medication-schedules/{uuid4()}/status"
        for body in ({"status": "paused", "reason": " "}, {"status": "draft", "reason": "test"}):
            self.assertEqual(
                self.client.post(path, json=body, headers={"If-Match": '"1"'}).status_code, 422
            )
        for value in ("0", "-1", "1.0", "*", 'W/"1"', '"1', '1"', "9999999999999"):
            response = self.client.post(
                path, json={"status": "paused", "reason": "test"}, headers={"If-Match": value}
            )
            self.assertEqual(response.status_code, 422)
        self.assertEqual(
            self.client.post(path, json={"status": "paused", "reason": "test"}).status_code, 422
        )

    def test_replacement_keeps_if_match_guard(self):
        path = f"/api/v1/medication-schedules/{uuid4()}/replacement"
        self.assertEqual(self.client.post(path, json=self.payload).status_code, 422)
        with patch.object(
            service,
            "replace_schedule",
            side_effect=service.MedicationScheduleError(
                409,
                "STALE_VERSION",
                "Refresh the schedule.",
            ),
        ):
            response = self.client.post(path, json=self.payload, headers={"If-Match": '"1"'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "STALE_VERSION")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_malformed_json_auth_and_shared_quota(self):
        response = self.client.post(
            self.path, content="{broken", headers={"Content-Type": "application/json"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "BAD_REQUEST")
        with patch.dict(app.dependency_overrides, {get_session: lambda: None}, clear=True):
            self.assertEqual(self.client.get(self.path).status_code, 401)

        async def limit():
            raise RateLimitExceeded(9)

        with patch.dict(app.dependency_overrides, {require_authenticated_rate_limit: limit}):
            response = self.client.get(self.path)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["Retry-After"], "9")

    def test_all_contract_operations_are_registered(self):
        schema = app.openapi()
        for path, method, operation in [
            ("/api/v1/patients/{patient_id}/medication-schedules", "post", "medilink_068"),
            ("/api/v1/patients/{patient_id}/medication-schedules", "get", "medilink_069"),
            ("/api/v1/medication-schedules/{schedule_id}/status", "post", "medilink_070"),
            ("/api/v1/medication-schedules/{schedule_id}/replacement", "post", "medilink_071"),
        ]:
            self.assertEqual(schema["paths"][path][method]["operationId"], operation)
