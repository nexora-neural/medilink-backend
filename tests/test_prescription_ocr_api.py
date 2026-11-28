"""Contract-level tests for the patient prescription OCR endpoints."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app.database.session import get_session
from app.main import app
from app.modules.identity.auth import get_current_user
from app.modules.identity.request_limits import (
    require_authenticated_rate_limit,
    require_upload_rate_limit,
)
from app.modules.prescriptions import ocr_service
from app.modules.prescriptions.schemas import (
    PrescriptionOcrDraftCreated,
    PrescriptionOcrFields,
    PrescriptionOcrJobRead,
    PrescriptionOcrJobReadPage,
)


class PrescriptionOcrApiContractTests(TestCase):
    def setUp(self):
        self.patient_id = uuid4()
        self.job_id = uuid4()
        self.document_id = uuid4()
        self.user = SimpleNamespace(id=self.patient_id, status="active")

        async def no_limit():
            return None

        self.overrides = app.dependency_overrides.copy()
        app.dependency_overrides.update(
            {
                get_session: lambda: None,
                get_current_user: lambda: self.user,
                require_authenticated_rate_limit: no_limit,
                require_upload_rate_limit: no_limit,
            }
        )
        self.addCleanup(self._restore_overrides)
        self.client = TestClient(app, raise_server_exceptions=False)

    def _restore_overrides(self):
        app.dependency_overrides.clear()
        app.dependency_overrides.update(self.overrides)

    def job_read(self, *, status="queued"):
        return PrescriptionOcrJobRead(
            id=self.job_id,
            patient_user_id=self.patient_id,
            original_document_id=self.document_id,
            status=status,
            extracted_fields=None,
            attempt_count=0,
            error_code=None,
            resulting_prescription_id=None,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

    def test_start_requires_idempotency_key_and_exact_body(self):
        path = f"/api/v1/patients/{self.patient_id}/prescription-ocr-jobs"
        with patch.object(ocr_service, "create_job", new_callable=AsyncMock) as create:
            response = self.client.post(path, json={"original_document_id": str(self.document_id)})
            self.assertEqual(response.status_code, 422)
            create.assert_not_awaited()

            response = self.client.post(
                path,
                json={
                    "original_document_id": str(self.document_id),
                    "status": "doctor_issued",
                },
                headers={"Idempotency-Key": "ml152-create-1"},
            )
            self.assertEqual(response.status_code, 422)
            create.assert_not_awaited()

            create.return_value = self.job_read()
            response = self.client.post(
                path,
                json={"original_document_id": str(self.document_id)},
                headers={"Idempotency-Key": "ml152-create-1"},
            )

        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["status"], "queued")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        create.assert_awaited_once()

    def test_list_filter_pagination_and_no_store(self):
        path = f"/api/v1/patients/{self.patient_id}/prescription-ocr-jobs"
        result = PrescriptionOcrJobReadPage(
            items=[self.job_read(status="ready_for_review")],
            page=2,
            page_size=1,
            total=2,
        )
        with patch.object(
            ocr_service, "list_jobs", new_callable=AsyncMock, return_value=result
        ) as listing:
            response = self.client.get(
                path,
                params={"status": "ready_for_review", "page": 2, "page_size": 1},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["total"], 2)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            listing.assert_awaited_once()

            invalid = self.client.get(path, params={"status": "doctor_issued"})
            self.assertEqual(invalid.status_code, 422)
            unknown = self.client.get(path, params={"page": 1, "unexpected": "private"})
            self.assertEqual(unknown.status_code, 422)
            self.assertNotIn("private", unknown.text)

    def test_read_retry_and_draft_routes_return_contract_schemas(self):
        read_path = f"/api/v1/prescription-ocr-jobs/{self.job_id}"
        retry_path = f"{read_path}/retry"
        draft_path = (
            f"/api/v1/patients/{self.patient_id}/prescription-ocr-jobs/"
            f"{self.job_id}/draft"
        )
        prescription_id = uuid4()
        draft = PrescriptionOcrDraftCreated.model_validate(
            {
                "job": self.job_read(status="draft_created").model_dump(mode="json"),
                "prescription": {
                    "prescription": {
                        "id": str(prescription_id),
                        "patient_user_id": str(self.patient_id),
                        "consultation_id": None,
                        "prescriber_user_id": None,
                        "source": "patient",
                        "claimed_prescriber": None,
                        "review_status": "not_reviewed",
                        "reviewed_by": None,
                        "reviewed_at": None,
                        "status": "draft",
                        "issued_at": None,
                        "starts_on": None,
                        "ends_on": None,
                        "previous_prescription_id": None,
                        "original_document_id": str(self.document_id),
                        "created_by": str(self.patient_id),
                        "created_at": datetime.now(UTC).isoformat(),
                        "updated_at": datetime.now(UTC).isoformat(),
                        "version": 1,
                        "status_reason": None,
                    },
                    "items": [],
                    "reviews": [],
                },
            }
        )
        ocr_read = self.job_read(status="ready_for_review")
        ocr_read.extracted_fields = PrescriptionOcrFields(
            items=[
                {
                    "medicine_name_snapshot": "Amoxicillin",
                    "dose_text": None,
                    "frequency": "three times a day",
                    "is_prn": False,
                }
            ]
        )
        with (
            patch.object(
                ocr_service,
                "read_job",
                new_callable=AsyncMock,
                return_value=ocr_read,
            ),
            patch.object(
                ocr_service,
                "retry_job",
                new_callable=AsyncMock,
                return_value=self.job_read(),
            ),
            patch.object(
                ocr_service,
                "create_reviewed_draft",
                new_callable=AsyncMock,
                return_value=draft,
            ),
        ):
            read = self.client.get(read_path)
            retry = self.client.post(retry_path, json={"reason": "Synthetic retry"})
            draft_response = self.client.post(
                draft_path,
                json={"items": [{
                    "medicine_name_snapshot": "Example",
                    "dose_text": "500 mg",
                    "frequency": "once daily",
                    "is_prn": False,
                }]},
                headers={"Idempotency-Key": "ml152-draft-1"},
            )

        self.assertEqual(read.status_code, 200, read.text)
        self.assertEqual(read.json()["status"], "ready_for_review")
        self.assertIsNone(read.json()["extracted_fields"]["items"][0]["dose_text"])
        self.assertEqual(retry.status_code, 202, retry.text)
        self.assertEqual(draft_response.status_code, 201, draft_response.text)
        self.assertEqual(draft_response.json()["prescription"]["prescription"]["source"], "patient")
        self.assertEqual(draft_response.json()["prescription"]["prescription"]["status"], "draft")

    def test_openapi_includes_the_five_approved_operations(self):
        spec = self.client.get("/openapi.json").json()
        expected = {
            ("/api/v1/patients/{patient_id}/prescription-ocr-jobs", "post", "202"),
            ("/api/v1/patients/{patient_id}/prescription-ocr-jobs", "get", "200"),
            ("/api/v1/prescription-ocr-jobs/{job_id}", "get", "200"),
            ("/api/v1/prescription-ocr-jobs/{job_id}/retry", "post", "202"),
            (
                "/api/v1/patients/{patient_id}/prescription-ocr-jobs/{job_id}/draft",
                "post",
                "201",
            ),
        }
        for path, method, success_code in expected:
            with self.subTest(path=path, method=method):
                operation = spec["paths"][path][method]
                self.assertIn(success_code, operation["responses"])
                self.assertIn("401", operation["responses"])
                self.assertIn("403", operation["responses"])
                self.assertIn("404", operation["responses"])
                self.assertIn("409", operation["responses"])
                self.assertIn("422", operation["responses"])
        for path, method in (
            ("/api/v1/patients/{patient_id}/prescription-ocr-jobs", "post"),
            (
                "/api/v1/patients/{patient_id}/prescription-ocr-jobs/{job_id}/draft",
                "post",
            ),
        ):
            params = spec["paths"][path][method]["parameters"]
            self.assertTrue(
                any(item["name"] == "Idempotency-Key" and item["required"] for item in params)
            )
        ocr_item = spec["components"]["schemas"]["PrescriptionOcrItemFields"]
        self.assertIn(
            {"type": "null"},
            ocr_item["properties"]["dose_text"]["anyOf"],
        )
        self.assertNotIn("dose_text", ocr_item["required"])
