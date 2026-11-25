"""Checks for connected synthetic demo data on a dedicated migrated database.

Run seed_demo first and set DEMO_SEED_TEST_DATABASE_URL for this test.
The test rolls back all clinical records it creates.
"""

import os
from datetime import UTC, datetime, timedelta
from unittest import IsolatedAsyncioTestCase, skipUnless

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import create_async_engine

from app.database.base import Base
from app.database.seed_demo import sample_id
from app.database.seed_synthetic_journeys import demo_id, seed

URL = os.getenv("DEMO_SEED_TEST_DATABASE_URL")


@skipUnless(URL, "Set DEMO_SEED_TEST_DATABASE_URL to a dedicated migrated database")
class DemoSeedPostgresTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine(URL)
        self.addAsyncCleanup(self.engine.dispose)
        self.connection = await self.engine.connect()
        self.addAsyncCleanup(self.connection.close)
        self.transaction = await self.connection.begin()
        self.addAsyncCleanup(self.transaction.rollback)

    async def test_repeatable_records_and_patient_links(self):
        now = datetime.now(UTC)
        await seed(self.connection, now)
        tables = Base.metadata.tables
        names = (
            "conditions",
            "diagnoses",
            "allergies",
            "clinical_notes",
            "access_grants",
            "consultations",
            "medicines",
            "prescriptions",
            "prescription_items",
            "prescription_ocr_jobs",
            "prescription_templates",
            "prescription_template_items",
            "medication_schedules",
            "schedule_times",
            "dose_occurrences",
            "medication_logs",
            "prescription_shares",
            "dispensing_events",
            "dispensing_items",
            "documents",
            "reports",
            "measurements",
            "vaccinations",
            "grant_categories",
            "users",
            "user_roles",
            "professional_profiles",
            "organizations",
            "organization_memberships",
        )
        before = {
            name: (await self.connection.execute(select(tables[name]))).all() for name in names
        }
        await seed(self.connection, now + timedelta(hours=1))
        after = {
            name: (await self.connection.execute(select(tables[name]))).all() for name in names
        }
        self.assertEqual(before, after)
        patient = sample_id("user", "patient")
        doctor = sample_id("user", "doctor")
        rx = (
            (
                await self.connection.execute(
                    select(tables["prescriptions"]).where(
                        tables["prescriptions"].c.id == demo_id("prescription")
                    )
                )
            )
            .mappings()
            .one()
        )
        self.assertEqual((rx["patient_user_id"], rx["prescriber_user_id"]), (patient, doctor))
        item = (
            (
                await self.connection.execute(
                    select(tables["prescription_items"]).where(
                        tables["prescription_items"].c.id == demo_id("item")
                    )
                )
            )
            .mappings()
            .one()
        )
        self.assertEqual(
            (item["prescription_id"], item["medicine_id"]), (rx["id"], demo_id("medicine"))
        )
        report = (
            (
                await self.connection.execute(
                    select(tables["reports"]).where(tables["reports"].c.id == demo_id("report"))
                )
            )
            .mappings()
            .one()
        )
        self.assertEqual(
            (report["patient_user_id"], report["document_id"]),
            (patient, demo_id("report-document")),
        )
        grant = (
            (
                await self.connection.execute(
                    select(tables["access_grants"]).where(
                        tables["access_grants"].c.id == demo_id("grant")
                    )
                )
            )
            .mappings()
            .one()
        )
        self.assertEqual(
            (grant["patient_user_id"], grant["professional_user_id"]), (patient, doctor)
        )
        self.assertEqual(
            len([r for r in after["grant_categories"] if r.grant_id == grant["id"]]), 5
        )
        share = (
            await self.connection.execute(
                select(tables["prescription_shares"]).where(
                    tables["prescription_shares"].c.id == demo_id("historical-share")
                )
            )
        ).mappings().one()
        self.assertEqual(share["status"], "revoked")
        self.assertEqual(share["patient_user_id"], patient)
        self.assertIsNotNone(share["revoked_at"])
        event = (
            await self.connection.execute(
                select(tables["dispensing_events"]).where(
                    tables["dispensing_events"].c.id == demo_id("historical-dispensing")
                )
            )
        ).mappings().one()
        self.assertEqual(
            (event["prescription_id"], event["share_id"]),
            (demo_id("prescription"), demo_id("historical-share")),
        )
        self.assertEqual(event["pharmacist_user_id"], demo_id("pharmacist"))
        dispensing_item = (
            await self.connection.execute(
                select(tables["dispensing_items"]).where(
                    tables["dispensing_items"].c.id == demo_id("historical-dispensing-item")
                )
            )
        ).mappings().one()
        self.assertEqual(
            (dispensing_item["event_id"], dispensing_item["prescription_item_id"]),
            (demo_id("historical-dispensing"), demo_id("item")),
        )
        self.assertEqual(dispensing_item["quantity"], 10)

    async def test_rejects_existing_record_owned_by_another_patient(self):
        now = datetime.now(UTC)
        await seed(self.connection, now)
        rows = Base.metadata.tables["reports"]
        other = demo_id("other-user")
        users = Base.metadata.tables["users"]
        profiles = Base.metadata.tables["patient_profiles"]
        await self.connection.execute(
            users.insert().values(
                id=other, firebase_uid=str(other), display_name="Synthetic other patient"
            )
        )
        await self.connection.execute(
            profiles.insert().values(
                user_id=other, date_of_birth=datetime(1991, 1, 1).date(), sex="female"
            )
        )
        await self.connection.execute(
            update(rows).where(rows.c.id == demo_id("report")).values(patient_user_id=other)
        )
        with self.assertRaisesRegex(RuntimeError, "identity collision"):
            await seed(self.connection, now)

    async def test_patient_and_doctor_can_read_seeded_measurements(self):
        from unittest.mock import patch

        from httpx import ASGITransport, AsyncClient
        from sqlalchemy.ext.asyncio import AsyncSession

        from app.database.session import get_session
        from app.main import app
        from app.modules.identity.auth import get_verified_identity

        await seed(self.connection, datetime.now(UTC))
        session = AsyncSession(
            bind=self.connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        self.addAsyncCleanup(session.close)
        actor = {"uid": "synthetic:ml26:patient"}

        async def session_override():
            yield session

        async def identity_override():
            return actor

        overrides = patch.dict(
            app.dependency_overrides,
            {get_session: session_override, get_verified_identity: identity_override},
        )
        overrides.start()
        self.addCleanup(overrides.stop)
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        self.addAsyncCleanup(client.aclose)
        url = f"/api/v1/patients/{sample_id('user', 'patient')}/measurements"
        for uid in ("synthetic:ml26:patient", "synthetic:ml26:doctor"):
            actor["uid"] = uid
            response = await client.get(url)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(
                {record["type"] for record in response.json()["items"]}, {"weight", "blood_glucose"}
            )

    async def test_explicit_accounts_require_existing_approved_profiles(self):
        from datetime import date
        from uuid import uuid4

        tables = Base.metadata.tables
        patient, doctor = uuid4(), uuid4()
        for uid, user_id, role in (
            ("test-patient", patient, "patient"),
            ("test-doctor", doctor, "doctor"),
        ):
            await self.connection.execute(
                tables["users"]
                .insert()
                .values(
                    id=user_id,
                    firebase_uid=uid,
                    display_name="Fictional test user",
                    status="active",
                )
            )
            role_id = await self.connection.scalar(
                select(tables["roles"].c.id).where(tables["roles"].c.code == role)
            )
            await self.connection.execute(
                tables["user_roles"].insert().values(user_id=user_id, role_id=role_id)
            )
        await self.connection.execute(
            tables["patient_profiles"]
            .insert()
            .values(user_id=patient, date_of_birth=date(1990, 1, 1), sex="female")
        )
        await self.connection.execute(
            tables["professional_profiles"]
            .insert()
            .values(
                user_id=doctor,
                role="doctor",
                doctor_reference="FAKE-TEST-UID",
                verification_status="pending",
            )
        )
        with self.assertRaisesRegex(RuntimeError, "approved doctor profile"):
            await seed(self.connection, datetime.now(UTC), "test-patient", "test-doctor")
        await self.connection.execute(
            update(tables["professional_profiles"])
            .where(tables["professional_profiles"].c.user_id == doctor)
            .values(verification_status="approved")
        )
        now = datetime.now(UTC)
        existing = await self.connection.scalar(
            select(tables["access_grants"].c.id).where(
                tables["access_grants"].c.id == demo_id("grant")
            )
        )
        if existing is not None:
            with self.assertRaisesRegex(RuntimeError, "identity collision"):
                await seed(self.connection, now, "test-patient", "test-doctor")
            return
        await seed(self.connection, now, "test-patient", "test-doctor")
        await seed(self.connection, now + timedelta(hours=1), "test-patient", "test-doctor")
        grant = (
            (
                await self.connection.execute(
                    select(tables["access_grants"]).where(
                        tables["access_grants"].c.id == demo_id("grant")
                    )
                )
            )
            .mappings()
            .one()
        )
        self.assertEqual(
            (grant["patient_user_id"], grant["professional_user_id"]), (patient, doctor)
        )
        role = await self.connection.scalar(
            select(tables["professional_profiles"].c.verification_status).where(
                tables["professional_profiles"].c.user_id == doctor
            )
        )
        self.assertEqual(role, "approved")

    async def test_patient_can_find_medicine_and_download_report(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from httpx import ASGITransport, AsyncClient
        from sqlalchemy.ext.asyncio import AsyncSession

        from app.core.config import settings
        from app.database.seed_synthetic_journeys import REPORT_BYTES
        from app.database.session import get_session
        from app.main import app
        from app.modules.identity.auth import get_current_user
        from app.modules.identity.models import User
        from app.modules.identity.request_limits import require_authenticated_rate_limit

        await seed(self.connection, datetime.now(UTC))
        session = AsyncSession(
            bind=self.connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        self.addAsyncCleanup(session.close)
        actor = await session.get(User, sample_id("user", "patient"))
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        (Path(temporary.name) / demo_id("report-document").hex).write_bytes(REPORT_BYTES)
        storage_config = patch.object(settings, "private_upload_dir", temporary.name)
        storage_config.start()
        self.addCleanup(storage_config.stop)

        async def session_override():
            yield session

        async def user_override():
            return actor

        async def no_limit():
            return None

        overrides = patch.dict(
            app.dependency_overrides,
            {
                get_session: session_override,
                get_current_user: user_override,
                require_authenticated_rate_limit: no_limit,
            },
        )
        overrides.start()
        self.addCleanup(overrides.stop)
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        self.addAsyncCleanup(client.aclose)
        response = await client.get("/api/v1/medicines", params={"q": "Synthetic medicine"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn(str(demo_id("medicine")), {str(x["id"]) for x in response.json()["items"]})
        response = await client.get(f"/api/v1/documents/{demo_id('report-document')}/content")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.content, REPORT_BYTES)
        # A different patient cannot retrieve the private seeded document.
        from uuid import uuid4

        other_id = uuid4()
        other = User(
            id=other_id, firebase_uid=str(other_id), display_name="Synthetic other", status="active"
        )
        session.add(other)
        await session.flush()
        role_id = await session.scalar(
            select(Base.metadata.tables["roles"].c.id).where(
                Base.metadata.tables["roles"].c.code == "patient"
            )
        )
        await session.execute(
            Base.metadata.tables["user_roles"].insert().values(user_id=other_id, role_id=role_id)
        )
        await session.flush()
        actor = other
        response = await client.get(f"/api/v1/documents/{demo_id('report-document')}/content")
        self.assertIn(response.status_code, (403, 404), response.text)
        self.assertNotEqual(response.content, REPORT_BYTES)

    async def test_rejects_existing_document_with_other_storage_key(self):
        now = datetime.now(UTC)
        await seed(self.connection, now)
        documents = Base.metadata.tables["documents"]
        await self.connection.execute(
            update(documents)
            .where(documents.c.id == demo_id("report-document"))
            .values(storage_key="f" * 32)
        )
        with self.assertRaisesRegex(RuntimeError, "documents identity collision"):
            await seed(self.connection, now)

    async def test_doctor_queue_and_consultation_use_seeded_grant(self):
        from unittest.mock import patch

        from httpx import ASGITransport, AsyncClient
        from sqlalchemy.ext.asyncio import AsyncSession

        from app.database.session import get_session
        from app.main import app
        from app.modules.identity.auth import get_current_user
        from app.modules.identity.models import User
        from app.modules.identity.request_limits import require_authenticated_rate_limit

        await seed(self.connection, datetime.now(UTC))
        session = AsyncSession(
            bind=self.connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        self.addAsyncCleanup(session.close)
        doctor = await session.get(User, sample_id("user", "doctor"))

        async def session_override():
            yield session

        async def user_override():
            return doctor

        async def no_limit():
            return None

        overrides = patch.dict(
            app.dependency_overrides,
            {
                get_session: session_override,
                get_current_user: user_override,
                require_authenticated_rate_limit: no_limit,
            },
        )
        overrides.start()
        self.addCleanup(overrides.stop)
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        self.addAsyncCleanup(client.aclose)
        response = await client.get("/api/v1/me/patient-queue")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn(str(demo_id("grant")), {str(x["grant_id"]) for x in response.json()["items"]})
        response = await client.post(
            "/api/v1/consultations", json={"grant_id": str(demo_id("grant"))}
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["patient_user_id"], str(sample_id("user", "patient")))

    async def test_clinical_and_medication_records_form_a_connected_journey(self):
        await seed(self.connection, datetime.now(UTC))
        t = Base.metadata.tables
        patient = sample_id("user", "patient")
        doctor = sample_id("user", "doctor")

        async def row(name, identifier):
            return (
                (
                    await self.connection.execute(
                        select(t[name]).where(t[name].c.id == demo_id(identifier))
                    )
                )
                .mappings()
                .one()
            )

        consultation = await row("consultations", "consultation")
        self.assertEqual(
            (
                consultation["patient_user_id"],
                consultation["doctor_user_id"],
                consultation["grant_id"],
            ),
            (patient, doctor, demo_id("grant")),
        )
        for table, identifier in (
            ("conditions", "condition"),
            ("diagnoses", "diagnosis"),
            ("allergies", "allergy"),
            ("clinical_notes", "note"),
        ):
            clinical = await row(table, identifier)
            self.assertEqual(
                (clinical["patient_user_id"], clinical["recorded_by"], clinical["consultation_id"]),
                (patient, doctor, consultation["id"]),
            )
        schedule = await row("medication_schedules", "schedule")
        occurrence = await row("dose_occurrences", "dose")
        log = await row("medication_logs", "dose-log")
        schedule_time = await row("schedule_times", "schedule-time")
        self.assertEqual(
            (schedule["patient_user_id"], schedule["prescription_item_id"]),
            (patient, demo_id("item")),
        )
        self.assertEqual(schedule_time["schedule_id"], schedule["id"])
        self.assertEqual(occurrence["schedule_id"], schedule["id"])
        self.assertEqual(
            (log["dose_occurrence_id"], log["recorded_by"], log["response"]),
            (occurrence["id"], patient, "taken"),
        )
        template = await row("prescription_templates", "template")
        template_item = await row("prescription_template_items", "template-item")
        self.assertEqual(template["doctor_user_id"], doctor)
        self.assertEqual(
            (template_item["template_id"], template_item["medicine_id"]),
            (template["id"], demo_id("medicine")),
        )
        vaccine = await row("vaccinations", "vaccination")
        self.assertEqual((vaccine["patient_user_id"], vaccine["recorded_by"]), (patient, patient))
        weights = (
            (
                await self.connection.execute(
                    select(t["measurements"])
                    .where(
                        t["measurements"].c.patient_user_id == patient,
                        t["measurements"].c.type == "weight",
                    )
                    .order_by(t["measurements"].c.measured_at)
                )
            )
            .mappings()
            .all()
        )
        self.assertEqual(
            [str(record["value"]) for record in weights], ["70.0000", "69.0000", "68.0000"]
        )

    async def test_synthetic_pharmacy_and_laboratory_staff_memberships(self):
        await seed(self.connection, datetime.now(UTC))
        tables = Base.metadata.tables
        for role, organization in (("pharmacist", "pharmacy"), ("lab_staff", "laboratory")):
            profile = (
                (
                    await self.connection.execute(
                        select(tables["professional_profiles"]).where(
                            tables["professional_profiles"].c.user_id == demo_id(role)
                        )
                    )
                )
                .mappings()
                .one()
            )
            membership = (
                (
                    await self.connection.execute(
                        select(tables["organization_memberships"]).where(
                            tables["organization_memberships"].c.id == demo_id(f"{role}-membership")
                        )
                    )
                )
                .mappings()
                .one()
            )
            self.assertEqual((profile["role"], profile["verification_status"]), (role, "approved"))
            self.assertEqual(
                (membership["user_id"], membership["organization_id"], membership["status"]),
                (demo_id(role), demo_id(organization), "approved"),
            )

    async def test_patient_can_issue_lab_request_resolvable_by_seeded_lab_staff(self):
        from unittest.mock import patch

        from httpx import ASGITransport, AsyncClient
        from sqlalchemy.ext.asyncio import AsyncSession

        from app.database.session import get_session
        from app.main import app
        from app.modules.identity.auth import get_current_user
        from app.modules.identity.models import User
        from app.modules.identity.request_limits import (
            require_authenticated_rate_limit,
            require_resolution_rate_limit,
        )

        await seed(self.connection, datetime.now(UTC))
        session = AsyncSession(
            bind=self.connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        self.addAsyncCleanup(session.close)
        actor = await session.get(User, sample_id("user", "patient"))

        async def session_override():
            yield session

        async def user_override():
            return actor

        async def no_limit():
            return None

        overrides = patch.dict(
            app.dependency_overrides,
            {
                get_session: session_override,
                get_current_user: user_override,
                require_authenticated_rate_limit: no_limit,
                require_resolution_rate_limit: no_limit,
            },
        )
        overrides.start()
        self.addCleanup(overrides.stop)
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        self.addAsyncCleanup(client.aclose)
        lab = str(demo_id("laboratory"))
        response = await client.post("/api/v1/lab-upload-requests", json={"laboratory_id": lab})
        self.assertEqual(response.status_code, 201, response.text)
        token = response.json()["request_token"]
        actor = await session.get(User, demo_id("lab_staff"))
        response = await client.post(
            "/api/v1/lab-upload-requests/resolve",
            json={
                "laboratory_id": lab,
                "request_token": token,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json()["patient"]["patient_user_id"], str(sample_id("user", "patient"))
        )

    async def test_staff_memberships_pass_the_real_authorization_guard(self):
        from fastapi import HTTPException
        from sqlalchemy.ext.asyncio import AsyncSession

        from app.modules.identity.authorization import require_verified_membership
        from app.modules.identity.models import User

        await seed(self.connection, datetime.now(UTC))
        session = AsyncSession(bind=self.connection, expire_on_commit=False)
        self.addAsyncCleanup(session.close)
        for role, organization in (("pharmacist", "pharmacy"), ("lab_staff", "laboratory")):
            actor = await session.get(User, demo_id(role))
            await require_verified_membership(session, actor, role, demo_id(organization))
            other_org = "laboratory" if organization == "pharmacy" else "pharmacy"
            with self.assertRaises(HTTPException) as denied:
                await require_verified_membership(session, actor, role, demo_id(other_org))
            self.assertEqual(denied.exception.status_code, 403)

    async def test_cli_removes_new_file_when_database_seed_fails(self):
        import asyncio
        import sys
        import tempfile
        from pathlib import Path

        from sqlalchemy.engine import make_url

        url = make_url(URL)
        with tempfile.TemporaryDirectory() as directory:
            env = os.environ.copy()
            env.update(
                {
                    "APP_ENV": "test",
                    "PRIVATE_UPLOAD_DIR": directory,
                    "DB_HOST": url.host or "localhost",
                    "DB_PORT": str(url.port or 5432),
                    "DB_NAME": url.database,
                    "DB_USER": url.username or "postgres",
                    "DB_PASSWORD": url.password or "",
                }
            )
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "app.database.seed_synthetic_journeys",
                "--apply",
                "--patient-uid",
                "nonexistent-fictional-patient",
                "--doctor-uid",
                "nonexistent-fictional-doctor",
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await process.communicate()
            self.assertNotEqual(process.returncode, 0)
            self.assertIn(b"test account", stderr)
            self.assertEqual(list(Path(directory).iterdir()), [])

    async def test_rejects_existing_clinical_record_with_other_author(self):
        await seed(self.connection, datetime.now(UTC))
        conditions = Base.metadata.tables["conditions"]
        await self.connection.execute(
            update(conditions)
            .where(conditions.c.id == demo_id("condition"))
            .values(recorded_by=sample_id("user", "patient"))
        )
        with self.assertRaisesRegex(RuntimeError, "conditions identity collision"):
            await seed(self.connection, datetime.now(UTC))

    async def test_patient_can_replace_seeded_medication_schedule(self):
        from unittest.mock import patch

        from httpx import ASGITransport, AsyncClient
        from sqlalchemy.ext.asyncio import AsyncSession

        from app.database.session import get_session
        from app.main import app
        from app.modules.identity.auth import get_current_user
        from app.modules.identity.models import User
        from app.modules.identity.request_limits import require_authenticated_rate_limit

        await seed(self.connection, datetime.now(UTC))
        session = AsyncSession(
            bind=self.connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        self.addAsyncCleanup(session.close)
        patient = await session.get(User, sample_id("user", "patient"))

        async def session_override():
            yield session

        async def actor_override():
            return patient

        overrides = patch.dict(
            app.dependency_overrides,
            {
                get_session: session_override,
                get_current_user: actor_override,
                require_authenticated_rate_limit: lambda: None,
            },
        )
        overrides.start()
        self.addCleanup(overrides.stop)
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        self.addAsyncCleanup(client.aclose)
        payload = {
            "prescription_item_id": str(demo_id("item")),
            "timezone": "Asia/Colombo",
            "starts_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "times": ["09:00:00"],
            "confirmed": True,
        }
        url = f"/api/v1/medication-schedules/{demo_id('schedule')}/replacement"
        response = await client.post(url, json=payload, headers={"If-Match": '"1"'})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["schedule"]["patient_user_id"], str(patient.id))
        old_log = await self.connection.scalar(
            select(Base.metadata.tables["medication_logs"].c.id).where(
                Base.metadata.tables["medication_logs"].c.id == demo_id("dose-log")
            )
        )
        self.assertEqual(old_log, demo_id("dose-log"))

    async def test_reference_multi_item_patient_source_and_adherence_fixture(self):
        await seed(self.connection, datetime.now(UTC))
        tables = Base.metadata.tables
        items = (
            (
                await self.connection.execute(
                    select(tables["prescription_items"]).where(
                        tables["prescription_items"].c.prescription_id == demo_id("prescription")
                    )
                )
            )
            .mappings()
            .all()
        )
        self.assertEqual({item["id"] for item in items}, {demo_id("item"), demo_id("item-second")})
        patient_rx = (
            (
                await self.connection.execute(
                    select(tables["prescriptions"]).where(
                        tables["prescriptions"].c.id == demo_id("patient-prescription")
                    )
                )
            )
            .mappings()
            .one()
        )
        self.assertEqual(
            (patient_rx["source"], patient_rx["status"], patient_rx["review_status"]),
            ("patient", "draft", "not_reviewed"),
        )
        occurrences = (
            (
                await self.connection.execute(
                    select(tables["dose_occurrences"]).where(
                        tables["dose_occurrences"].c.schedule_id == demo_id("schedule")
                    )
                )
            )
            .mappings()
            .all()
        )
        past = [
            item for item in occurrences if item["status"] in ("taken", "skipped", "unreported")
        ]
        self.assertEqual(len(past), 4)
        self.assertEqual(sum(item["status"] == "taken" for item in past), 2)
        self.assertEqual(sum(item["status"] == "skipped" for item in past), 1)
        self.assertEqual(sum(item["status"] == "unreported" for item in past), 1)
        self.assertEqual(sum(item["status"] == "upcoming" for item in occurrences), 1)

    async def test_failed_ocr_fixture_keeps_private_original_and_patient_draft(self):
        await seed(self.connection, datetime.now(UTC))
        t = Base.metadata.tables
        job = (
            (
                await self.connection.execute(
                    select(t["prescription_ocr_jobs"]).where(
                        t["prescription_ocr_jobs"].c.id == demo_id("ocr-failed")
                    )
                )
            )
            .mappings()
            .one()
        )
        document = (
            (
                await self.connection.execute(
                    select(t["documents"]).where(t["documents"].c.id == job["original_document_id"])
                )
            )
            .mappings()
            .one()
        )
        draft = (
            (
                await self.connection.execute(
                    select(t["prescriptions"]).where(
                        t["prescriptions"].c.id == demo_id("patient-prescription")
                    )
                )
            )
            .mappings()
            .one()
        )
        patient = sample_id("user", "patient")
        self.assertEqual(
            (job["patient_user_id"], job["status"], job["attempt_count"]), (patient, "failed", 1)
        )
        self.assertEqual(
            (document["owner_user_id"], document["patient_user_id"], document["document_type"]),
            (patient, patient, "prescription"),
        )
        self.assertEqual(
            (draft["original_document_id"], draft["source"], draft["status"]),
            (document["id"], "patient", "draft"),
        )
        self.assertIsNone(job["resulting_prescription_id"])
