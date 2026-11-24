"""ML-124 HTTP/PostgreSQL tests using the existing rollback-isolated fixtures.

Set ML116_TEST_DATABASE_URL to a migrated disposable test database.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from app.modules.documents.models import Document
from app.modules.laboratory.models import LabUploadRequest, Report
from tests import test_report_api_postgres as fixtures


class ReportCorrectionPostgresTests(fixtures.ReportPostgresTests):
    async def correct(self, original, document_id, *, key=None, reason="Correct result"):
        return await self.client.post(
            f"/api/v1/reports/{original['id']}/versions",
            json={"document_id": document_id, "report_type": "FBC", "reason": reason},
            headers={"Idempotency-Key": key or str(uuid4())},
        )

    async def test_expired_request_correction_replay_and_authorization(self):
        request_id, doc_id = await self.staged()
        original = await self.publish(doc_id, request_id)
        self.assertEqual(original.status_code, 201, original.text)
        original = original.json()
        request = await self.db.get(LabUploadRequest, request_id)
        request.expires_at = datetime.now(UTC) - timedelta(days=1)
        await self.db.commit()
        upload = await self.client.post(
            f"/api/v1/patients/{self.patient_id}/documents",
            data={
                "document_type": "report",
                "supersedes_id": original["document_id"],
                "stage_for_report": "true",
            },
            files={"file": ("corrected.pdf", self.pdf, "application/pdf")},
        )
        self.assertEqual(upload.status_code, 201, upload.text)
        document_id = upload.json()["id"]
        self.assertEqual((await self.db.get(Report, UUID(original["id"]))).status, "active")
        for actor_id in (self.other_lab_user_id, self.patient_id, self.other_patient_id):
            self.actor_id = actor_id
            denied = await self.correct(original, document_id)
            self.assertEqual(denied.status_code, 403, denied.text)
        self.actor_id = self.lab_user_id
        key = str(uuid4())
        corrected = await self.correct(original, document_id, key=key)
        self.assertEqual(corrected.status_code, 201, corrected.text)
        replay = await self.correct(original, document_id, key=key)
        self.assertEqual(replay.json(), corrected.json())
        conflict = await self.correct(original, document_id, key=key, reason="Different")
        self.assertEqual(conflict.status_code, 409, conflict.text)
        history = await self.client.get(f"/api/v1/reports/{original['id']}/history")
        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual(history.json()["total"], 2)
        denied = await self.upload(request_id=request_id, stage=True)
        self.assertEqual(denied.status_code, 403, denied.text)

    async def test_patient_correction_withdrawal_preserves_history_and_blocks_files(self):
        self.actor_id = self.patient_id
        upload = await self.upload()
        original = await self.publish(UUID(upload.json()["id"]))
        self.assertEqual(original.status_code, 201, original.text)
        original = original.json()
        replacement = await self.upload()
        corrected = await self.correct(original, replacement.json()["id"])
        self.assertEqual(corrected.status_code, 201, corrected.text)
        current = corrected.json()
        withdrawn = await self.client.post(
            f"/api/v1/reports/{current['id']}/withdraw",
            json={"reason": "Wrong report"},
        )
        self.assertEqual(withdrawn.status_code, 201, withdrawn.text)
        self.assertEqual(withdrawn.json()["withdrawal_reason"], "Wrong report")
        for document_id in (original["document_id"], current["document_id"]):
            denied = await self.client.get(f"/api/v1/documents/{document_id}/content")
            self.assertEqual(denied.status_code, 404, denied.text)
            doc = await self.db.get(Document, UUID(document_id))
            self.assertEqual(doc.status, "withdrawn")
            self.assertTrue(doc.storage_key)
        history = await self.client.get(f"/api/v1/reports/{original['id']}/history")
        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual(history.json()["total"], 2)
        repeated = await self.client.post(
            f"/api/v1/reports/{current['id']}/withdraw",
            json={"reason": "Again"},
        )
        self.assertEqual(repeated.status_code, 409, repeated.text)
