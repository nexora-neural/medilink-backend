"""HTTP route coverage for ML-64 consultations."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from app.database.session import get_session
from app.main import app
from app.modules.consultations import service
from app.modules.consultations.schemas import (
    ConsultationsRead,
    ConsultationsReadPage,
    QueueEntryPage,
)
from app.modules.identity.auth import get_current_user
from app.modules.identity.request_limits import require_authenticated_rate_limit


class ConsultationRouteTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.doctor = SimpleNamespace(id=uuid4(), status="active")
        self.db = AsyncMock()
        self.patient_id = uuid4()
        self.grant_id = uuid4()
        self.consultation_id = uuid4()

        self.previous_overrides = app.dependency_overrides.copy()
        app.dependency_overrides[require_authenticated_rate_limit] = lambda: None
        app.dependency_overrides[get_current_user] = lambda: self.doctor
        app.dependency_overrides[get_session] = lambda: self.db

        self.client = AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        app.dependency_overrides.clear()
        app.dependency_overrides.update(self.previous_overrides)

    def consultation(self, *, ended=False):
        now = datetime.now(UTC)
        return ConsultationsRead(
            id=self.consultation_id,
            patient_user_id=self.patient_id,
            doctor_user_id=self.doctor.id,
            organization_id=None,
            grant_id=self.grant_id,
            started_at=now,
            ended_at=now if ended else None,
            summary="Synthetic summary." if ended else None,
        )

    async def test_queue_passes_pagination_and_disables_caching(self):
        page = QueueEntryPage(items=[], page=2, page_size=10, total=0)

        with patch.object(service, "list_patient_queue", return_value=page) as load:
            response = await self.client.get("/api/v1/me/patient-queue?page=2&page_size=10")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.json()["items"], [])
        query = load.await_args.args[2]
        self.assertEqual(query.page, 2)
        self.assertEqual(query.page_size, 10)

    async def test_start_returns_created_consultation(self):
        with patch.object(
            service,
            "start_consultation",
            return_value=self.consultation(),
        ) as start:
            response = await self.client.post(
                "/api/v1/consultations",
                json={"grant_id": str(self.grant_id)},
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["grant_id"], str(self.grant_id))
        self.assertEqual(start.await_args.args[2].grant_id, self.grant_id)
        self.assertTrue(start.await_args.args[3])
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_list_passes_patient_id_and_pagination(self):
        page = ConsultationsReadPage(
            items=[self.consultation()],
            page=1,
            page_size=20,
            total=1,
        )

        with patch.object(service, "list_consultations", return_value=page) as load:
            response = await self.client.get(f"/api/v1/patients/{self.patient_id}/consultations")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(load.await_args.args[2], self.patient_id)
        self.assertEqual(response.json()["total"], 1)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_end_passes_summary_and_returns_ended_context(self):
        with patch.object(
            service,
            "end_consultation",
            return_value=self.consultation(ended=True),
        ) as end:
            response = await self.client.post(
                f"/api/v1/consultations/{self.consultation_id}/end",
                json={"summary": "Synthetic summary."},
            )

        self.assertEqual(response.status_code, 201)
        self.assertIsNotNone(response.json()["ended_at"])
        self.assertEqual(end.await_args.args[2], self.consultation_id)
        self.assertEqual(end.await_args.args[3].summary, "Synthetic summary.")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_start_rejects_extra_identity_fields(self):
        with patch.object(service, "start_consultation") as start:
            response = await self.client.post(
                "/api/v1/consultations",
                json={
                    "grant_id": str(self.grant_id),
                    "doctor_user_id": str(uuid4()),
                },
            )

        self.assertEqual(response.status_code, 422)
        start.assert_not_awaited()

    async def test_invalid_pagination_and_unknown_query_are_rejected(self):
        for query in (
            "page=0",
            "page_size=101",
            "patient_id=unapproved-filter",
        ):
            with self.subTest(query=query):
                with patch.object(service, "list_patient_queue") as load:
                    response = await self.client.get(f"/api/v1/me/patient-queue?{query}")

                self.assertEqual(response.status_code, 422)
                load.assert_not_awaited()

    async def test_malformed_json_returns_safe_error(self):
        with patch.object(service, "start_consultation") as start:
            response = await self.client.post(
                "/api/v1/consultations",
                content="{",
                headers={"Content-Type": "application/json"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["message"], "Malformed JSON.")
        self.assertTrue(response.json()["request_id"])
        start.assert_not_awaited()

    async def test_permission_and_not_found_errors_follow_contract(self):
        for status, code in (
            (403, "ACCESS_DENIED"),
            (404, "RESOURCE_NOT_FOUND"),
        ):
            with self.subTest(status=status):
                with patch.object(
                    service,
                    "start_consultation",
                    side_effect=HTTPException(status, "Request is unavailable."),
                ):
                    response = await self.client.post(
                        "/api/v1/consultations",
                        json={"grant_id": str(self.grant_id)},
                    )

                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json()["code"], code)
                self.assertEqual(response.json()["field_errors"], {})
                self.assertTrue(response.json()["request_id"])
                self.assertEqual(response.headers["Cache-Control"], "no-store")
