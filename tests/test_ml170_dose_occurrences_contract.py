"""ML-170 request/response contract checks without PostgreSQL."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app.database.session import get_session
from app.main import app
from app.modules.identity.auth import get_current_user
from app.modules.identity.request_limits import require_authenticated_rate_limit
from app.modules.medication import service
from app.modules.medication.schemas import DoseOccurrencesRead, DoseOccurrencesReadPage


class DoseOccurrenceContractTests(TestCase):
    def setUp(self):
        self.patient_id = uuid4()
        overrides = patch.dict(
            app.dependency_overrides,
            {
                get_current_user: lambda: SimpleNamespace(id=self.patient_id, status="active"),
                require_authenticated_rate_limit: lambda: None,
                get_session: lambda: None,
            },
        )
        overrides.start()
        self.addCleanup(overrides.stop)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.list_path = f"/api/v1/patients/{self.patient_id}/dose-occurrences"
        self.snooze_path = f"/api/v1/dose-occurrences/{uuid4()}/snooze"
        self.now = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)
        self.occurrence = DoseOccurrencesRead(
            id=uuid4(),
            schedule_id=uuid4(),
            scheduled_for=self.now,
            status="unreported",
            snoozed_until=None,
            created_at=self.now,
        )

    def test_routes_match_reserved_operation_ids_and_schemas(self):
        schema = app.openapi()
        self.assertEqual(
            schema["paths"]["/api/v1/patients/{patient_id}/dose-occurrences"]["get"][
                "operationId"
            ],
            "medilink_072",
        )
        self.assertEqual(
            schema["paths"]["/api/v1/dose-occurrences/{occurrence_id}/snooze"]["post"][
                "operationId"
            ],
            "medilink_073",
        )
        self.assertEqual(
            schema["paths"]["/api/v1/dose-occurrences/{occurrence_id}/snooze"]["post"][
                "requestBody"
            ]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/SnoozeInput",
        )
        self.assertEqual(
            schema["components"]["schemas"]["DoseOccurrencesReadPage"]["properties"]["items"][
                "items"
            ]["$ref"],
            "#/components/schemas/DoseOccurrencesRead",
        )
        self.assertNotIn(
            "snoozed_until", schema["components"]["schemas"]["DoseOccurrencesRead"]["required"]
        )
        snooze = schema["paths"]["/api/v1/dose-occurrences/{occurrence_id}/snooze"]["post"]
        self.assertEqual(
            set(snooze["responses"]),
            {"201", "400", "401", "403", "404", "409", "422", "429", "500", "503"},
        )
        self.assertNotIn("Idempotency-Key", {p["name"] for p in snooze.get("parameters", [])})

    def test_list_filters_and_pagination_are_passed_to_service(self):
        page = DoseOccurrencesReadPage(items=[self.occurrence], page=2, page_size=7, total=11)
        query = (
            "?from=2026-11-26T00:00:00Z&to=2026-11-28T00:00:00Z"
            "&status=unreported&page=2&page_size=7"
        )
        with patch.object(
            service, "list_occurrences", new_callable=AsyncMock, return_value=page
        ) as fn:
            response = self.client.get(self.list_path + query)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.json()["page"], 2)
        self.assertEqual(response.json()["page_size"], 7)
        self.assertEqual(response.json()["items"][0]["status"], "unreported")
        passed_query = fn.await_args.args[3]
        self.assertEqual(passed_query.from_.year, 2026)
        self.assertEqual(passed_query.to.day, 28)
        self.assertEqual(passed_query.status, "unreported")

    def test_list_rejects_invalid_ranges_status_paging_and_unknown_queries(self):
        with patch.object(service, "list_occurrences", new_callable=AsyncMock) as fn:
            for query in (
                "?from=2026-11-28T00:00:00Z&to=2026-11-27T00:00:00Z",
                "?from=2026-11-27T00:00:00&to=2026-11-28T00:00:00Z",
                "?status=unknown",
                "?page=0",
                "?page_size=101",
                "?unexpected=yes",
            ):
                with self.subTest(query=query):
                    self.assertEqual(self.client.get(self.list_path + query).status_code, 422)
            fn.assert_not_awaited()

    def test_snooze_requires_an_aware_timestamp_and_rejects_extra_fields(self):
        for payload in (
            {},
            {"snoozed_until": "2026-11-28T12:00:00"},
            {"snoozed_until": "2026-11-28T12:00:00Z", "status": "taken"},
        ):
            with self.subTest(payload=payload):
                self.assertEqual(
                    self.client.post(self.snooze_path, json=payload).status_code, 422
                )

    def test_snooze_returns_occurrence_projection_and_safe_service_errors(self):
        snoozed = self.occurrence.model_copy(
            update={"snoozed_until": datetime(2026, 11, 27, 12, 30, tzinfo=UTC)}
        )
        with patch.object(
            service, "snooze_occurrence", new_callable=AsyncMock, return_value=snoozed
        ):
            response = self.client.post(
                self.snooze_path,
                json={"snoozed_until": "2026-11-27T12:30:00Z"},
            )
        self.assertEqual(response.status_code, 201, response.text)
        scheduled_for = datetime.fromisoformat(
            response.json()["scheduled_for"].replace("Z", "+00:00")
        )
        self.assertEqual(scheduled_for, self.now)
        snoozed_until = datetime.fromisoformat(
            response.json()["snoozed_until"].replace("Z", "+00:00")
        )
        self.assertEqual(snoozed_until, datetime(2026, 11, 27, 12, 30, tzinfo=UTC))
        self.assertEqual(response.headers["Cache-Control"], "no-store")

        with patch.object(
            service,
            "snooze_occurrence",
            new_callable=AsyncMock,
            side_effect=service.MedicationScheduleError(409, "INVALID_STATE", "Not eligible."),
        ):
            denied = self.client.post(
                self.snooze_path,
                json={"snoozed_until": "2026-11-27T12:30:00Z"},
            )
        self.assertEqual(denied.status_code, 409)
        self.assertEqual(denied.json()["code"], "INVALID_STATE")
        self.assertNotIn("SQL", denied.text)

    def test_malformed_json_keeps_the_safe_error_shape(self):
        response = self.client.post(
            self.snooze_path,
            content="{broken",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "BAD_REQUEST")
        self.assertNotIn("broken", response.text)
