"""Disposable PostgreSQL workflow tests for limited FHIR exports."""

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.session import get_session
from app.main import app
from app.modules.audit.models import AuditEvent
from app.modules.consent.models import AccessGrant, GrantCategory
from app.modules.identity.auth import get_current_user
from app.modules.identity.models import Role, User, UserRole
from app.modules.identity.request_limits import require_authenticated_rate_limit
from app.modules.prescriptions.core_models import Prescription, PrescriptionItem
from app.modules.profiles.models import PatientProfile, ProfessionalProfile

TEST_DATABASE_URL = os.getenv("ML151_TEST_DATABASE_URL")
EXPECTED_TEST_DATABASE = "medilink_ml151_test"


def require_dedicated_database(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"postgresql+asyncpg", "postgresql"}:
        raise RuntimeError("ML151_TEST_DATABASE_URL must use PostgreSQL.")
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise RuntimeError("ML151_TEST_DATABASE_URL must point to local PostgreSQL.")
    if parsed.path.removeprefix("/") != EXPECTED_TEST_DATABASE:
        raise RuntimeError("Refusing to run ML-151 tests outside the dedicated test database.")


class FhirExportsPostgresTests(IsolatedAsyncioTestCase):
    """Exercise authorization and real FHIR responses against migrated PostgreSQL."""

    def setUp(self):
        if not TEST_DATABASE_URL:
            self.skipTest("Set ML151_TEST_DATABASE_URL to the dedicated migrated test database.")
        require_dedicated_database(TEST_DATABASE_URL)

    async def asyncSetUp(self):
        self.engine = create_async_engine(TEST_DATABASE_URL, hide_parameters=True)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.users: list[User] = []
        self.prescription_ids = []
        self.grant_id = None
        self.patient_id = None
        self.patient_two_id = None

        async with self.session_factory() as session:
            current_database = await session.scalar(text("SELECT current_database()"))
            self.assertEqual(current_database, EXPECTED_TEST_DATABASE)
            revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
            self.assertEqual(revision, "ml170_worker_fairness")

        self.patient_actor = None
        self.doctor_actor = None
        self.unverified_doctor_actor = None
        self.no_grant_doctor_actor = None
        self.actor = None

        async def current_user(request: Request):
            request.state.audit_actor_user_id = self.actor.id
            return self.actor

        async def open_session():
            async with self.session_factory() as session:
                yield session

        self.overrides = patch.dict(
            app.dependency_overrides,
            {
                get_current_user: current_user,
                get_session: open_session,
                require_authenticated_rate_limit: lambda: None,
            },
        )
        self.overrides.start()
        self.old_audit_factory = getattr(app.state, "audit_session_factory", None)
        app.state.audit_session_factory = self.session_factory
        self.client = AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        )
        await self._create_synthetic_records()

    async def asyncTearDown(self):
        await self.client.aclose()
        self.overrides.stop()
        if self.old_audit_factory is None:
            delattr(app.state, "audit_session_factory")
        else:
            app.state.audit_session_factory = self.old_audit_factory
        await self.engine.dispose()

    async def _create_user(self, session, *, name: str, role: str, verified: bool = False) -> User:
        user = User(
            firebase_uid=f"ml151-synthetic-{uuid4()}",
            email=f"ml151-{uuid4()}@example.invalid",
            display_name=name,
            phone="0711111111",
            status="active",
        )
        session.add(user)
        await session.flush()
        self.users.append(user)

        role_row = await session.scalar(select(Role).where(Role.code == role))
        if role_row is None:
            labels = {
                "patient": "Patient",
                "doctor": "Doctor",
            }
            role_row = Role(code=role, label=labels[role])
            session.add(role_row)
            await session.flush()
        session.add(UserRole(user_id=user.id, role_id=role_row.id))

        if role == "patient":
            session.add(
                PatientProfile(
                    user_id=user.id,
                    date_of_birth=datetime(1990, 1, 2, tzinfo=UTC).date(),
                    sex="female",
                )
            )
        elif role == "doctor":
            session.add(
                ProfessionalProfile(
                    user_id=user.id,
                    role="doctor",
                    doctor_reference=f"ml151-doctor-{uuid4()}",
                    verification_status="approved" if verified else "pending",
                )
            )
        return user

    async def _create_prescription(
        self,
        session,
        *,
        patient: User,
        doctor: User,
        status: str,
        source: str = "doctor",
        item_count: int = 1,
        reviewed_patient_source: bool = False,
    ) -> list[PrescriptionItem]:
        now = datetime.now(UTC)
        prescription = Prescription(
            patient_user_id=patient.id,
            prescriber_user_id=doctor.id if source == "doctor" else None,
            source=source,
            review_status="approved" if reviewed_patient_source else "not_applicable",
            reviewed_by=doctor.id if reviewed_patient_source else None,
            reviewed_at=now if reviewed_patient_source else None,
            status=status,
            issued_at=None if status == "draft" else now,
            created_by=doctor.id if source == "doctor" else patient.id,
        )
        session.add(prescription)
        await session.flush()
        self.prescription_ids.append(prescription.id)
        items = [
            PrescriptionItem(
                prescription_id=prescription.id,
                medicine_name_snapshot=f"Synthetic medicine {index + 1}",
                dose_text="1 tablet",
                route="oral",
                frequency="twice daily",
                duration_days=5,
                instructions="after meals",
                is_prn=(index == 0),
            )
            for index in range(item_count)
        ]
        session.add_all(items)
        await session.flush()
        return items

    async def _create_synthetic_records(self):
        now = datetime.now(UTC)
        async with self.session_factory.begin() as session:
            patient = await self._create_user(
                session, name="ML-151 Synthetic Patient", role="patient"
            )
            patient_two = await self._create_user(
                session, name="ML-151 Synthetic Patient Two", role="patient"
            )
            doctor = await self._create_user(
                session, name="ML-151 Synthetic Verified Doctor", role="doctor", verified=True
            )
            unverified_doctor = await self._create_user(
                session,
                name="ML-151 Synthetic Unverified Doctor",
                role="doctor",
                verified=False,
            )
            no_grant_doctor = await self._create_user(
                session, name="ML-151 Synthetic Doctor Without Grant", role="doctor", verified=True
            )
            self.patient_id = patient.id
            self.patient_two_id = patient_two.id
            self.patient_actor = SimpleNamespace(id=patient.id, status="active")
            self.patient_two_actor = SimpleNamespace(id=patient_two.id, status="active")
            self.doctor_actor = SimpleNamespace(id=doctor.id, status="active")
            self.unverified_doctor_actor = SimpleNamespace(id=unverified_doctor.id, status="active")
            self.no_grant_doctor_actor = SimpleNamespace(id=no_grant_doctor.id, status="active")

            grant = AccessGrant(
                patient_user_id=patient.id,
                professional_user_id=doctor.id,
                organization_id=None,
                allow_consultation=False,
                status="active",
                starts_at=now - timedelta(days=1),
                expires_at=now + timedelta(days=1),
            )
            session.add(grant)
            await session.flush()
            self.grant_id = grant.id
            session.add_all(
                [
                    GrantCategory(grant_id=grant.id, category="history_allergies"),
                    GrantCategory(grant_id=grant.id, category="prescriptions"),
                ]
            )

            status_items = {}
            for prescription_status, item_count in (
                ("active", 2),
                ("completed", 1),
                ("discontinued", 1),
                ("cancelled", 1),
                ("superseded", 1),
            ):
                status_items[prescription_status] = await self._create_prescription(
                    session,
                    patient=patient,
                    doctor=doctor,
                    status=prescription_status,
                    item_count=item_count,
                )
            self.items_by_status = status_items
            self.patient_source_item = (
                await self._create_prescription(
                    session,
                    patient=patient,
                    doctor=doctor,
                    status="active",
                    source="patient",
                    reviewed_patient_source=True,
                )
            )[0]
            self.draft_item = (
                await self._create_prescription(
                    session,
                    patient=patient,
                    doctor=doctor,
                    status="draft",
                )
            )[0]
            self.cross_patient_item = (
                await self._create_prescription(
                    session,
                    patient=patient_two,
                    doctor=doctor,
                    status="active",
                )
            )[0]

    async def _get(self, actor, path):
        self.actor = actor
        return await self.client.get(path)

    async def test_postgres_patient_and_multiple_medication_exports_and_audit(self):
        patient_response = await self._get(
            self.patient_actor, f"/fhir/r4/Patient/{self.patient_id}"
        )
        self.assertEqual(patient_response.status_code, 200, patient_response.text)
        self.assertTrue(
            patient_response.headers["content-type"].startswith("application/fhir+json")
        )
        self.assertEqual(patient_response.json()["id"], str(self.patient_id))
        self.assertEqual(
            patient_response.json()["identifier"][0],
            {"system": "urn:medilink:patient", "value": str(self.patient_id)},
        )
        self.assertEqual(patient_response.json()["name"][0]["text"], "ML-151 Synthetic Patient")
        self.assertEqual(patient_response.json()["birthDate"], "1990-01-02")
        for sensitive_field in ("email", "phone", "sex", "emergency_contact_phone"):
            self.assertNotIn(sensitive_field, patient_response.json())

        doctor_patient = await self._get(self.doctor_actor, f"/fhir/r4/Patient/{self.patient_id}")
        self.assertEqual(doctor_patient.status_code, 200, doctor_patient.text)

        exported_ids = set()
        for status_name, items in self.items_by_status.items():
            for item in items:
                response = await self._get(
                    self.doctor_actor, f"/fhir/r4/MedicationRequest/{item.id}"
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(
                    response.headers["content-type"].startswith("application/fhir+json")
                )
                resource = response.json()
                exported_ids.add(resource["id"])
                expected_status = {
                    "active": "active",
                    "completed": "completed",
                    "discontinued": "stopped",
                    "cancelled": "cancelled",
                    "superseded": "cancelled",
                }[status_name]
                self.assertEqual(resource["resourceType"], "MedicationRequest")
                self.assertEqual(resource["status"], expected_status)
                self.assertEqual(resource["subject"]["reference"], f"Patient/{self.patient_id}")
                self.assertEqual(
                    resource["medicationCodeableConcept"]["text"], item.medicine_name_snapshot
                )
                self.assertEqual(resource["dosageInstruction"][0]["asNeededBoolean"], item.is_prn)
                self.assertIn("Dose: 1 tablet", resource["dosageInstruction"][0]["text"])
                self.assertNotIn("requester", resource)
        self.assertEqual(len(exported_ids), 6)
        self.assertEqual(len(exported_ids), len(self.items_by_status["active"]) + 4)

        request_id = patient_response.headers["x-request-id"]
        async with self.session_factory() as session:
            event = await session.scalar(
                select(AuditEvent).where(AuditEvent.request_id == request_id)
            )
        self.assertIsNotNone(event)
        self.assertEqual(event.action, "api.request")
        self.assertEqual(event.actor_user_id, self.patient_id)
        self.assertEqual(event.metadata_["route"], "/fhir/r4/Patient/{patient_id}")
        self.assertIsNone(event.target_id)
        self.assertNotIn(str(self.patient_id), str(event.metadata_))

    async def test_patient_source_draft_missing_and_cross_patient_results(self):
        self.actor = self.patient_actor
        reviewed_patient_source = await self.client.get(
            f"/fhir/r4/MedicationRequest/{self.patient_source_item.id}"
        )
        self.assertEqual(reviewed_patient_source.status_code, 404)
        self.assertEqual(reviewed_patient_source.json()["code"], "RESOURCE_NOT_FOUND")

        draft = await self.client.get(f"/fhir/r4/MedicationRequest/{self.draft_item.id}")
        self.assertEqual(draft.status_code, 409)
        self.assertEqual(draft.json()["code"], "INVALID_STATE")

        cross_patient = await self.client.get(
            f"/fhir/r4/MedicationRequest/{self.cross_patient_item.id}"
        )
        self.assertEqual(cross_patient.status_code, 404)
        missing = await self.client.get(f"/fhir/r4/MedicationRequest/{uuid4()}")
        self.assertEqual(missing.status_code, 404)
        malformed = await self.client.get("/fhir/r4/MedicationRequest/not-a-uuid")
        self.assertEqual(malformed.status_code, 422)
        self.assertEqual(malformed.json()["code"], "VALIDATION_ERROR")
        self.assertNotIn("not-a-uuid", malformed.text)

    async def test_doctor_grant_must_be_verified_current_and_category_specific(self):
        current = await self._get(self.doctor_actor, f"/fhir/r4/Patient/{self.patient_id}")
        self.assertEqual(current.status_code, 200)
        item = self.items_by_status["active"][0]
        current_medication = await self._get(
            self.doctor_actor, f"/fhir/r4/MedicationRequest/{item.id}"
        )
        self.assertEqual(current_medication.status_code, 200)

        async with self.session_factory.begin() as session:
            await session.execute(
                delete(GrantCategory).where(
                    GrantCategory.grant_id == self.grant_id,
                    GrantCategory.category == "history_allergies",
                )
            )
        wrong_category = await self._get(self.doctor_actor, f"/fhir/r4/Patient/{self.patient_id}")
        self.assertEqual(wrong_category.status_code, 404)
        still_allowed = await self._get(self.doctor_actor, f"/fhir/r4/MedicationRequest/{item.id}")
        self.assertEqual(still_allowed.status_code, 200)

        async with self.session_factory.begin() as session:
            grant = await session.get(AccessGrant, self.grant_id)
            grant.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        expired = await self._get(self.doctor_actor, f"/fhir/r4/MedicationRequest/{item.id}")
        self.assertEqual(expired.status_code, 404)

        async with self.session_factory.begin() as session:
            grant = await session.get(AccessGrant, self.grant_id)
            grant.expires_at = datetime.now(UTC) + timedelta(days=1)
            grant.status = "revoked"
            grant.revoked_at = datetime.now(UTC)
        revoked = await self._get(self.doctor_actor, f"/fhir/r4/MedicationRequest/{item.id}")
        self.assertEqual(revoked.status_code, 404)

        no_grant = await self._get(
            self.no_grant_doctor_actor, f"/fhir/r4/Patient/{self.patient_id}"
        )
        self.assertEqual(no_grant.status_code, 404)
        existing_item = self.items_by_status["active"][0]
        no_grant_existing = await self._get(
            self.no_grant_doctor_actor,
            f"/fhir/r4/MedicationRequest/{existing_item.id}",
        )
        no_grant_missing = await self._get(
            self.no_grant_doctor_actor,
            f"/fhir/r4/MedicationRequest/{uuid4()}",
        )
        self.assertEqual(no_grant_existing.status_code, 404)
        self.assertEqual(no_grant_missing.status_code, 404)

        unverified = await self._get(
            self.unverified_doctor_actor, f"/fhir/r4/Patient/{self.patient_id}"
        )
        self.assertEqual(unverified.status_code, 403)
        self.assertEqual(unverified.json()["code"], "ACCESS_DENIED")
        unverified_existing = await self._get(
            self.unverified_doctor_actor,
            f"/fhir/r4/MedicationRequest/{existing_item.id}",
        )
        unverified_missing = await self._get(
            self.unverified_doctor_actor,
            f"/fhir/r4/MedicationRequest/{uuid4()}",
        )
        self.assertEqual(unverified_existing.status_code, 403)
        self.assertEqual(unverified_missing.status_code, 403)


__all__ = ["FhirExportsPostgresTests"]
