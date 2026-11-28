"""Contract validation, identity and request quota regression checks."""

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
from app.modules.prescriptions import lifecycle


class LifecycleContractTests(TestCase):
    def setUp(self):
        override = patch.dict(
            app.dependency_overrides,
            {
                get_current_user: lambda: SimpleNamespace(id=uuid4()),
                get_session: lambda: None,
                require_authenticated_rate_limit: lambda: None,
            },
        )
        override.start()
        self.addCleanup(override.stop)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.root = f"/api/v1/prescriptions/{uuid4()}"
        self.item = dict(
            medicine_name_snapshot="Synthetic",
            dose_text="Synthetic",
            frequency="daily",
            is_prn=False,
        )

    def test_version_input_requires_bounded_items_reason_and_no_privilege_fields(self):
        with patch.object(lifecycle, "create_version", new_callable=AsyncMock) as write:
            for action in ["versions", "renewals", "reissues"]:
                for change in [
                    {"reason": ""},
                    {"reason": " "},
                    {"reason": "x" * 1001},
                    {"items": []},
                    {"items": [self.item] * 101},
                    {"source": "doctor"},
                    {"review_status": "approved"},
                    {"status": "active"},
                    {"previous_prescription_id": str(uuid4())},
                    {"items": [self.item | {"dose_text": " "}]},
                    {"starts_on": "2026-11-30", "ends_on": "2026-11-29"},
                ]:
                    with self.subTest(action=action, fields=list(change)):
                        response = self.client.post(
                            self.root + "/" + action,
                            json={"items": [self.item], "reason": "Reviewed correction"} | change,
                        )
                        self.assertEqual(response.status_code, 422, response.text)
                        self.assertEqual(response.json()["code"], "VALIDATION_ERROR")
            write.assert_not_awaited()

    def test_terminal_actions_require_only_reason(self):
        for action in ["cancel", "discontinue", "complete"]:
            for body in [
                {},
                {"reason": None},
                {"reason": " "},
                {"reason": 1},
                {"reason": "x" * 1001},
                {"reason": "Synthetic", "status": "active"},
            ]:
                with self.subTest(action=action, body_fields=list(body)):
                    response = self.client.post(self.root + "/" + action, json=body)
                    self.assertEqual(response.status_code, 422)
                    self.assertTrue(response.json()["request_id"])

    def test_unknown_queries_malformed_body_and_optional_if_match_validation(self):
        with patch.object(lifecycle, "change_status", new_callable=AsyncMock) as write:
            response = self.client.post(
                self.root + "/cancel?source=doctor", json={"reason": "Test"}
            )
            self.assertEqual(response.status_code, 422)
            for value in ["0", "-1", "*", 'W/"1"', "1.0", '"1', '1"']:
                self.assertEqual(
                    self.client.post(
                        self.root + "/cancel", json={"reason": "Test"}, headers={"If-Match": value}
                    ).status_code,
                    422,
                )
            response = self.client.post(
                self.root + "/cancel", content="{", headers={"Content-Type": "application/json"}
            )
            self.assertEqual(response.status_code, 400)
            write.assert_not_awaited()

    def test_history_paging_and_unknown_queries_are_bounded(self):
        for query in ["page=0", "page_size=101", "source=doctor"]:
            self.assertEqual(self.client.get(self.root + "/history?" + query).status_code, 422)

    def test_missing_identity_and_shared_quota_fail_closed(self):
        with patch.dict(app.dependency_overrides, {get_session: lambda: None}, clear=True):
            self.assertEqual(self.client.get(self.root + "/history").status_code, 401)
            self.assertEqual(
                self.client.post(self.root + "/cancel", json={"reason": "Synthetic"}).status_code,
                401,
            )

        async def limited():
            raise RateLimitExceeded(9)

        with patch.dict(app.dependency_overrides, {require_authenticated_rate_limit: limited}):
            response = self.client.post(self.root + "/cancel", json={"reason": "Synthetic"})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["Retry-After"], "9")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_runtime_has_all_seven_contract_operations_and_private_event_schema(self):
        spec = self.client.get("/openapi.json").json()
        for index, action in enumerate(
            ["versions", "renewals", "reissues", "cancel", "discontinue", "complete", "history"], 51
        ):
            operation = spec["paths"]["/api/v1/prescriptions/{prescription_id}/" + action]
            operation = operation["get" if action == "history" else "post"]
            self.assertEqual(operation["operationId"], f"medilink_{index:03}")
            self.assertTrue(operation["security"])
            self.assertIn("200" if action == "history" else "201", operation["responses"])
        self.assertIn(
            "lifecycle_events", spec["components"]["schemas"]["PrescriptionDetail"]["properties"]
        )
