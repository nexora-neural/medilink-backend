from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import FastAPI

from app.modules.laboratory import report_service
from app.modules.laboratory.report_schemas import ReportInput
from app.modules.laboratory.router import router


def test_report_contract_routes():
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    paths = app.openapi()["paths"]
    patient = paths["/api/v1/patients/{patient_id}/reports"]
    assert "201" in patient["post"]["responses"]
    assert any(
        param["name"] == "Idempotency-Key" and param["required"]
        for param in patient["post"]["parameters"]
    )
    assert "200" in patient["get"]["responses"]
    assert "200" in paths["/api/v1/reports/{report_id}"]["get"]["responses"]
    assert "200" in paths["/api/v1/me/laboratory-reports"]["get"]["responses"]


class TestReportReplay(IsolatedAsyncioTestCase):
    async def test_lab_retry_replays_after_upload_request_is_used(self):
        db = AsyncMock()
        actor = SimpleNamespace(id=uuid4())
        patient_id = uuid4()
        laboratory_id = uuid4()
        request_id = uuid4()
        document_id = uuid4()
        payload = {
            "id": str(uuid4()),
            "patient_user_id": str(patient_id),
            "laboratory_id": str(laboratory_id),
            "report_identity_id": str(uuid4()),
            "upload_request_id": str(request_id),
            "document_id": str(document_id),
            "report_type": "FBC",
            "report_date": None,
            "notes": None,
            "source": "lab_staff",
            "uploaded_by": str(actor.id),
            "status": "active",
            "created_at": "2026-11-21T00:00:00Z",
            "supersedes_id": None,
            "correction_reason": None,
        }
        body = ReportInput(
            document_id=document_id, report_type="FBC",
            upload_request_id=request_id, lab_reference="LAB-1",
        )
        with (
            patch.object(report_service, "require_role"),
            patch.object(report_service, "require_verified_membership"),
            patch.object(report_service.documents, "lab_request", return_value=SimpleNamespace(
                patient_user_id=patient_id, laboratory_id=laboratory_id, status="used",
            )),
            patch.object(
                report_service, "lock_and_replay", return_value={"response_body": payload},
            ),
            patch.object(report_service.documents, "document_by_id") as document_by_id,
        ):
            result = await report_service.publish_report(
                db, actor, patient_id, body, "same-key", "audit-request",
            )
        assert str(result.id) == payload["id"]
        document_by_id.assert_not_awaited()
        db.commit.assert_not_awaited()
