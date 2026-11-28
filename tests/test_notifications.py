from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app.database.session import get_session
from app.main import app
from app.modules.identity.auth import get_current_user
from app.modules.identity.request_limits import require_authenticated_rate_limit
from app.modules.notifications import service
from app.modules.notifications.schemas import NotificationsRead, NotificationsReadPage


class NotificationsContractTests(TestCase):
    def setUp(self):
        self.actor = SimpleNamespace(id=uuid4(), status="active")
        overrides = patch.dict(
            app.dependency_overrides,
            {
                get_current_user: lambda: self.actor,
                require_authenticated_rate_limit: lambda: None,
                get_session: lambda: None,
            },
        )
        overrides.start()
        self.addCleanup(overrides.stop)
        self.client = TestClient(app, raise_server_exceptions=False)

    def test_paths_operation_ids_and_response_schemas_match_contract(self):
        schema = app.openapi()
        listing = schema["paths"]["/api/v1/me/notifications"]["get"]
        reading = schema["paths"]["/api/v1/notifications/{notification_id}/read"]["post"]

        self.assertEqual(listing["operationId"], "medilink_104")
        self.assertEqual(reading["operationId"], "medilink_105")
        self.assertEqual(
            listing["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/NotificationsReadPage",
        )
        self.assertEqual(
            reading["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/NotificationsRead",
        )
        self.assertEqual(
            {parameter["name"] for parameter in listing["parameters"]},
            {"page", "page_size", "unread"},
        )
        page = next(parameter for parameter in listing["parameters"] if parameter["name"] == "page")
        self.assertEqual(page["schema"]["minimum"], 1)
        self.assertNotIn("maximum", page["schema"])
        self.assertNotIn("requestBody", reading)

    def test_list_passes_filters_and_pagination_and_disables_caching(self):
        item = NotificationsRead(
            id=uuid4(),
            user_id=self.actor.id,
            notification_type="dose_reminder",
            title="Medication reminder",
            body="A scheduled medicine dose is due.",
            created_at="2026-11-28T10:00:00Z",
        )
        page = NotificationsReadPage(items=[item], page=2, page_size=7, total=11)
        with patch.object(
            service,
            "list_own_notifications",
            new_callable=AsyncMock,
            return_value=page,
        ) as list_notifications:
            response = self.client.get("/api/v1/me/notifications?unread=true&page=2&page_size=7")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.json()["items"][0]["id"], str(item.id))
        self.assertEqual(response.json()["total"], 11)
        self.assertEqual(list_notifications.await_args.kwargs["unread"], True)
        self.assertEqual(list_notifications.await_args.kwargs["page"], 2)
        self.assertEqual(list_notifications.await_args.kwargs["page_size"], 7)

    def test_invalid_and_unknown_list_inputs_return_safe_validation_errors(self):
        with patch.object(service, "list_own_notifications", new_callable=AsyncMock) as listing:
            for query in ("?page=0", "?page_size=101", "?unread=maybe", "?other=true"):
                with self.subTest(query=query):
                    response = self.client.get("/api/v1/me/notifications" + query)
                    self.assertEqual(response.status_code, 422, response.text)
                    self.assertEqual(response.json()["code"], "VALIDATION_ERROR")
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
            listing.assert_not_awaited()

    def test_read_route_returns_contract_projection(self):
        item = NotificationsRead(
            id=uuid4(),
            user_id=self.actor.id,
            notification_type="report_accepted",
            title="Report available",
            body="A report is available.",
            created_at="2026-11-28T10:00:00Z",
            read_at="2026-11-28T10:01:00Z",
        )
        with patch.object(
            service,
            "mark_notification_read",
            new_callable=AsyncMock,
            return_value=item,
        ) as mark_read:
            response = self.client.post(f"/api/v1/notifications/{item.id}/read")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.json()["read_at"], "2026-11-28T10:01:00Z")
        self.assertEqual(mark_read.await_args.args[1].id, self.actor.id)
        self.assertEqual(mark_read.await_args.args[2], item.id)

    def test_malformed_notification_identifier_uses_safe_validation_shape(self):
        response = self.client.post("/api/v1/notifications/not-a-uuid/read")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "VALIDATION_ERROR")
        self.assertNotIn("not-a-uuid", response.text)
