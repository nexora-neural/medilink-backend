"""ML-124 HTTP/PostgreSQL regression tests.

Set ML116_TEST_DATABASE_URL to a migrated disposable test database.
Physical test files are isolated in a temporary directory.
"""

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import UUID, uuid4

from app.modules.documents import storage
from app.modules.documents.models import Document
from app.modules.laboratory.models import LabUploadRequest, Report
from app.modules.notifications.models import Notification
from tests import test_report_api_postgres as fixtures


class ReportCorrectionPostgresTests(fixtures.ReportPostgresTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()

        temporary_directory = TemporaryDirectory(prefix="ml124-tests-")
        self.addCleanup(temporary_directory.cleanup)
        self.upload_root = Path(temporary_directory.name)

        root_patch = patch.object(
            storage, "_root", return_value=self.upload_root
        )
        root_patch.start()
        self.addCleanup(root_patch.stop)

        async def save(upload):
            content = await upload.read()
            key = uuid4().hex
            (self.upload_root / key).write_bytes(content)
            return key, len(content), hashlib.sha256(content).hexdigest()

        async def delete(key):
            (self.upload_root / key).unlink(missing_ok=True)

        self.storage["save"].side_effect = save
        self.storage["delete"].side_effect = delete

    async def correct(
        self, original, document_id, *, key=None, reason="Correct result"
    ):
        return await self.client.post(
            f"/api/v1/reports/{original['id']}/versions",
            json={
                "document_id": str(document_id),
                "report_type": "FBC",
                "reason": reason,
            },
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

        self.assertEqual(
            (await self.db.get(Report, UUID(original["id"]))).status,
            "active",
        )

        for actor_id in (
            self.other_lab_user_id,
            self.patient_id,
            self.other_patient_id,
        ):
            self.actor_id = actor_id
            denied = await self.correct(original, document_id)
            self.assertEqual(denied.status_code, 403, denied.text)

        self.actor_id = self.lab_user_id
        key = str(uuid4())

        corrected = await self.correct(original, document_id, key=key)
        self.assertEqual(corrected.status_code, 201, corrected.text)

        replay = await self.correct(original, document_id, key=key)
        self.assertEqual(replay.status_code, 201, replay.text)
        self.assertEqual(replay.json(), corrected.json())

        self.assertEqual(
            await self.count(
                Notification,
                Notification.related_id == UUID(corrected.json()["id"]),
                Notification.notification_type == "report_accepted",
            ),
            1,
        )

        conflict = await self.correct(
            original, document_id, key=key, reason="Different"
        )
        self.assertEqual(conflict.status_code, 409, conflict.text)

        history = await self.client.get(
            f"/api/v1/reports/{original['id']}/history"
        )
        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual(history.json()["total"], 2)

        denied = await self.upload(request_id=request_id, stage=True)
        self.assertEqual(denied.status_code, 403, denied.text)

    async def test_patient_correction_withdrawal_preserves_history_and_blocks_files(
        self,
    ):
        self.actor_id = self.patient_id

        upload = await self.upload()
        self.assertEqual(upload.status_code, 201, upload.text)

        original = await self.publish(UUID(upload.json()["id"]))
        self.assertEqual(original.status_code, 201, original.text)
        original = original.json()

        replacement = await self.upload()
        self.assertEqual(replacement.status_code, 201, replacement.text)

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
            denied = await self.client.get(
                f"/api/v1/documents/{document_id}/content"
            )
            self.assertEqual(denied.status_code, 404, denied.text)

            doc = await self.db.get(Document, UUID(document_id))
            self.assertEqual(doc.status, "withdrawn")
            self.assertTrue(
                storage.get_download_path(doc.storage_key).is_file()
            )

        history = await self.client.get(
            f"/api/v1/reports/{original['id']}/history"
        )
        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual(history.json()["total"], 2)

        repeated = await self.client.post(
            f"/api/v1/reports/{current['id']}/withdraw",
            json={"reason": "Again"},
        )
        self.assertEqual(repeated.status_code, 409, repeated.text)

    async def _assert_missing_replacement_rejected(self, original, document_id):
        replacement_id = UUID(document_id)
        replacement = await self.db.get(Document, replacement_id)
        storage_key = replacement.storage_key

        self.assertTrue(storage.get_download_path(storage_key).is_file())
        await storage.delete(storage_key)

        notification_count = await self.count(
            Notification, Notification.user_id == self.patient_id
        )
        await self.db.commit()

        key = str(uuid4())
        response = await self.correct(original, document_id, key=key)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["code"], "INVALID_STATE")

        original_report = await self.db.get(
            Report, UUID(original["id"]), populate_existing=True
        )
        original_document = await self.db.get(
            Document, UUID(original["document_id"]), populate_existing=True
        )
        replacement = await self.db.get(
            Document, replacement_id, populate_existing=True
        )

        self.assertEqual(original_report.status, "active")
        self.assertEqual(original_document.status, "active")
        self.assertEqual(replacement.status, "active")
        self.assertEqual(
            await self.count(Report, Report.document_id == replacement_id),
            0,
        )
        self.assertEqual(
            await self.count(
                Notification, Notification.user_id == self.patient_id
            ),
            notification_count,
        )

        history = await self.client.get(
            f"/api/v1/reports/{original['id']}/history"
        )
        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual(history.json()["total"], 1)

        # Restore only the synthetic test blob, then retry the same request.
        (self.upload_root / storage_key).write_bytes(self.pdf)

        retry = await self.correct(original, document_id, key=key)
        self.assertEqual(retry.status_code, 201, retry.text)
        self.assertEqual(retry.json()["supersedes_id"], original["id"])

    async def test_patient_missing_replacement_keeps_original_active(self):
        self.actor_id = self.patient_id

        upload = await self.upload()
        self.assertEqual(upload.status_code, 201, upload.text)

        published = await self.publish(UUID(upload.json()["id"]))
        self.assertEqual(published.status_code, 201, published.text)

        replacement = await self.upload()
        self.assertEqual(replacement.status_code, 201, replacement.text)

        await self._assert_missing_replacement_rejected(
            published.json(), replacement.json()["id"]
        )

    async def test_lab_missing_replacement_keeps_original_active(self):
        self.actor_id = self.lab_user_id

        request_id, document_id = await self.staged()
        published = await self.publish(document_id, request_id)
        self.assertEqual(published.status_code, 201, published.text)
        original = published.json()

        replacement = await self.client.post(
            f"/api/v1/patients/{self.patient_id}/documents",
            data={
                "document_type": "report",
                "supersedes_id": original["document_id"],
                "stage_for_report": "true",
            },
            files={"file": ("corrected.pdf", self.pdf, "application/pdf")},
        )
        self.assertEqual(replacement.status_code, 201, replacement.text)

        await self._assert_missing_replacement_rejected(
            original, replacement.json()["id"]
        )

    async def test_patient_correction_notifies_once_on_replay(self):
        self.actor_id = self.patient_id

        upload = await self.upload()
        self.assertEqual(upload.status_code, 201, upload.text)

        published = await self.publish(UUID(upload.json()["id"]))
        self.assertEqual(published.status_code, 201, published.text)
        original = published.json()

        replacement = await self.upload()
        self.assertEqual(replacement.status_code, 201, replacement.text)

        key = str(uuid4())
        corrected = await self.correct(
            original, replacement.json()["id"], key=key
        )
        self.assertEqual(corrected.status_code, 201, corrected.text)

        replay = await self.correct(
            original, replacement.json()["id"], key=key
        )
        self.assertEqual(replay.status_code, 201, replay.text)
        self.assertEqual(replay.json(), corrected.json())

        self.assertEqual(
            await self.count(
                Notification,
                Notification.user_id == self.patient_id,
                Notification.related_id == UUID(corrected.json()["id"]),
                Notification.notification_type == "report_accepted",
            ),
            1,
        )