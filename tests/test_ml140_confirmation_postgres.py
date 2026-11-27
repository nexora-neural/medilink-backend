"""Patient-added confirmation on migrated PostgreSQL with synthetic records."""

import os
from datetime import UTC, date, datetime
from unittest import IsolatedAsyncioTestCase, skipUnless
from unittest.mock import patch
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.database.session import get_session
from app.main import app
from app.modules.audit.models import AuditEvent
from app.modules.dispensing.models import DispensingEventsModel
from app.modules.documents.models import Document
from app.modules.identity.auth import get_current_user
from app.modules.identity.models import Role, User, UserRole
from app.modules.identity.request_limits import require_authenticated_rate_limit
from app.modules.medication.models import MedicationSchedule
from app.modules.prescriptions import service
from app.modules.prescriptions.core_models import Prescription, PrescriptionItem
from app.modules.prescriptions.models import PrescriptionSharesModel
from app.modules.profiles.models import PatientProfile

URL = os.getenv("ML140_TEST_DATABASE_URL")


async def synthetic_patient(db):
    role = await db.scalar(select(Role).where(Role.code == "patient"))
    if role is None:
        role = Role(code="patient", label="Patient")
        db.add(role)
        await db.flush()
    patient = User(firebase_uid=f"ml140-{uuid4()}", display_name="Synthetic ML140", status="active")
    db.add(patient)
    await db.flush()
    db.add(PatientProfile(user_id=patient.id, date_of_birth=date(1990, 1, 1), sex="female"))
    db.add(UserRole(user_id=patient.id, role_id=role.id))
    await db.flush()
    return patient


async def synthetic_draft(db, patient_id):
    rx = Prescription(
        patient_user_id=patient_id,
        source="patient",
        created_by=patient_id,
        claimed_prescriber="Unverified imported label",
        status="draft",
    )
    db.add(rx)
    await db.flush()
    db.add(
        PrescriptionItem(
            prescription_id=rx.id,
            medicine_name_snapshot="Synthetic medicine",
            dose_text="Original dose",
            frequency="daily",
            is_prn=False,
            instructions="Original instructions",
        )
    )
    await db.flush()
    return rx


