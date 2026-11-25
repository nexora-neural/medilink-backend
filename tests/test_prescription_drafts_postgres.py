"""Real PostgreSQL checks for prescription draft persistence."""

import os
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, skipUnless
from uuid import uuid4

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.database import models as _models  # noqa: F401
from app.database.session import get_session
from app.modules.audit.models import AuditEvent
from app.modules.consent.models import AccessGrant, GrantCategory
from app.modules.consultations.models import Consultation
from app.modules.documents.models import Document
from app.modules.identity.auth import AuthenticationError, get_current_user
from app.modules.identity.models import Role, User, UserRole
from app.modules.organizations.models import Organization, OrganizationMembership
from app.modules.prescriptions import repository, service
from app.modules.prescriptions.access import require_prescription_access
from app.modules.prescriptions.core_models import Prescription, PrescriptionItem
from app.modules.prescriptions.router import router
from app.modules.prescriptions.schemas import PrescriptionInput
from app.modules.profiles.models import PatientProfile, ProfessionalProfile

TEST_DATABASE_URL = os.getenv("PRESCRIPTION_STORAGE_TEST_DATABASE_URL")


@skipUnless(
    TEST_DATABASE_URL,
    "Set PRESCRIPTION_STORAGE_TEST_DATABASE_URL to a migrated test database",
)
class PrescriptionDraftPostgresTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        engine = create_async_engine(TEST_DATABASE_URL)
        self.addAsyncCleanup(engine.dispose)

        connection = await engine.connect()
        self.addAsyncCleanup(connection.close)

        transaction = await connection.begin()
        self.addAsyncCleanup(transaction.rollback)

        self.db = AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        self.addAsyncCleanup(self.db.close)

        self.patient = User(
            firebase_uid=str(uuid4()),
            display_name="Synthetic draft patient",
            status="active",
        )
        self.db.add(self.patient)
        await self.db.flush()

        self.db.add(
            PatientProfile(
                user_id=self.patient.id,
                date_of_birth=date(1990, 1, 1),
                sex="female",
            )
        )
        await self.db.flush()

        self.patient_id = self.patient.id

        self.doctor = User(
            firebase_uid=str(uuid4()),
            display_name="Synthetic authorization doctor",
            status="active",
        )
        self.db.add(self.doctor)
        await self.db.flush()

        for actor, code in (
            (self.patient, "patient"),
            (self.doctor, "doctor"),
        ):
            role = await self.db.scalar(select(Role).where(Role.code == code))
            if role is None:
                role = Role(code=code, label=f"Test {code}")
                self.db.add(role)
                await self.db.flush()

            self.db.add(UserRole(user_id=actor.id, role_id=role.id))

        self.db.add(
            ProfessionalProfile(
                user_id=self.doctor.id,
                role="doctor",
                doctor_reference=f"TEST-{uuid4()}",
                verification_status="approved",
            )
        )
        await self.db.flush()

        now = datetime.now(UTC)
        self.grant = AccessGrant(
            patient_user_id=self.patient_id,
            professional_user_id=self.doctor.id,
            organization_id=None,
            allow_consultation=True,
            status="active",
            starts_at=now - timedelta(hours=1),
            expires_at=now + timedelta(hours=1),
        )
        self.db.add(self.grant)
        await self.db.flush()

        self.db.add(
            GrantCategory(
                grant_id=self.grant.id,
                category="prescriptions",
            )
        )
        self.consultation = Consultation(
            patient_user_id=self.patient_id,
            doctor_user_id=self.doctor.id,
            organization_id=None,
            grant_id=self.grant.id,
            started_at=now - timedelta(minutes=10),
        )
        self.db.add(self.consultation)
        await self.db.flush()

        self.request = SimpleNamespace(state=SimpleNamespace(request_id=str(uuid4())))

        # Release the setup savepoint; outer transaction still owns all data.
        await self.db.commit()

    def payload(self):
        return PrescriptionInput(
            items=[
                {
                    "medicine_name_snapshot": "Synthetic test medicine",
                    "dose_text": "Synthetic dose",
                    "route": "Oral",
                    "frequency": "Synthetic frequency",
                    "duration_days": 3,
                    "quantity": "3",
                    "quantity_unit": "tablet",
                    "instructions": "Interface testing only.",
                    "is_prn": False,
                }
            ]
        )

    async def create_draft(self):
        return await repository.create_prescription_draft(
            self.db,
            patient_id=self.patient_id,
            actor_id=self.patient_id,
            source="patient",
            payload=self.payload(),
        )

    async def test_create_persists_draft_and_items(self):
        draft = await self.create_draft()
        draft_id = draft.id
        self.db.expire_all()

        stored = await repository.find_prescription(self.db, draft_id)
        items = list(
            (
                await self.db.scalars(
                    select(PrescriptionItem).where(PrescriptionItem.prescription_id == draft_id)
                )
            ).all()
        )

        self.assertIsNotNone(stored)
        self.assertEqual(stored.source, "patient")
        self.assertEqual(stored.status, "draft")
        self.assertIsNone(stored.prescriber_user_id)
        self.assertIsNone(stored.issued_at)
        self.assertEqual(stored.version, 1)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].medicine_name_snapshot, "Synthetic test medicine")
        self.assertEqual(items[0].quantity_unit, "tablet")

    async def test_list_filters_and_paginates(self):
        await self.create_draft()
        await self.create_draft()

        rows, total = await repository.list_patient_prescriptions(
            self.db,
            patient_id=self.patient_id,
            page=1,
            page_size=1,
            status="draft",
            source="patient",
        )
        next_rows, next_total = await repository.list_patient_prescriptions(
            self.db,
            patient_id=self.patient_id,
            page=2,
            page_size=1,
            status="draft",
            source="patient",
        )
        other_rows, other_total = await repository.list_patient_prescriptions(
            self.db,
            patient_id=uuid4(),
            page=1,
            page_size=20,
        )

        self.assertEqual(total, 2)
        self.assertEqual(next_total, 2)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(next_rows), 1)
        self.assertNotEqual(rows[0].id, next_rows[0].id)
        self.assertEqual(other_rows, [])
        self.assertEqual(other_total, 0)

    async def test_delete_unreferenced_draft_removes_parent_and_items(self):
        draft = await self.create_draft()
        draft_id = draft.id

        await repository.delete_draft_rows(self.db, draft_id)

        stored = await repository.find_prescription(self.db, draft_id)
        item_id = await self.db.scalar(
            select(PrescriptionItem.id).where(PrescriptionItem.prescription_id == draft_id)
        )

        self.assertIsNone(stored)
        self.assertIsNone(item_id)

    async def test_linked_version_blocks_delete_and_restores_items(self):
        original = await self.create_draft()
        original_id = original.id

        replacement = Prescription(
            patient_user_id=self.patient_id,
            source="patient",
            created_by=self.patient_id,
            previous_prescription_id=original_id,
        )
        self.db.add(replacement)
        await self.db.flush()
        replacement_id = replacement.id

        # The exception must leave the nested transaction before recovery.
        with self.assertRaises(IntegrityError):
            async with self.db.begin_nested():
                await repository.delete_draft_rows(
                    self.db,
                    original_id,
                )

        original_still_exists = await repository.find_prescription(self.db, original_id)
        replacement_still_exists = await repository.find_prescription(self.db, replacement_id)
        retained_item = await self.db.scalar(
            select(PrescriptionItem.id).where(PrescriptionItem.prescription_id == original_id)
        )

        self.assertIsNotNone(original_still_exists)
        self.assertIsNotNone(replacement_still_exists)
        self.assertIsNotNone(retained_item)

    async def doctor_access(self, *, write=False, consultation_id=None):
        return await require_prescription_access(
            self.db,
            user=self.doctor,
            request=self.request,
            patient_id=self.patient_id,
            consultation_id=consultation_id,
            clinical_write=write,
        )

    async def test_patient_can_access_own_record(self):
        source = await require_prescription_access(
            self.db,
            user=self.patient,
            request=self.request,
            patient_id=self.patient_id,
            clinical_write=True,
        )
        self.assertEqual(source, "patient")

    async def test_doctor_read_does_not_require_consultation(self):
        source = await self.doctor_access()
        self.assertEqual(source, "doctor")

    async def test_doctor_write_requires_matching_consultation(self):
        source = await self.doctor_access(
            write=True,
            consultation_id=self.consultation.id,
        )
        self.assertEqual(source, "doctor")

        with self.assertRaises(AuthenticationError) as caught:
            await self.doctor_access(
                write=True,
                consultation_id=uuid4(),
            )
        self.assertEqual(caught.exception.status_code, 403)

    async def test_expired_grant_denies_access(self):
        # Keep status active to verify the actual time-window check.
        self.grant.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await self.db.flush()

        with self.assertRaises(AuthenticationError) as caught:
            await self.doctor_access()
        self.assertEqual(caught.exception.status_code, 403)

    async def test_revoked_grant_denies_access(self):
        self.grant.status = "revoked"
        self.grant.revoked_at = datetime.now(UTC)
        await self.db.flush()

        with self.assertRaises(AuthenticationError) as caught:
            await self.doctor_access()
        self.assertEqual(caught.exception.status_code, 403)

    async def test_closed_consultation_denies_write(self):
        self.consultation.ended_at = datetime.now(UTC)
        await self.db.flush()

        with self.assertRaises(AuthenticationError) as caught:
            await self.doctor_access(
                write=True,
                consultation_id=self.consultation.id,
            )
        self.assertEqual(caught.exception.status_code, 403)

    async def test_wrong_category_denies_access(self):
        from sqlalchemy import delete

        await self.db.execute(delete(GrantCategory).where(GrantCategory.grant_id == self.grant.id))
        self.db.add(
            GrantCategory(
                grant_id=self.grant.id,
                category="history_allergies",
            )
        )
        await self.db.flush()

        with self.assertRaises(AuthenticationError) as caught:
            await self.doctor_access()
        self.assertEqual(caught.exception.status_code, 403)

    async def test_doctor_create_persists_source_items_and_audit(self):
        body = self.payload().model_copy(update={"consultation_id": self.consultation.id})

        result = await service.create_draft(
            session=self.db,
            user=self.doctor,
            request=self.request,
            patient_id=self.patient_id,
            payload=body,
        )
        draft_id = result.prescription.id

        self.db.expire_all()

        stored = await repository.find_prescription(self.db, draft_id)
        audit = await self.db.scalar(
            select(AuditEvent).where(
                AuditEvent.target_id == draft_id,
                AuditEvent.action == "prescription.draft.created",
            )
        )
        item_id = await self.db.scalar(
            select(PrescriptionItem.id).where(PrescriptionItem.prescription_id == draft_id)
        )

        self.assertIsNotNone(stored)
        self.assertEqual(stored.source, "doctor")
        self.assertIsNotNone(stored.prescriber_user_id)
        self.assertEqual(stored.status, "draft")
        self.assertIsNone(stored.issued_at)
        self.assertIsNotNone(item_id)
        self.assertIsNotNone(audit)
        self.assertEqual(audit.actor_user_id, stored.prescriber_user_id)
        self.assertEqual(audit.metadata_["item_count"], 1)
        self.assertEqual(
            audit.request_id,
            self.request.state.request_id,
        )

    async def organization_context(self, *, membership_status="approved"):
        # Use a valid organization type from the existing model enum.
        organization_type = Organization.__table__.c.type.type.enums[0]

        organization = Organization(
            name="Synthetic prescription test organization",
            type=organization_type,
            status="active",
        )
        self.db.add(organization)
        await self.db.flush()

        membership = OrganizationMembership(
            organization_id=organization.id,
            user_id=self.doctor.id,
            role="doctor",
            status=membership_status,
        )
        self.db.add(membership)

        self.grant.organization_id = organization.id
        self.consultation.organization_id = organization.id
        await self.db.flush()

        return organization, membership

    async def test_approved_organization_member_can_write(self):
        await self.organization_context()

        source = await self.doctor_access(
            write=True,
            consultation_id=self.consultation.id,
        )

        self.assertEqual(source, "doctor")

    async def test_pending_organization_member_cannot_read(self):
        await self.organization_context(membership_status="pending")

        with self.assertRaises(AuthenticationError) as caught:
            await self.doctor_access()

        self.assertEqual(caught.exception.status_code, 403)

    async def test_inactive_organization_cannot_read(self):
        organization, _ = await self.organization_context()
        organization.status = "pending"
        await self.db.flush()

        with self.assertRaises(AuthenticationError) as caught:
            await self.doctor_access()

        self.assertEqual(caught.exception.status_code, 403)

    async def test_consultation_organization_must_match_grant(self):
        await self.organization_context()

        # Grant belongs to the organization; consultation does not.
        self.consultation.organization_id = None
        await self.db.flush()

        with self.assertRaises(AuthenticationError) as caught:
            await self.doctor_access(
                write=True,
                consultation_id=self.consultation.id,
            )

        self.assertEqual(caught.exception.status_code, 403)

    async def test_http_patient_draft_crud(self):
        app = FastAPI()
        app.include_router(router, prefix="/api/v1")

        async def test_session():
            yield self.db

        async def test_user():
            return self.patient

        app.dependency_overrides[get_session] = test_session
        app.dependency_overrides[get_current_user] = test_user

        @app.middleware("http")
        async def request_id_middleware(request, call_next):
            request.state.request_id = str(uuid4())
            return await call_next(request)

        transport = ASGITransport(app=app)

        async with AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            collection_url = f"/api/v1/patients/{self.patient_id}/prescriptions"
            body = self.payload().model_dump(mode="json")

            # 1. Create a draft.
            response = await client.post(collection_url, json=body)
            self.assertEqual(response.status_code, 201, response.text)
            created = response.json()
            draft_id = created["prescription"]["id"]
            version = created["prescription"]["version"]

            self.assertEqual(created["prescription"]["source"], "patient")
            self.assertEqual(created["prescription"]["status"], "draft")
            self.assertEqual(len(created["items"]), 1)

            detail_url = f"/api/v1/prescriptions/{draft_id}"

            # 2. Read the draft.
            response = await client.get(detail_url)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["prescription"]["id"], draft_id)

            # 3. Find it in the filtered list.
            response = await client.get(
                collection_url,
                params={"status": "draft", "source": "patient"},
            )
            self.assertEqual(response.status_code, 200, response.text)
            page = response.json()
            self.assertEqual(page["total"], 1)
            self.assertEqual(page["items"][0]["prescription"]["id"], draft_id)

            # 4. Replace the draft using its current version.
            body["items"][0]["instructions"] = "Updated synthetic instructions."
            response = await client.put(
                f"{detail_url}/draft",
                json=body,
                headers={"If-Match": str(version)},
            )
            self.assertEqual(response.status_code, 200, response.text)
            updated = response.json()
            self.assertEqual(updated["prescription"]["version"], version + 1)
            self.assertEqual(
                updated["items"][0]["instructions"],
                "Updated synthetic instructions.",
            )

            # 5. Delete the unreferenced draft.
            response = await client.delete(f"{detail_url}/draft")
            self.assertEqual(response.status_code, 204, response.text)
            self.assertEqual(response.content, b"")

            # 6. Confirm it disappeared from the list.
            response = await client.get(collection_url)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["total"], 0)

    async def test_delete_draft_retains_original_document(self):
        document = Document(
            owner_user_id=self.patient_id,
            patient_user_id=self.patient_id,
            uploaded_by=self.patient_id,
            source="patient",
            document_type="prescription",
            original_filename="synthetic.pdf",
            storage_key=str(uuid4()),
            media_type="application/pdf",
            byte_size=42,
        )
        self.db.add(document)
        await self.db.flush()
        document_id = document.id

        payload = self.payload().model_copy(update={"original_document_id": document_id})
        draft = await repository.create_prescription_draft(
            self.db,
            patient_id=self.patient_id,
            actor_id=self.patient_id,
            source="patient",
            payload=payload,
        )
        draft_id = draft.id

        await repository.delete_draft_rows(self.db, draft_id)

        self.db.expire_all()

        self.assertIsNone(await repository.find_prescription(self.db, draft_id))
        self.assertIsNotNone(await self.db.get(Document, document_id))

    async def test_http_rejects_unsupported_quantity_precision(self):
        app = FastAPI()
        app.include_router(router, prefix="/api/v1")

        async def test_session():
            yield self.db

        async def test_user():
            return self.patient

        app.dependency_overrides[get_session] = test_session
        app.dependency_overrides[get_current_user] = test_user

        @app.middleware("http")
        async def request_id_middleware(request, call_next):
            request.state.request_id = str(uuid4())
            return await call_next(request)

        transport = ASGITransport(app=app)

        async with AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            collection_url = f"/api/v1/patients/{self.patient_id}/prescriptions"

            # A supported quantity must save without rounding.
            valid_body = self.payload().model_dump(mode="json")
            valid_body["items"][0]["quantity"] = "1.234"
            valid_body["items"][0]["quantity_unit"] = "mL"

            response = await client.post(
                collection_url,
                json=valid_body,
            )
            self.assertEqual(response.status_code, 201, response.text)

            created = response.json()
            draft_id = created["prescription"]["id"]
            version = created["prescription"]["version"]
            detail_url = f"/api/v1/prescriptions/{draft_id}"

            for quantity in ("1.2345", "0.0001", "1000000000"):
                with self.subTest(quantity=quantity):
                    invalid_body = self.payload().model_dump(mode="json")
                    invalid_body["items"][0]["quantity"] = quantity
                    invalid_body["items"][0]["quantity_unit"] = "mL"

                    # Invalid create must return 422.
                    response = await client.post(
                        collection_url,
                        json=invalid_body,
                    )
                    self.assertEqual(response.status_code, 422, response.text)

                    # Invalid edit must also return 422.
                    response = await client.put(
                        f"{detail_url}/draft",
                        json=invalid_body,
                        headers={"If-Match": str(version)},
                    )
                    self.assertEqual(response.status_code, 422, response.text)

            # Invalid edits must leave the original draft unchanged.
            response = await client.get(detail_url)
            self.assertEqual(response.status_code, 200, response.text)

            stored = response.json()
            self.assertEqual(stored["prescription"]["version"], version)
            self.assertEqual(str(stored["items"][0]["quantity"]), "1.234")

            # Invalid creates must not add any drafts.
            response = await client.get(collection_url)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["total"], 1)

    async def test_issue_doctor_draft_persists_and_retry_is_idempotent(self):
        body = self.payload().model_copy(
            update={
                "consultation_id": self.consultation.id,
                "starts_on": date(2026, 11, 25),
                "ends_on": date(2026, 11, 27),
            }
        )

        created = await service.create_draft(
            session=self.db,
            user=self.doctor,
            request=self.request,
            patient_id=self.patient_id,
            payload=body,
        )
        draft_id = created.prescription.id
        draft_version = created.prescription.version

        issued = await service.issue_prescription(
            session=self.db,
            user=self.doctor,
            request=self.request,
            prescription_id=draft_id,
        )

        self.assertEqual(issued.prescription.status, "active")
        self.assertIsNotNone(issued.prescription.issued_at)
        self.assertEqual(issued.prescription.version, draft_version + 1)
        issued_at = issued.prescription.issued_at

        retried = await service.issue_prescription(
            session=self.db,
            user=self.doctor,
            request=self.request,
            prescription_id=draft_id,
        )

        self.assertEqual(retried.prescription.issued_at, issued_at)
        self.assertEqual(retried.prescription.version, draft_version + 1)

        self.db.expire_all()

        stored = await repository.find_prescription(self.db, draft_id)
        events = list(
            (
                await self.db.scalars(
                    select(AuditEvent).where(
                        AuditEvent.target_id == draft_id,
                        AuditEvent.action == "prescription.issued",
                    )
                )
            ).all()
        )

        self.assertEqual(stored.status, "active")
        self.assertEqual(stored.issued_at, issued_at)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].metadata_["new_version"], draft_version + 1)

    async def test_issue_incomplete_draft_keeps_saved_draft_unchanged(self):
        # Missing start/end dates: valid draft, invalid issued order.
        body = self.payload().model_copy(update={"consultation_id": self.consultation.id})

        created = await service.create_draft(
            session=self.db,
            user=self.doctor,
            request=self.request,
            patient_id=self.patient_id,
            payload=body,
        )
        draft_id = created.prescription.id
        draft_version = created.prescription.version

        with self.assertRaises(AuthenticationError) as caught:
            await service.issue_prescription(
                session=self.db,
                user=self.doctor,
                request=self.request,
                prescription_id=draft_id,
            )

        self.assertEqual(caught.exception.status_code, 422)

        self.db.expire_all()

        stored = await repository.find_prescription(self.db, draft_id)
        issue_event = await self.db.scalar(
            select(AuditEvent.id).where(
                AuditEvent.target_id == draft_id,
                AuditEvent.action == "prescription.issued",
            )
        )

        self.assertEqual(stored.status, "draft")
        self.assertIsNone(stored.issued_at)
        self.assertEqual(stored.version, draft_version)
        self.assertIsNone(issue_event)

    async def test_http_doctor_issue_and_retry(self):
        body = self.payload().model_copy(
            update={
                "consultation_id": self.consultation.id,
                "starts_on": date(2026, 11, 25),
                "ends_on": date(2026, 11, 27),
            }
        )
        created = await service.create_draft(
            session=self.db,
            user=self.doctor,
            request=self.request,
            patient_id=self.patient_id,
            payload=body,
        )
        draft_id = created.prescription.id
        original_version = created.prescription.version

        app = FastAPI()
        app.include_router(router, prefix="/api/v1")

        async def test_session():
            yield self.db

        async def test_user():
            return self.doctor

        app.dependency_overrides[get_session] = test_session
        app.dependency_overrides[get_current_user] = test_user

        @app.middleware("http")
        async def request_id_middleware(request, call_next):
            request.state.request_id = str(uuid4())
            return await call_next(request)

        transport = ASGITransport(app=app)

        async with AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            issue_url = f"/api/v1/prescriptions/{draft_id}/issue"

            # Explicit POST issues the saved draft; no request body needed.
            response = await client.post(issue_url)
            self.assertEqual(response.status_code, 200, response.text)

            issued = response.json()["prescription"]
            self.assertEqual(issued["status"], "active")
            self.assertIsNotNone(issued["issued_at"])
            self.assertEqual(issued["version"], original_version + 1)

            # Retrying must return the same issued state.
            response = await client.post(issue_url)
            self.assertEqual(response.status_code, 200, response.text)

            retried = response.json()["prescription"]
            self.assertEqual(retried["issued_at"], issued["issued_at"])
            self.assertEqual(retried["version"], issued["version"])

        events = list(
            (
                await self.db.scalars(
                    select(AuditEvent).where(
                        AuditEvent.target_id == draft_id,
                        AuditEvent.action == "prescription.issued",
                    )
                )
            ).all()
        )
        self.assertEqual(len(events), 1)
