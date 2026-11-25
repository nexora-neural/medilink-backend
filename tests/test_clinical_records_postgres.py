"""Synthetic ML-68 API integration tests on an isolated migrated PostgreSQL DB."""

import os
from datetime import UTC, date, datetime, timedelta
from unittest import IsolatedAsyncioTestCase, skipUnless
from unittest.mock import patch
from uuid import UUID, uuid4

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.database.session import get_session
from app.main import app
from app.modules.audit.models import AuditEvent
from app.modules.clinical_records import service
from app.modules.clinical_records.models import AllergiesModel, ConditionsModel
from app.modules.consent.models import AccessGrant, GrantCategory
from app.modules.consultations.models import Consultation
from app.modules.identity.auth import get_current_user
from app.modules.identity.models import Role, User, UserRole
from app.modules.identity.request_limits import require_authenticated_rate_limit
from app.modules.organizations.models import Organization, OrganizationMembership
from app.modules.profiles.models import PatientProfile, ProfessionalProfile

TEST_DATABASE_URL = os.getenv("ML68_TEST_DATABASE_URL")


@skipUnless(TEST_DATABASE_URL, "Set ML68_TEST_DATABASE_URL to an isolated migrated test database")
class ClinicalPostgresTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        engine = create_async_engine(TEST_DATABASE_URL)
        self.addAsyncCleanup(engine.dispose)
        connection = await engine.connect()
        self.addAsyncCleanup(connection.close)
        transaction = await connection.begin()
        self.addAsyncCleanup(transaction.rollback)
        self.db = AsyncSession(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        self.addAsyncCleanup(self.db.close)
        self.patient = await self.user("patient")
        self.other = await self.user("patient")
        self.doctor = await self.user("doctor")
        self.other_doctor = await self.user("doctor")
        self.admin = await self.user("super_admin")
        await self.db.commit()
        self.actor = self.patient

        async def session():
            try:
                yield self.db
            finally:
                # Each request normally owns a new session. Our shared fixture
                # session must refresh objects expired by a denied-request rollback.
                for row in list(self.db.identity_map.values()):
                    await self.db.refresh(row)

        async def actor():
            return self.actor

        override = patch.dict(
            app.dependency_overrides,
            {
                get_session: session,
                get_current_user: actor,
                require_authenticated_rate_limit: lambda: None,
            },
        )
        override.start()
        self.addCleanup(override.stop)
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)
        self.path = f"/api/v1/patients/{self.patient.id}/clinical-records"
        self.body = {
            "type": "condition",
            "title": "Synthetic condition",
            "description": "Synthetic details",
            "clinical_code": "SYN-01",
            "recorded_at": "2026-11-23T10:00:00+05:30",
        }

    async def user(self, code):
        role = await self.db.scalar(select(Role).where(Role.code == code))
        if role is None:
            role = Role(code=code, label=code)
            self.db.add(role)
            await self.db.flush()
        user = User(firebase_uid=f"ml68-{uuid4()}", display_name="ML68 synthetic", status="active")
        self.db.add(user)
        await self.db.flush()
        self.db.add(UserRole(user_id=user.id, role_id=role.id))
        if code == "patient":
            self.db.add(
                PatientProfile(user_id=user.id, date_of_birth=date(1990, 1, 1), sex="female")
            )
        if code == "doctor":
            self.db.add(
                ProfessionalProfile(
                    user_id=user.id,
                    role="doctor",
                    doctor_reference=f"ml68-{uuid4()}",
                    verification_status="approved",
                )
            )
        await self.db.flush()
        return user

    async def context(self, *, org=None, category="history_allergies", doctor=None):
        doctor = doctor or self.doctor
        now = datetime.now(UTC)
        grant = AccessGrant(
            patient_user_id=self.patient.id,
            professional_user_id=doctor.id,
            organization_id=org,
            allow_consultation=True,
            status="active",
            starts_at=now - timedelta(hours=1),
            expires_at=now + timedelta(hours=1),
        )
        self.db.add(grant)
        await self.db.flush()
        self.db.add(GrantCategory(grant_id=grant.id, category=category))
        consultation = Consultation(
            patient_user_id=self.patient.id,
            doctor_user_id=doctor.id,
            grant_id=grant.id,
            organization_id=org,
            started_at=now - timedelta(minutes=5),
        )
        self.db.add(consultation)
        await self.db.commit()
        return grant, consultation

    async def post(self, body=None):
        return await self.client.post(self.path, json=body or self.body)

    async def test_patient_create_read_and_provenance_roundtrip(self):
        response = await self.post()
        self.assertEqual(response.status_code, 201, response.text)
        record = response.json()
        self.assertEqual(record["source"], "patient")
        self.assertEqual(record["recorded_by"], str(self.patient.id))
        self.assertIsNone(record["organization_id"])
        self.assertIsNone(record["consultation_id"])
        self.assertEqual(record["status"], "active")
        self.assertEqual(record["clinical_code"], "SYN-01")
        self.assertEqual(
            datetime.fromisoformat(record["recorded_at"]),
            datetime.fromisoformat(self.body["recorded_at"]),
        )
        detail = await self.client.get(f"/api/v1/clinical-records/{record['id']}")
        self.assertEqual(detail.json(), record)
        self.assertEqual(detail.headers["Cache-Control"], "no-store")
        event = await self.db.scalar(
            select(AuditEvent).where(
                AuditEvent.target_id == UUID(record["id"]),
                AuditEvent.action == "clinical_record.created",
            )
        )
        self.assertEqual(event.actor_user_id, self.patient.id)
        self.assertEqual(event.metadata_, {"type": "condition", "source": "patient"})

    async def test_allergy_absence_is_not_no_known_allergies(self):
        empty = (await self.client.get(self.path + "?type=allergy")).json()
        self.assertEqual(empty["total"], 0)
        self.assertEqual(empty["items"], [])
        body = self.body | {
            "type": "allergy",
            "title": "No known allergies",
            "description": "Explicit synthetic patient report",
            "clinical_code": None,
        }
        response = await self.post(body)
        self.assertEqual(response.status_code, 201, response.text)
        listing = (await self.client.get(self.path + "?type=allergy")).json()
        self.assertEqual(listing["items"][0]["title"], "No known allergies")
        self.assertEqual(listing["items"][0]["source"], "patient")
        self.assertEqual(listing["items"][0]["description"], body["description"])

    async def test_patient_cannot_author_diagnosis_note_or_claim_consultation(self):
        _, consultation = await self.context()
        for changes, expected in [
            ({"type": "diagnosis"}, 403),
            ({"type": "note", "clinical_code": None}, 403),
            ({"consultation_id": str(consultation.id)}, 422),
        ]:
            response = await self.post(self.body | changes)
            self.assertEqual(response.status_code, expected, response.text)
        self.assertEqual(await self.db.scalar(select(func.count()).select_from(ConditionsModel)), 0)

    async def test_other_patient_and_admin_cannot_read_or_write(self):
        record = (await self.post()).json()
        for actor in (self.other, self.admin, self.other_doctor):
            self.actor = actor
            for response in (
                await self.client.get(self.path),
                await self.post(),
                await self.client.get(f"/api/v1/clinical-records/{record['id']}"),
            ):
                self.assertEqual(response.status_code, 404, response.text)
                self.assertEqual(response.json()["code"], "RESOURCE_NOT_FOUND")

    async def test_doctor_all_types_roundtrip_without_overwriting_other_authors(self):
        original = (await self.post()).json()
        _, consultation = await self.context()
        self.actor = self.doctor
        for kind in ("condition", "diagnosis", "allergy", "note"):
            body = self.body | {"type": kind, "consultation_id": str(consultation.id)}
            response = await self.post(body)
            self.assertEqual(response.status_code, 201, response.text)
            self.assertEqual(response.json()["source"], "doctor")
            self.assertEqual(response.json()["recorded_by"], str(self.doctor.id))
            self.assertEqual(response.json()["description"], body["description"])
            self.assertEqual(response.json()["clinical_code"], body["clinical_code"])
        detail = await self.client.get(f"/api/v1/clinical-records/{original['id']}")
        self.assertEqual(detail.json(), original)
        for verb in (self.client.put, self.client.patch, self.client.delete):
            self.assertEqual(
                (await verb(f"/api/v1/clinical-records/{original['id']}")).status_code, 405
            )

    async def test_doctor_reads_without_consultation_but_needs_it_for_writes(self):
        _, consultation = await self.context()
        consultation.ended_at = datetime.now(UTC)
        await self.db.commit()
        self.actor = self.doctor
        self.assertEqual((await self.client.get(self.path)).status_code, 200)
        self.assertEqual((await self.post()).status_code, 422)
        response = await self.post(self.body | {"consultation_id": str(consultation.id)})
        self.assertEqual(response.status_code, 404)

    async def test_active_grant_includes_patient_records_created_after_grant(self):
        await self.context(category="history_allergies")
        created = await self.post(
            self.body | {"title": "Patient record created after grant"}
        )
        self.assertEqual(created.status_code, 201, created.text)

        self.actor = self.doctor
        listing = await self.client.get(self.path)

        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual(listing.json()["total"], 1)
        self.assertEqual(listing.json()["items"][0]["id"], created.json()["id"])
        self.assertEqual(
            listing.json()["items"][0]["title"], "Patient record created after grant"
        )

    async def test_wrong_category_is_not_access(self):
        _, consultation = await self.context(category="prescriptions")
        self.actor = self.doctor
        self.assertEqual((await self.client.get(self.path)).status_code, 404)
        self.assertEqual(
            (
                await self.post(
                    self.body
                    | {
                        "consultation_id": str(consultation.id),
                    }
                )
            ).status_code,
            404,
        )

    async def test_expired_future_and_revoked_grants_are_denied(self):
        grant, consultation = await self.context()
        grant_id, consultation_id = grant.id, consultation.id
        self.actor = self.doctor
        for state in ("expired", "future", "revoked"):
            grant = await self.db.get(AccessGrant, grant_id)
            now = datetime.now(UTC)
            grant.starts_at = now - timedelta(hours=2)
            grant.expires_at = now + timedelta(hours=2)
            grant.status, grant.revoked_at = "active", None
            if state == "expired":
                grant.expires_at = now - timedelta(hours=1)
            elif state == "future":
                grant.starts_at = now + timedelta(hours=1)
            else:
                grant.status, grant.revoked_at = "revoked", now
            await self.db.commit()
            self.assertEqual((await self.client.get(self.path)).status_code, 404)
            self.assertEqual(
                (
                    await self.post(
                        self.body
                        | {
                            "consultation_id": str(consultation_id),
                        }
                    )
                ).status_code,
                404,
            )

    async def test_consultation_binding_and_permission_are_required(self):
        grant, consultation = await self.context()
        self.actor = self.doctor
        body = self.body | {"consultation_id": str(consultation.id)}
        grant.allow_consultation = False
        await self.db.commit()
        self.assertEqual((await self.post(body)).status_code, 404)
        grant = await self.db.get(AccessGrant, grant.id)
        grant.allow_consultation = True
        consultation = await self.db.get(Consultation, consultation.id)
        consultation.patient_user_id = self.other.id
        await self.db.commit()
        self.assertEqual((await self.post(body)).status_code, 404)

    async def test_suspended_actor_revoked_role_and_unverified_doctor_denied(self):
        await self.context()
        patient_id = self.patient.id
        self.patient.status = "suspended"
        await self.db.commit()
        self.assertEqual((await self.client.get(self.path)).status_code, 403)
        self.actor = self.doctor
        profile = await self.db.get(ProfessionalProfile, self.doctor.id)
        profile.verification_status = "not_submitted"
        await self.db.commit()
        self.assertEqual((await self.client.get(self.path)).status_code, 403)
        self.actor = await self.db.get(User, patient_id)
        self.actor.status = "active"
        role = await self.db.scalar(select(UserRole).where(UserRole.user_id == patient_id))
        role.revoked_at = datetime.now(UTC)
        await self.db.commit()
        self.assertEqual((await self.client.get(self.path)).status_code, 404)

    async def test_organization_attribution_and_membership_rechecked(self):
        org = Organization(name="ML68 synthetic clinic", type="clinic", status="active")
        self.db.add(org)
        await self.db.flush()
        membership = OrganizationMembership(
            user_id=self.doctor.id, organization_id=org.id, role="doctor", status="approved"
        )
        self.db.add(membership)
        await self.db.commit()
        _, consultation = await self.context(org=org.id)
        self.actor = self.doctor
        response = await self.post(self.body | {"consultation_id": str(consultation.id)})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["organization_id"], str(org.id))
        membership.status = "suspended"
        await self.db.commit()
        self.assertEqual((await self.client.get(self.path)).status_code, 404)

    async def test_list_pagination_type_window_and_history_preserved(self):
        records = []
        for title, timestamp in [
            ("A", "2026-11-01T00:00:00Z"),
            ("B", "2026-11-02T00:00:00Z"),
            ("C", "2026-11-03T00:00:00Z"),
        ]:
            records.append(
                (
                    await self.post(
                        self.body
                        | {
                            "title": title,
                            "recorded_at": timestamp,
                        }
                    )
                ).json()
            )
        old = await self.db.get(ConditionsModel, UUID(records[0]["id"]))
        old.status = "superseded"
        await self.db.commit()
        page1 = (await self.client.get(self.path + "?page_size=2")).json()
        page2 = (await self.client.get(self.path + "?page_size=2&page=2")).json()
        self.assertEqual(page1["total"], 3)
        self.assertEqual(len(page1["items"]), 2)
        self.assertEqual(len(page2["items"]), 1)
        expected = sorted(records, key=lambda row: (row["created_at"], row["id"]), reverse=True)
        self.assertEqual(
            [row["id"] for row in page1["items"] + page2["items"]], [row["id"] for row in expected]
        )
        window = (
            await self.client.get(
                self.path + "?type=condition&from=2026-11-02T00:00:00Z&to=2026-11-03T00:00:00Z"
            )
        ).json()
        self.assertEqual(window["total"], 1)
        self.assertEqual(window["items"][0]["title"], "B")
        detail = await self.client.get(f"/api/v1/clinical-records/{old.id}")
        self.assertEqual(detail.json()["status"], "superseded")

    async def test_audit_failure_rolls_back_insert(self):
        with patch.object(service, "append_event", side_effect=RuntimeError("synthetic-secret")):
            response = await self.post()
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("synthetic-secret", response.text)
        self.assertEqual(await self.db.scalar(select(func.count()).select_from(ConditionsModel)), 0)
        self.assertEqual(await self.db.scalar(select(func.count()).select_from(AllergiesModel)), 0)
