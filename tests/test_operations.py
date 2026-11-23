"""ML-67 request validation, projections and transactional retry rules."""

import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from app.database.session import get_session
from app.modules.identity.auth import get_current_user
from app.modules.identity.request_limits import require_authenticated_rate_limit
from app.modules.operations import service
from app.modules.operations.policy import patient_predicate, safe_metadata
from app.modules.operations.router import router
from app.modules.operations.schemas import PatientActivity, ReasonInput


class ProjectionTests(unittest.TestCase):
    def test_reason_cannot_be_blank_or_accept_extra_fields(self):
        for value in (
            {"reason": "  "},
            {"reason": "retry", "status": "succeeded"},
        ):
            with self.assertRaises(ValidationError):
                ReasonInput(**value)

    def test_safe_projection_removes_payloads_and_errors(self):
        result = safe_metadata(
            {
                "status": "failed",
                "token": "secret",
                "reason": "private",
                "payload": {"value": 1},
            }
        )

        self.assertEqual(result, {"status": "failed"})
        self.assertEqual(
            service.safe_error("password=secret"),
            "JOB_FAILED",
        )
        self.assertEqual(
            safe_metadata({"status": "secret", "job_type": "payload"}),
            {},
        )

    def test_patient_fields_exclude_internal_audit_fields(self):
        self.assertEqual(
            set(PatientActivity.model_fields),
            {
                "id",
                "action",
                "target_type",
                "target_id",
                "outcome",
                "created_at",
            },
        )

    def test_ownership_is_resolved_against_resource_not_actor(self):
        sql = str(
            patient_predicate(uuid4()).compile(
                dialect=postgresql.dialect()
            )
        )

        self.assertIn("measurements.patient_user_id", sql)
        self.assertIn("access_grants.patient_user_id", sql)
        self.assertNotIn("actor_user_id", sql)


class RetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = MagicMock()
        self.db.commit = AsyncMock()
        self.db.rollback = AsyncMock()

        self.actor = SimpleNamespace(
            id=uuid4(),
            status="active",
        )
        self.job = SimpleNamespace(
            id=uuid4(),
            job_type="operations.database_check",
            status="failed",
            attempt_count=1,
            last_error_code="JOB_FAILED",
            created_at=datetime.now(UTC),
            started_at=None,
            finished_at=None,
        )

    async def call_retry(self, existing=None):
        with (
            patch.object(service, "require_admin", AsyncMock()),
            patch.object(
                service,
                "lock_and_replay",
                AsyncMock(return_value=None),
            ),
            patch.object(service, "save_success", AsyncMock()),
            patch.object(
                service.repo,
                "lock_job",
                AsyncMock(return_value=self.job),
            ),
            patch.object(
                service.repo,
                "open_retry",
                AsyncMock(return_value=existing),
            ),
            patch.object(service, "append_event", AsyncMock()),
        ):
            return await service.retry(
                self.db,
                self.actor,
                self.job.id,
                "Retry after recovery",
                str(uuid4()),
                idempotency_key=str(uuid4()),
            )

    async def test_retry_is_persisted_and_previous_failure_is_retained(self):
        await self.call_retry()

        item = self.db.add.call_args.args[0]

        self.assertEqual(item.reason, "Retry after recovery")
        self.assertEqual(item.attempt_number, 2)
        self.assertEqual(self.job.last_error_code, "JOB_FAILED")
        self.db.commit.assert_awaited_once()

    async def test_existing_retry_does_not_enqueue_again(self):
        existing = SimpleNamespace(
            requested_by=self.actor.id,
        )

        await self.call_retry(existing)

        self.db.add.assert_not_called()

    async def test_retry_limit_is_enforced(self):
        self.job.attempt_count = 3

        with self.assertRaises(HTTPException) as ctx:
            await self.call_retry()

        self.assertEqual(ctx.exception.status_code, 409)
        self.db.add.assert_not_called()

    async def test_unknown_job_type_is_not_executed(self):
        self.job.job_type = "some.module.execute"

        with self.assertRaises(HTTPException) as ctx:
            await self.call_retry()

        self.assertEqual(ctx.exception.status_code, 409)

    async def test_running_job_cannot_be_retried(self):
        self.job.status = "running"

        with self.assertRaises(HTTPException):
            await self.call_retry()

    async def test_failed_audit_rolls_back_queue(self):
        with (
            patch.object(service, "require_admin", AsyncMock()),
            patch.object(
                service,
                "lock_and_replay",
                AsyncMock(return_value=None),
            ),
            patch.object(service, "save_success", AsyncMock()),
            patch.object(
                service.repo,
                "lock_job",
                AsyncMock(return_value=self.job),
            ),
            patch.object(
                service.repo,
                "open_retry",
                AsyncMock(return_value=None),
            ),
            patch.object(
                service,
                "append_event",
                AsyncMock(side_effect=RuntimeError),
            ),
        ):
            with self.assertRaises(RuntimeError):
                await service.retry(
                    self.db,
                    self.actor,
                    self.job.id,
                    "retry",
                    str(uuid4()),
                    idempotency_key=str(uuid4()),
                )

        self.db.rollback.assert_awaited_once()
        self.db.commit.assert_not_awaited()

    async def test_patient_cannot_read_another_patients_activity(self):
        with patch.object(service, "require_role", AsyncMock()):
            with self.assertRaises(HTTPException) as ctx:
                await service.activity(
                    self.db,
                    self.actor,
                    uuid4(),
                    1,
                    20,
                )

        self.assertEqual(ctx.exception.status_code, 404)