@skipUnless(URL, "Set ML140_TEST_DATABASE_URL to a migrated disposable PostgreSQL database")
class ConfirmationPostgresTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        engine = create_async_engine(URL)
        self.addAsyncCleanup(engine.dispose)
        connection = await engine.connect()
        self.addAsyncCleanup(connection.close)
        transaction = await connection.begin()
        self.addAsyncCleanup(transaction.rollback)
        self.db = AsyncSession(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        self.addAsyncCleanup(self.db.close)
        self.patient = await synthetic_patient(self.db)
        self.other = await synthetic_patient(self.db)
        self.rx = await synthetic_draft(self.db, self.patient.id)
        self.rx_id = self.rx.id
        self.patient_id = self.patient.id
        await self.db.commit()
        self.actor = self.patient

        async def session():
            try:
                yield self.db
            finally:
                for row in list(self.db.identity_map.values()):
                    await self.db.refresh(row)

        overrides = patch.dict(
            app.dependency_overrides,
            {
                get_session: session,
                get_current_user: lambda: self.actor,
                require_authenticated_rate_limit: lambda: None,
            },
        )
        overrides.start()
        self.addCleanup(overrides.stop)
        self.client = AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
        )
        self.addAsyncCleanup(self.client.aclose)
        self.path = f"/api/v1/prescriptions/{self.rx_id}/confirm"

    async def events(self):
        return list(
            (
                await self.db.scalars(
                    select(AuditEvent).where(
                        AuditEvent.target_id == self.rx_id,
                        AuditEvent.action == "prescription.patient.confirmed",
                    )
                )
            ).all()
        )

    async def test_confirm_keeps_source_labels_items_and_clinical_boundaries(self):
        before = (await self.client.get(f"/api/v1/prescriptions/{self.rx_id}")).json()
        response = await self.client.post(self.path)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        rx = body["prescription"]
        self.assertEqual(rx["source"], "patient")  # UI: Patient-added
        self.assertEqual(rx["review_status"], "not_reviewed")  # Not clinically verified
        self.assertEqual(rx["status"], "active")
        self.assertIsNone(rx["prescriber_user_id"])
        self.assertIsNone(rx["reviewed_by"])
        self.assertIsNone(rx["reviewed_at"])
        self.assertIsNone(rx["consultation_id"])
        self.assertEqual(rx["version"], 2)
        self.assertIsNotNone(rx["issued_at"])
        self.assertEqual(body["items"], before["items"])
        self.assertEqual(rx["claimed_prescriber"], before["prescription"]["claimed_prescriber"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertTrue(response.headers["X-Request-ID"])
        for model in (MedicationSchedule, PrescriptionSharesModel, DispensingEventsModel):
            self.assertEqual(await self.db.scalar(select(func.count()).select_from(model)), 0)
        self.assertNotIn("firebase_uid", response.text)
        self.assertNotIn("storage_key", response.text)

    async def test_audit_is_transactional_minimal_and_records_versions(self):
        self.assertEqual((await self.client.post(self.path)).status_code, 200)
        events = await self.events()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.actor_user_id, self.patient_id)
        self.assertEqual(
            event.metadata_,
            {
                "source": "patient",
                "previous_status": "draft",
                "status": "active",
                "previous_version": 1,
                "new_version": 2,
                "item_count": 1,
            },
        )
        self.assertNotIn("Original", str(event.metadata_))
        self.assertTrue(event.request_id)

    async def test_retries_do_not_change_timestamp_version_or_duplicate_audit(self):
        first = await self.client.post(self.path)
        again = await self.client.post(self.path)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(again.json(), first.json())
        self.assertEqual(len(await self.events()), 1)

    async def test_confirmed_prescription_cannot_be_draft_edited_or_deleted(self):
        self.assertEqual((await self.client.post(self.path)).status_code, 200)
        response = await self.client.put(
            f"/api/v1/prescriptions/{self.rx_id}/draft",
            headers={"If-Match": "2"},
            json={
                "items": [
                    {
                        "medicine_name_snapshot": "Forged rewrite",
                        "dose_text": "Changed",
                        "frequency": "daily",
                        "is_prn": False,
                    }
                ]
            },
        )
        self.assertEqual(response.status_code, 409, response.text)
        response = await self.client.delete(f"/api/v1/prescriptions/{self.rx_id}/draft")
        self.assertEqual(response.status_code, 409, response.text)

    async def test_other_patient_and_missing_records_are_concealed(self):
        self.actor = self.other
        response = await self.client.post(self.path)
        missing = await self.client.post(f"/api/v1/prescriptions/{uuid4()}/confirm")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(response.json()["message"], missing.json()["message"])
        self.assertEqual(self.rx.status, "draft")
        self.assertEqual(await self.events(), [])

    async def test_revoked_patient_role_and_suspended_account_are_rechecked(self):
        assignment = await self.db.scalar(
            select(UserRole).where(UserRole.user_id == self.patient_id)
        )
        assignment.revoked_at = datetime.now(UTC)
        await self.db.commit()
        self.assertEqual((await self.client.post(self.path)).status_code, 403)
        assignment.revoked_at = None
        self.patient.status = "suspended"
        await self.db.commit()
        self.assertEqual((await self.client.post(self.path)).status_code, 403)
        self.assertEqual(self.rx.status, "draft")

    async def test_privileged_and_doctor_roles_do_not_authorize_confirmation(self):
        assignment = await self.db.scalar(
            select(UserRole).where(UserRole.user_id == self.patient_id)
        )
        for code in ["doctor", "admin", "super_admin", "pharmacist"]:
            with self.subTest(role=code):
                role = await self.db.scalar(select(Role).where(Role.code == code))
                if role is None:
                    role = Role(code=code, label=code)
                    self.db.add(role)
                    await self.db.flush()
                assignment.role_id = role.id
                await self.db.commit()
                self.assertEqual((await self.client.post(self.path)).status_code, 403)
        self.assertEqual(self.rx.status, "draft")

    async def test_patient_cannot_confirm_doctor_or_external_source(self):
        for source in ["doctor", "approved_external_system"]:
            with self.subTest(source=source):
                self.rx.source = source
                self.rx.prescriber_user_id = self.other.id if source == "doctor" else None
                await self.db.commit()
                response = await self.client.post(self.path)
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(self.rx.status, "draft")
                self.assertIsNone(self.rx.issued_at)

    async def test_terminal_prescriptions_are_never_reactivated(self):
        for state in ["completed", "cancelled", "discontinued", "superseded"]:
            with self.subTest(state=state):
                self.rx.status = state
                self.rx.issued_at = datetime.now(UTC)
                await self.db.commit()
                response = await self.client.post(self.path)
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(self.rx.status, state)
                self.assertEqual(await self.events(), [])

    async def test_empty_oversized_and_invalid_saved_items_rejected(self):
        item = await self.db.scalar(
            select(PrescriptionItem).where(PrescriptionItem.prescription_id == self.rx_id)
        )
        item.dose_text = "   "
        await self.db.commit()
        self.assertEqual((await self.client.post(self.path)).status_code, 422)
        await self.db.delete(item)
        await self.db.commit()
        self.assertEqual((await self.client.post(self.path)).status_code, 422)
        for _ in range(101):
            self.db.add(
                PrescriptionItem(
                    prescription_id=self.rx_id,
                    medicine_name_snapshot="Synthetic",
                    dose_text="Synthetic",
                    frequency="daily",
                    is_prn=False,
                )
            )
        await self.db.commit()
        self.assertEqual((await self.client.post(self.path)).status_code, 422)
        self.assertEqual(self.rx.status, "draft")

    async def test_optional_quantity_course_and_route_remain_optional_for_patient(self):
        response = await self.client.post(self.path)
        self.assertEqual(response.status_code, 200, response.text)
        item = response.json()["items"][0]
        for key in ["quantity", "quantity_unit", "route", "duration_days"]:
            self.assertIsNone(item[key])
        self.assertIsNone(response.json()["prescription"]["starts_on"])
        self.assertIsNone(response.json()["prescription"]["ends_on"])

    async def test_evidence_must_still_be_active_owned_and_prescription_type(self):
        doc = Document(
            owner_user_id=self.patient_id,
            patient_user_id=self.patient_id,
            document_type="prescription",
            source="patient",
            original_filename="synthetic.pdf",
            storage_key=f"synthetic/{uuid4()}",
            media_type="application/pdf",
            byte_size=10,
        )
        self.db.add(doc)
        await self.db.flush()
        self.rx.original_document_id = doc.id
        await self.db.commit()
        for changes in [
            dict(status="withdrawn"),
            dict(status="quarantined"),
            dict(status="active", owner_user_id=self.other.id),
            dict(owner_user_id=self.patient_id, document_type="report"),
        ]:
            with self.subTest(changes=list(changes)):
                for key, value in changes.items():
                    setattr(doc, key, value)
                await self.db.commit()
                response = await self.client.post(self.path)
                self.assertEqual(response.status_code, 422, response.text)
        doc.document_type = "prescription"
        await self.db.commit()
        response = await self.client.post(self.path)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["prescription"]["original_document_id"], str(doc.id))

    async def test_audit_failure_rolls_back_every_state_change(self):
        with patch.object(
            service, "append_event", side_effect=RuntimeError("synthetic-private-failure")
        ):
            response = await self.client.post(self.path)
        self.assertEqual(response.status_code, 500, response.text)
        self.assertNotIn("synthetic-private-failure", response.text)
        self.assertEqual(self.rx.status, "draft")
        self.assertIsNone(self.rx.issued_at)
        self.assertEqual(self.rx.version, 1)
        self.assertEqual(await self.events(), [])
        self.assertEqual((await self.client.post(self.path)).status_code, 200)

    async def test_retry_does_not_reset_later_doctor_review(self):
        self.assertEqual((await self.client.post(self.path)).status_code, 200)
        self.rx.review_status = "approved"
        self.rx.reviewed_by = self.other.id
        self.rx.reviewed_at = datetime.now(UTC)
        await self.db.commit()
        response = await self.client.post(self.path)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["prescription"]["review_status"], "approved")
        self.assertEqual(response.json()["prescription"]["source"], "patient")
        self.assertIsNone(response.json()["prescription"]["prescriber_user_id"])
        self.assertEqual(len(await self.events()), 1)

    async def test_confirmed_record_is_visible_in_patient_list_and_read(self):
        self.assertEqual((await self.client.post(self.path)).status_code, 200)
        detail = await self.client.get(f"/api/v1/prescriptions/{self.rx_id}")
        page = await self.client.get(
            f"/api/v1/patients/{self.patient_id}/prescriptions",
            params={"source": "patient", "status": "active"},
        )
        self.assertEqual(detail.json()["prescription"]["status"], "active")
        self.assertEqual(page.json()["total"], 1)
        self.assertEqual(page.json()["items"][0], detail.json())

    async def test_create_correct_review_then_confirm_through_real_routes(self):
        payload = {
            "claimed_prescriber": "Patient-supplied unverified label",
            "items": [
                {
                    "medicine_name_snapshot": "Synthetic uploaded prescription item",
                    "dose_text": "Patient transcribed dose",
                    "frequency": "daily",
                    "is_prn": False,
                }
            ],
        }
        created = await self.client.post(
            f"/api/v1/patients/{self.patient_id}/prescriptions",
            json=payload,
        )
        self.assertEqual(created.status_code, 201, created.text)
        record = created.json()["prescription"]
        path = f"/api/v1/prescriptions/{record['id']}"
        payload["items"][0]["dose_text"] = "Patient corrected dose after review"
        edited = await self.client.put(path + "/draft", json=payload, headers={"If-Match": "1"})
        self.assertEqual(edited.status_code, 200, edited.text)
        confirmed = await self.client.post(path + "/confirm")
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(confirmed.json()["items"], edited.json()["items"])
        self.assertEqual(confirmed.json()["prescription"]["version"], 3)
        self.assertEqual(confirmed.json()["prescription"]["source"], "patient")
        self.assertEqual(confirmed.json()["prescription"]["review_status"], "not_reviewed")
