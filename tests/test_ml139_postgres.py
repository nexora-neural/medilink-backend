"""Integration report workflow against a migrated disposable PostgreSQL database."""

import hashlib
import os
from datetime import UTC, datetime, timedelta
from unittest import skipUnless
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.database.session import get_session
from app.modules.audit.models import AuditEvent
from app.modules.integrations import report_service
from app.modules.integrations.auth import get_active_integration_client
from app.modules.integrations.models import IntegrationClient
from app.modules.integrations.report_router import router
from app.modules.laboratory.models import LabUploadRequest, Report
from tests.test_report_api_postgres import ReportPostgresTests


@skipUnless(os.getenv("ML116_TEST_DATABASE_URL"), "Requires a disposable PostgreSQL database")
class IntegrationReportPostgresTests(ReportPostgresTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.token = f"ml139-{uuid4()}"
        lab_request = LabUploadRequest(
            patient_user_id=self.patient_id,
            laboratory_id=self.lab_id,
            request_token_hash=hashlib.sha256(self.token.encode()).hexdigest(),
            status="active",
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
        integration = IntegrationClient(
            organization_id=self.lab_id,
            name="Synthetic lab",
            credential_hash=hashlib.sha256(uuid4().bytes).hexdigest(),
            scopes=["reports:write"],
            status="active",
            expires_at=datetime.now(UTC) + timedelta(days=1),
            created_by=self.lab_user_id,
        )
        self.db.add_all([lab_request, integration])
        await self.db.flush()
        self.integration_id = integration.id
        await self.db.commit()
        app = FastAPI()
        app.include_router(router, prefix="/api/v1")

        @app.middleware("http")
        async def request_id(request, call_next):
            request.state.request_id = str(uuid4())
            return await call_next(request)

        async def get_db():
            yield self.db

        async def get_client():
            return await self.db.get(IntegrationClient, self.integration_id)

        app.dependency_overrides[get_session] = get_db
        app.dependency_overrides[get_active_integration_client] = get_client
        self.integration_http = AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
        )
        self.addAsyncCleanup(self.integration_http.aclose)

    async def send_report(self, *, key, external, report_id=None, **fields):
        data = {
            "request_token": self.token,
            "external_report_id": external,
            "lab_reference": "ML139-REF",
            "report_type": "Synthetic test",
            **fields,
        }
        path = "/api/v1/integrations/reports"
        if report_id:
            path += f"/{report_id}/versions"
        return await self.integration_http.post(
            path,
            data=data,
            files={"file": ("test.pdf", self.pdf, "application/pdf")},
            headers={"Idempotency-Key": key},
        )

    async def test_submission_retry_and_correction_history(self):
        first = await self.send_report(key="ml139-key-one", external="initial")
        self.assertEqual(first.status_code, 201, first.text)
        retry = await self.send_report(key="ml139-key-one", external="initial")
        self.assertEqual(retry.status_code, 201, retry.text)
        self.assertEqual(first.json(), retry.json())
        replay = await self.db.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "integration.report_replayed",
                AuditEvent.target_id == UUID(first.json()["id"]),
            )
        )
        self.assertIsNotNone(replay)
        corrected = await self.send_report(
            key="ml139-key-two",
            external="corrected",
            report_id=first.json()["report_id"],
            reason="Synthetic correction",
        )
        self.assertEqual(corrected.status_code, 201, corrected.text)
        old = await self.db.get(Report, UUID(first.json()["report_id"]))
        new = await self.db.get(Report, UUID(corrected.json()["report_id"]))
        self.assertEqual(old.status, "superseded")
        self.assertEqual(new.supersedes_id, old.id)
        self.assertEqual(new.correction_reason, "Synthetic correction")

    async def test_duplicate_external_id_and_key_conflict(self):
        first = await self.send_report(key="ml139-original", external="same-id")
        self.assertEqual(first.status_code, 201, first.text)
        duplicate = await self.send_report(key="ml139-another", external="same-id")
        self.assertEqual(duplicate.status_code, 201, duplicate.text)
        self.assertEqual(duplicate.json(), first.json())
        conflict = await self.send_report(
            key="ml139-original", external="same-id", report_type="Changed report"
        )
        self.assertEqual(conflict.status_code, 409, conflict.text)
        conflict_id = await self.send_report(
            key="ml139-third-key", external="same-id", report_type="Changed report"
        )
        self.assertEqual(conflict_id.status_code, 409, conflict_id.text)

    async def test_bad_token_blank_input_and_missing_reason(self):
        invalid = await self.send_report(
            key="ml139-invalid", external="invalid", request_token="incorrect"
        )
        self.assertEqual(invalid.status_code, 403, invalid.text)
        blank = await self.send_report(key="ml139-blank-key", external="blank", report_type="   ")
        self.assertEqual(blank.status_code, 422, blank.text)
        first = await self.send_report(key="ml139-valid-key", external="initial")
        self.assertEqual(first.status_code, 201, first.text)
        no_reason = await self.send_report(
            key="ml139-correct-key", external="version", report_id=first.json()["report_id"]
        )
        self.assertEqual(no_reason.status_code, 422, no_reason.text)

    async def test_storage_failure_rolls_back_request_and_report(self):
        with patch.object(
            report_service.storage,
            "save",
            new_callable=AsyncMock,
            side_effect=OSError("synthetic storage failure"),
        ):
            response = await self.send_report(key="ml139-failure", external="failure")
        self.assertEqual(response.status_code, 500, response.text)
        request = await self.db.scalar(
            select(LabUploadRequest).where(
                LabUploadRequest.request_token_hash
                == hashlib.sha256(self.token.encode()).hexdigest()
            )
        )
        self.assertEqual(request.status, "active")
        reports = (
            await self.db.scalars(select(Report).where(Report.patient_user_id == self.patient_id))
        ).all()
        self.assertEqual(reports, [])

    async def test_other_client_cannot_correct_report(self):
        first = await self.send_report(key="ml139-first-key", external="initial")
        self.assertEqual(first.status_code, 201, first.text)
        other = IntegrationClient(
            organization_id=self.lab_id,
            name="Other synthetic client",
            credential_hash=hashlib.sha256(uuid4().bytes).hexdigest(),
            scopes=["reports:write"],
            status="active",
            expires_at=datetime.now(UTC) + timedelta(days=1),
            created_by=self.lab_user_id,
        )
        self.db.add(other)
        await self.db.flush()
        self.integration_id = other.id
        await self.db.commit()
        denied = await self.send_report(
            key="ml139-other-key",
            external="other",
            report_id=first.json()["report_id"],
            reason="Synthetic correction",
        )
        self.assertEqual(denied.status_code, 404, denied.text)

    async def test_failure_after_storage_deletes_blob_and_rolls_back(self):
        with patch.object(
            report_service,
            "append_event",
            new_callable=AsyncMock,
            side_effect=RuntimeError("synthetic audit failure"),
        ):
            response = await self.send_report(key="ml139-audit-fail", external="audit-fail")
        self.assertEqual(response.status_code, 500, response.text)
        self.storage["delete"].assert_awaited_once()
        request = await self.db.scalar(
            select(LabUploadRequest).where(
                LabUploadRequest.request_token_hash
                == hashlib.sha256(self.token.encode()).hexdigest()
            )
        )
        self.assertEqual(request.status, "active")
        self.assertEqual(
            (
                await self.db.scalars(
                    select(Report).where(Report.patient_user_id == self.patient_id)
                )
            ).all(),
            [],
        )

    async def test_authentication_rejects_missing_and_invalid_credentials(self):
        app = self.integration_http._transport.app
        app.dependency_overrides.pop(get_active_integration_client)
        with patch(
            "app.modules.integrations.auth.require_integration_rate_limit", new_callable=AsyncMock
        ):
            missing = await self.send_report(key="ml139-auth-key", external="auth")
            self.assertEqual(missing.status_code, 401, missing.text)
            invalid = await self.integration_http.post(
                "/api/v1/integrations/reports",
                data={
                    "request_token": self.token,
                    "external_report_id": "auth",
                    "lab_reference": "ref",
                    "report_type": "test",
                },
                files={"file": ("test.pdf", self.pdf, "application/pdf")},
                headers={"Idempotency-Key": "ml139-auth-key", "X-API-Key": "invalid"},
            )
            self.assertEqual(invalid.status_code, 401, invalid.text)

    async def test_manual_reference_collision_is_rejected(self):
        manual_request_id, document_id = await self.staged()
        manual = await self.publish(document_id, manual_request_id, lab_reference="ML139-REF")
        self.assertEqual(manual.status_code, 201, manual.text)
        duplicate = await self.send_report(key="ml139-cross-channel", external="duplicate")
        self.assertEqual(duplicate.status_code, 409, duplicate.text)
        records = (
            await self.db.scalars(select(Report).where(Report.patient_user_id == self.patient_id))
        ).all()
        self.assertEqual(len(records), 1)
