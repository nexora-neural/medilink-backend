"""Unit tests for validated append-only audit event handling."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException, Request, Response
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.database.session import get_session
from app.main import app
from app.modules.audit import service
from app.modules.audit.models import AuditEvent
from app.modules.audit.schemas import AuditEventInput
from app.modules.identity.auth import get_current_user


class FakeSession:
    def __init__(self):
        self.events = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def add(self, value):
        self.events.append(value)

    async def flush(self):
        return None

    async def commit(self):
        self.committed = True


class AuditEventSchemaTests(unittest.TestCase):
    def test_accepts_safe_event_fields(self):
        event = AuditEventInput(
            action="api.request",
            target_type="api_route",
            outcome="denied",
            request_id="req-123",
            metadata={"method": "GET", "route": "/api/v1/me", "status_code": 403},
        )

        self.assertEqual(event.metadata["status_code"], 403)

    def test_rejects_sensitive_or_unapproved_metadata_fields(self):
        for key, value in (("authorization", "Bearer secret"), ("reason", "patient data")):
            with self.subTest(key=key), self.assertRaises(ValidationError):
                AuditEventInput(
                    action="api.request",
                    target_type="api_route",
                    outcome="success",
                    request_id="req-123",
                    metadata={key: value},
                )

    def test_rejects_multiple_actor_identities(self):
        with self.assertRaises(ValidationError):
            AuditEventInput(
                actor_user_id=uuid4(),
                actor_client_id=uuid4(),
                action="api.request",
                target_type="api_route",
                outcome="success",
                request_id="req-123",
            )

    def test_clinical_history_routes_do_not_expose_update_or_delete_methods(self):
        paths = app.openapi()["paths"]
        protected_paths = {
            path: operations
            for path, operations in paths.items()
            if "/measurements" in path
        }

        self.assertTrue(protected_paths)
        for path, operations in protected_paths.items():
            with self.subTest(path=path):
                self.assertTrue({"put", "patch", "delete"}.isdisjoint(operations))


class ApiRequestAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_audit_records_route_actor_and_outcome_without_secrets(self):
        actor_id = uuid4()
        session = FakeSession()
        app.state.audit_session_factory = lambda: session
        scope = {
            "app": app,
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/api/v1/patients/real-id/records",
            "raw_path": b"/api/v1/patients/real-id/records",
            "query_string": b"token=private-value",
            "headers": [(b"authorization", b"Bearer private-value")],
            "server": ("test.local", 443),
            "client": ("127.0.0.1", 10000),
            "root_path": "",
            "state": {"request_id": "req-123", "audit_actor_user_id": actor_id},
            "route": SimpleNamespace(path="/api/v1/patients/{patient_id}/records"),
        }

        try:
            await service.record_api_request(Request(scope), Response(status_code=403))
        finally:
            del app.state.audit_session_factory

        self.assertTrue(session.committed)
        self.assertEqual(len(session.events), 1)
        event = session.events[0]
        self.assertIsInstance(event, AuditEvent)
        self.assertEqual(event.actor_user_id, actor_id)
        self.assertEqual(event.action, "api.request")
        self.assertEqual(event.outcome, "denied")
        self.assertEqual(event.request_id, "req-123")
        self.assertEqual(
            event.metadata_,
            {
                "method": "GET",
                "route": "/api/v1/patients/{patient_id}/records",
                "status_code": 403,
            },
        )
        self.assertNotIn("private-value", str(event.metadata_))

    async def asyncSetUp(self):
        async def session_override():
            yield object()

        async def actor_override(request: Request):
            actor_id = uuid4()
            request.state.audit_actor_user_id = actor_id
            return SimpleNamespace(id=actor_id, status="active")

        app.dependency_overrides[get_session] = session_override
        app.dependency_overrides[get_current_user] = actor_override
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        app.dependency_overrides.clear()
        if hasattr(app.state, "audit_session_factory"):
            del app.state.audit_session_factory

    async def test_api_middleware_persists_successful_request(self):
        session = FakeSession()
        app.state.audit_session_factory = lambda: session
        with (
            patch(
                "app.modules.organizations.service.repository.list_organizations",
                new=AsyncMock(return_value=([], 0)),
            ),
        ):
            response = await self.client.get("/api/v1/organizations?q=private-value")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(session.events), 1)
        event = session.events[0]
        self.assertEqual(event.action, "api.request")
        self.assertEqual(event.outcome, "success")
        self.assertEqual(event.metadata_["route"], "/organizations")
        self.assertNotIn("private-value", str(event.metadata_))

    async def test_api_middleware_persists_denied_request(self):
        session = FakeSession()

        async def denied_actor(request: Request):
            request.state.audit_actor_user_id = uuid4()
            raise HTTPException(status_code=403, detail="Denied")

        app.dependency_overrides[get_current_user] = denied_actor
        app.state.audit_session_factory = lambda: session
        with patch("app.modules.audit.service.async_session", return_value=session):
            response = await self.client.get("/api/v1/organizations")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(len(session.events), 1)
        self.assertEqual(session.events[0].outcome, "denied")
        self.assertIsNotNone(session.events[0].actor_user_id)

    async def test_job_attempt_is_recorded_without_error_details(self):
        session = FakeSession()
        run = SimpleNamespace(id=uuid4(), job_type="report_import", attempt_count=2)

        event = await service.record_job_attempt(
            session,
            run,
            request_id="job-run-123",
            outcome="failed",
        )

        self.assertEqual(event.action, "job.attempt")
        self.assertEqual(event.target_id, run.id)
        self.assertEqual(event.outcome, "failed")
        self.assertEqual(event.metadata_, {"job_type": "report_import", "attempt_count": 2})

    async def test_backup_attempt_is_recorded_without_backup_contents(self):
        session = FakeSession()
        run = SimpleNamespace(
            id=uuid4(),
            status="succeeded",
            database_included=True,
            files_included=False,
            restore_check_status="passed",
        )

        event = await service.record_backup_run(
            session,
            run,
            request_id="backup-run-123",
            outcome="success",
        )

        self.assertEqual(event.action, "backup.run")
        self.assertEqual(event.target_id, run.id)
        self.assertEqual(
            event.metadata_,
            {
                "status": "succeeded",
                "database_included": True,
                "files_included": False,
                "restore_check_status": "passed",
            },
        )