class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = FastAPI()
        self.app.include_router(router, prefix="/api/v1")

        async def actor():
            return SimpleNamespace(id=uuid4())

        async def db():
            yield MagicMock()

        async def quota():
            return None

        self.app.dependency_overrides[get_current_user] = actor
        self.app.dependency_overrides[get_session] = db
        self.app.dependency_overrides[
            require_authenticated_rate_limit
        ] = quota

        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://test",
        )

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_invalid_status_and_page_are_rejected(self):
        for query in (
            "status=unknown",
            "page=0",
            "page_size=101",
            "extra=1",
        ):
            response = await self.client.get(
                "/api/v1/admin/jobs?" + query
            )

            self.assertEqual(response.status_code, 422)
            self.assertEqual(
                response.headers["cache-control"],
                "no-store",
            )

    async def test_non_admin_denied_with_contract_error(self):
        with patch.object(
            service,
            "require_admin",
            AsyncMock(
                side_effect=HTTPException(403, "Access denied.")
            ),
        ):
            response = await self.client.get(
                "/api/v1/admin/operations"
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json()["code"],
            "ACCESS_DENIED",
        )

    async def test_retry_requires_valid_idempotency_header(self):
        for headers in (
            {},
            {"Idempotency-Key": "short"},
            {"Idempotency-Key": "x" * 129},
        ):
            response = await self.client.post(
                f"/api/v1/admin/jobs/{uuid4()}/retry",
                json={"reason": "Recovery"},
                headers=headers,
            )

            self.assertEqual(response.status_code, 422)

    async def test_all_six_routes_registered(self):
        paths = self.app.openapi()["paths"]

        self.assertEqual(len(paths), 6)
        self.assertIn("/api/v1/admin/backups", paths)
        self.assertEqual(
            set(paths["/api/v1/admin/audit-events"]),
            {"get"},
        )


class ReviewRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_retry_is_replayed_without_reading_current_job(self):
        db = MagicMock()
        db.commit = AsyncMock()
        db.rollback = AsyncMock()

        actor = SimpleNamespace(id=uuid4())
        response = {
            "id": str(uuid4()),
            "job_type": "operations.database_check",
            "status": "pending",
            "attempt_count": 1,
            "error_code": "JOB_FAILED",
            "updated_at": datetime.now(UTC).isoformat(),
        }

        with (
            patch.object(
                service,
                "require_admin",
                AsyncMock(),
            ) as authorize,
            patch.object(
                service,
                "lock_and_replay",
                AsyncMock(
                    return_value={"response_body": response}
                ),
            ),
            patch.object(
                service.repo,
                "lock_job",
                AsyncMock(),
            ) as lock_job,
            patch.object(
                service,
                "save_success",
                AsyncMock(),
            ) as save,
            patch.object(
                service,
                "append_event",
                AsyncMock(),
            ) as audit,
        ):
            result = await service.retry(
                db,
                actor,
                response["id"],
                "Recovery",
                str(uuid4()),
                idempotency_key="retry-test-key-1",
            )

        self.assertEqual(result.status, "pending")
        self.assertEqual(result.attempt_count, 1)
        authorize.assert_awaited_once()
        lock_job.assert_not_awaited()
        save.assert_not_awaited()
        audit.assert_not_awaited()
        db.add.assert_not_called()

    async def test_retry_key_payload_mismatch_returns_conflict(self):
        from app.core.idempotency import IdempotencyConflict

        db = MagicMock()
        db.rollback = AsyncMock()

        with (
            patch.object(
                service,
                "require_admin",
                AsyncMock(),
            ),
            patch.object(
                service,
                "lock_and_replay",
                AsyncMock(side_effect=IdempotencyConflict),
            ),
        ):
            with self.assertRaises(HTTPException) as ctx:
                await service.retry(
                    db,
                    SimpleNamespace(id=uuid4()),
                    uuid4(),
                    "changed",
                    str(uuid4()),
                    idempotency_key="retry-test-key-1",
                )

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(
            ctx.exception.detail["code"],
            "IDEMPOTENCY_CONFLICT",
        )
        db.rollback.assert_awaited_once()

    async def test_jobs_returns_query_status_without_later_retry_lookup(self):
        row = {
            "id": uuid4(),
            "job_type": "operations.database_check",
            "status": "failed",
            "attempt_count": 1,
            "error_code": "JOB_FAILED",
            "updated_at": datetime.now(UTC),
        }

        with (
            patch.object(
                service,
                "require_admin",
                AsyncMock(),
            ),
            patch.object(
                service.repo,
                "job_page",
                AsyncMock(return_value=([row], 1)),
            ),
            patch.object(
                service.repo,
                "open_retry",
                AsyncMock(side_effect=AssertionError),
            ) as lookup,
        ):
            result = await service.jobs(
                MagicMock(),
                SimpleNamespace(id=uuid4()),
                1,
                20,
                "failed",
            )

        self.assertEqual(result.items[0].status, "failed")
        lookup.assert_not_awaited()