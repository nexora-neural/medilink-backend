"""ML-155 route tests on migrated PostgreSQL with synthetic clinical fixtures."""

import os
from datetime import UTC, date, datetime, time, timedelta
from unittest import IsolatedAsyncioTestCase, skipUnless
from unittest.mock import patch
from uuid import UUID, uuid4

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.database.session import get_session
from app.main import app
from app.modules.audit.models import AuditEvent
from app.modules.consent.models import AccessGrant, GrantCategory
from app.modules.dispensing.models import DispensingEventsModel
from app.modules.identity.auth import get_current_user
from app.modules.identity.models import Role, User, UserRole
from app.modules.identity.request_limits import require_authenticated_rate_limit
from app.modules.medication import service
from app.modules.medication.models import (
    DoseOccurrence,
    MedicationLog,
    MedicationSchedule,
    ScheduleStatusChange,
    ScheduleTime,
)
from app.modules.organizations.models import Organization, OrganizationMembership
from app.modules.prescriptions.core_models import Prescription, PrescriptionItem
from app.modules.profiles.models import PatientProfile, ProfessionalProfile

URL = os.getenv("ML155_TEST_DATABASE_URL")


@skipUnless(URL, "Set ML155_TEST_DATABASE_URL to a migrated disposable PostgreSQL database")
class SchedulePostgresTests(IsolatedAsyncioTestCase):
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
        self.patient = await self.user("patient")
        self.other = await self.user("patient")
        self.doctor = await self.user("doctor")
        self.admin = await self.user("super_admin")
        self.now = datetime.now(UTC)
        self.prescription = Prescription(
            patient_user_id=self.patient.id,
            source="patient",
            created_by=self.patient.id,
            status="active",
            issued_at=self.now - timedelta(days=2),
            starts_on=(self.now - timedelta(days=2)).date(),
            ends_on=(self.now + timedelta(days=10)).date(),
        )
        self.db.add(self.prescription)
        await self.db.flush()
        self.item = PrescriptionItem(
            prescription_id=self.prescription.id,
            medicine_name_snapshot="Synthetic medicine",
            dose_text="Original clinical dose",
            frequency="daily",
            instructions="Original instructions",
            duration_days=13,
            is_prn=False,
        )
        self.db.add(self.item)
        await self.db.commit()
        self.actor = self.patient

        async def session():
            try:
                yield self.db
            finally:
                # Real requests have separate sessions; refresh fixtures after a
                # failed-request rollback expires objects in our shared session.
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
        self.path = f"/api/v1/patients/{self.patient.id}/medication-schedules"
        self.body = {
            "prescription_item_id": str(self.item.id),
            "timezone": "UTC",
            "starts_at": (self.now + timedelta(hours=1)).isoformat(),
            "ends_at": (self.now + timedelta(days=5)).isoformat(),
            "times": ["08:00:00"],
            "confirmed": True,
        }

    async def user(self, role):
        row = await self.db.scalar(select(Role).where(Role.code == role))
        if row is None:
            row = Role(code=role, label=role)
            self.db.add(row)
            await self.db.flush()
        user = User(
            firebase_uid=f"ml155-{uuid4()}", display_name="ML155 synthetic", status="active"
        )
        self.db.add(user)
        await self.db.flush()
        self.db.add(UserRole(user_id=user.id, role_id=row.id))
        if role == "patient":
            self.db.add(
                PatientProfile(user_id=user.id, date_of_birth=date(1990, 1, 1), sex="female")
            )
        if role == "doctor":
            self.db.add(
                ProfessionalProfile(
                    user_id=user.id,
                    role="doctor",
                    doctor_reference=f"ml155-{uuid4()}",
                    verification_status="approved",
                )
            )
        await self.db.flush()
        return user

    async def create(self, body=None):
        return await self.client.post(self.path, json=body or self.body)

    async def status(self, schedule, state, version, reason="Synthetic private reason"):
        return await self.client.post(
            f"/api/v1/medication-schedules/{schedule}/status",
            json={"status": state, "reason": reason},
            headers={"If-Match": str(version)},
        )

    async def grant(self, *, category="medication_logs"):
        grant = AccessGrant(
            patient_user_id=self.patient.id,
            professional_user_id=self.doctor.id,
            organization_id=None,
            status="active",
            allow_consultation=False,
            starts_at=self.now - timedelta(hours=1),
            expires_at=self.now + timedelta(hours=1),
        )
        self.db.add(grant)
        await self.db.flush()
        self.db.add(GrantCategory(grant_id=grant.id, category=category))
        await self.db.commit()
        return grant

    async def test_confirm_list_source_and_clinical_instruction_preservation(self):
        response = await self.create()
        self.assertEqual(response.status_code, 201, response.text)
        data = response.json()
        self.assertEqual(data["prescription_source"], "patient")
        self.assertEqual(data["schedule"]["status"], "active")
        self.assertEqual(data["times"][0]["time_of_day"], "08:00:00")
        self.assertEqual(data["schedule"]["version"], 1)
        self.assertEqual(self.item.dose_text, "Original clinical dose")
        self.assertEqual(self.item.instructions, "Original instructions")
        listing = await self.client.get(self.path)
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual(listing.json()["total"], 1)
        self.assertEqual(listing.headers["Cache-Control"], "no-store")
        self.assertEqual(await self.db.scalar(select(func.count()).select_from(DoseOccurrence)), 0)

    async def test_other_patient_doctor_and_admin_cannot_write(self):
        for actor in (self.other, self.doctor, self.admin):
            self.actor = actor
            self.assertEqual((await self.create()).status_code, 404)
        self.actor = self.patient
        self.item.prescription_id = self.prescription.id
        self.prescription.patient_user_id = self.other.id
        await self.db.commit()
        self.assertEqual((await self.create()).status_code, 404)

    async def test_active_role_and_local_account_rechecked(self):
        role = await self.db.scalar(select(UserRole).where(UserRole.user_id == self.patient.id))
        role.revoked_at = self.now
        await self.db.commit()
        self.assertEqual((await self.create()).status_code, 403)
        self.patient.status = "suspended"
        await self.db.commit()
        self.assertEqual((await self.client.get(self.path)).status_code, 403)

    async def test_draft_and_terminal_prescriptions_cannot_create(self):
        for state in ("draft", "completed", "discontinued", "cancelled", "superseded"):
            self.prescription.status = state
            await self.db.commit()
            self.assertEqual((await self.create()).status_code, 409)

    async def test_prn_weekly_intervals_and_tapers_rejected(self):
        for frequency, prn in [
            ("daily", True),
            ("weekly", False),
            ("every 8 hours", False),
            ("taper as directed", False),
        ]:
            self.item.frequency, self.item.is_prn = frequency, prn
            await self.db.commit()
            self.assertEqual((await self.create()).status_code, 422)

    async def test_daily_frequency_count_is_preserved(self):
        self.item.frequency = "twice daily"
        await self.db.commit()
        self.assertEqual((await self.create()).status_code, 422)
        response = await self.create(self.body | {"times": ["20:00:00", "08:00:00"]})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(
            [row["time_of_day"] for row in response.json()["times"]], ["08:00:00", "20:00:00"]
        )

    async def test_course_start_end_duration_and_timezone_bounds(self):
        for changes in [
            {"starts_at": (self.now - timedelta(days=4)).isoformat()},
            {"ends_at": None},
            {"ends_at": (self.now + timedelta(days=20)).isoformat()},
            {
                "starts_at": (self.now - timedelta(days=1)).isoformat(),
                "ends_at": (self.now - timedelta(hours=1)).isoformat(),
            },
        ]:
            self.assertEqual((await self.create(self.body | changes)).status_code, 422)
        self.prescription.ends_on = None
        self.item.duration_days = 3
        await self.db.commit()
        self.assertEqual((await self.create()).status_code, 422)

    async def test_overlaps_paused_reservations_and_boundary_adjacency(self):
        first = await self.create()
        self.assertEqual(first.status_code, 201)
        self.assertEqual((await self.create()).status_code, 409)
        schedule_id = first.json()["schedule"]["id"]
        self.assertEqual((await self.status(schedule_id, "paused", 1)).status_code, 201)
        self.assertEqual((await self.create()).status_code, 409)
        second = await self.create(
            self.body
            | {
                "starts_at": self.body["ends_at"],
                "ends_at": (self.now + timedelta(days=8)).isoformat(),
            }
        )
        self.assertEqual(second.status_code, 201, second.text)
        listing = await self.client.get(self.path + "?page_size=1&status=active")
        self.assertEqual(listing.json()["total"], 1)
        self.assertEqual(len(listing.json()["items"]), 1)

    async def add_occurrences(self, schedule_id):
        row = await self.db.get(MedicationSchedule, UUID(schedule_id))
        row.starts_at = self.now - timedelta(days=1)
        past = DoseOccurrence(
            schedule_id=row.id, scheduled_for=self.now - timedelta(hours=1), status="skipped"
        )
        future = DoseOccurrence(
            schedule_id=row.id, scheduled_for=self.now + timedelta(hours=2), status="upcoming"
        )
        self.db.add_all([past, future])
        await self.db.flush()
        self.db.add(
            MedicationLog(
                dose_occurrence_id=past.id, response="skipped", recorded_by=self.patient.id
            )
        )
        await self.db.commit()
        return past, future

    async def test_pause_resume_is_reminder_only_and_no_duplicate_occurrences(self):
        data = (await self.create()).json()
        schedule = data["schedule"]["id"]
        past, future = await self.add_occurrences(schedule)
        original = future.scheduled_for
        self.assertEqual((await self.status(schedule, "paused", 1)).status_code, 201)
        self.assertEqual((await self.status(schedule, "active", 2)).status_code, 201)
        repeat = await self.status(schedule, "active", 3)
        self.assertEqual(repeat.status_code, 201)
        self.assertEqual(repeat.json()["schedule"]["version"], 3)
        self.assertEqual(future.scheduled_for, original)
        self.assertEqual(past.status, "skipped")
        self.assertEqual(await self.db.scalar(select(func.count()).select_from(DoseOccurrence)), 2)
        self.assertEqual(
            await self.db.scalar(select(func.count()).select_from(ScheduleStatusChange)), 2
        )
        event = await self.db.scalar(
            select(AuditEvent)
            .where(AuditEvent.action == "medication_schedule.status_changed")
            .limit(1)
        )
        self.assertNotIn("Synthetic private reason", str(event.metadata_))
        reason = await self.db.scalar(select(ScheduleStatusChange.reason).limit(1))
        self.assertEqual(reason, "Synthetic private reason")

    async def test_end_cancels_only_future_unanswered_and_terminal_cannot_resume(self):
        schedule = (await self.create()).json()["schedule"]["id"]
        past, future = await self.add_occurrences(schedule)
        self.assertEqual((await self.status(schedule, "completed", 1)).status_code, 201)
        self.assertEqual(future.status, "cancelled")
        self.assertEqual(past.status, "skipped")
        self.assertEqual(await self.db.scalar(select(func.count()).select_from(MedicationLog)), 1)
        self.assertEqual((await self.status(schedule, "active", 2)).status_code, 409)

    async def test_status_stale_and_resume_after_prescription_end_rejected(self):
        schedule = (await self.create()).json()["schedule"]["id"]
        self.assertEqual((await self.status(schedule, "paused", 1)).status_code, 201)
        self.assertEqual((await self.status(schedule, "active", 1)).status_code, 409)
        self.prescription.status = "discontinued"
        await self.db.commit()
        self.assertEqual((await self.status(schedule, "active", 2)).status_code, 409)

    async def test_replacement_retains_link_old_times_past_logs_and_stale_guard(self):
        old = (await self.create()).json()["schedule"]["id"]
        past, future = await self.add_occurrences(old)
        path = f"/api/v1/medication-schedules/{old}/replacement"
        response = await self.client.post(
            path, json=self.body | {"times": ["09:00:00"]}, headers={"If-Match": '"1"'}
        )
        self.assertEqual(response.status_code, 201, response.text)
        new = response.json()
        self.assertEqual(new["schedule"]["previous_schedule_id"], old)
        self.assertEqual(new["prescription_source"], "patient")
        self.assertEqual(future.status, "cancelled")
        self.assertEqual(past.status, "skipped")
        self.assertEqual(await self.db.scalar(select(func.count()).select_from(MedicationLog)), 1)
        times = list(
            (
                await self.db.scalars(
                    select(ScheduleTime).where(ScheduleTime.schedule_id == UUID(old))
                )
            ).all()
        )
        self.assertEqual(times[0].time_of_day, time(8))
        stale = await self.client.post(path, json=self.body, headers={"If-Match": '"1"'})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["code"], "STALE_VERSION")

    async def test_doctor_requires_current_medication_category_grant(self):
        await self.create()
        grant = await self.grant()
        self.actor = self.doctor
        self.assertEqual((await self.client.get(self.path)).status_code, 200)
        grant.status, grant.revoked_at = "revoked", self.now
        await self.db.commit()
        self.assertEqual((await self.client.get(self.path)).status_code, 404)
        self.assertEqual((await self.create()).status_code, 404)

    async def test_wrong_category_expiry_and_verification_cannot_read(self):
        grant = await self.grant(category="prescriptions")
        self.actor = self.doctor
        self.assertEqual((await self.client.get(self.path)).status_code, 404)
        await self.db.delete(await self.db.get(GrantCategory, (grant.id, "prescriptions")))
        self.db.add(GrantCategory(grant_id=grant.id, category="medication_logs"))
        grant.expires_at = self.now - timedelta(minutes=1)
        await self.db.commit()
        self.assertEqual((await self.client.get(self.path)).status_code, 404)
        profile = await self.db.get(ProfessionalProfile, self.doctor.id)
        profile.verification_status = "not_submitted"
        await self.db.commit()
        self.assertEqual((await self.client.get(self.path)).status_code, 403)

    async def test_audit_failure_rolls_back_create_and_status_history(self):
        with patch.object(service, "append_event", side_effect=RuntimeError("private synthetic")):
            response = await self.create()
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("private synthetic", response.text)
        self.assertEqual(
            await self.db.scalar(select(func.count()).select_from(MedicationSchedule)), 0
        )
        data = (await self.create()).json()
        with patch.object(service, "append_event", side_effect=RuntimeError("synthetic")):
            response = await self.status(data["schedule"]["id"], "paused", 1)
        self.assertEqual(response.status_code, 500)
        row = await self.db.get(MedicationSchedule, UUID(data["schedule"]["id"]))
        self.assertEqual(row.status, "active")
        self.assertEqual(row.version, 1)
        self.assertEqual(
            await self.db.scalar(select(func.count()).select_from(ScheduleStatusChange)), 0
        )

    async def test_dispensing_never_shifts_confirmed_times_or_clinical_instructions(self):
        self.prescription.source = "doctor"
        self.prescription.prescriber_user_id = self.doctor.id
        await self.db.commit()
        response = await self.create()
        self.assertEqual(response.status_code, 201, response.text)
        before = response.json()
        self.assertEqual(before["prescription_source"], "doctor")
        pharmacist = await self.user("pharmacist")
        pharmacy = Organization(name="Synthetic pharmacy", type="pharmacy", status="active")
        self.db.add(pharmacy)
        await self.db.flush()
        self.db.add(
            DispensingEventsModel(
                prescription_id=self.prescription.id,
                pharmacist_user_id=pharmacist.id,
                organization_id=pharmacy.id,
                request_key=f"ml155-dispense-{uuid4()}",
            )
        )
        await self.db.commit()
        after = (await self.client.get(self.path)).json()["items"][0]
        self.assertEqual(after, before)
        self.assertEqual(self.item.dose_text, "Original clinical dose")
        self.assertEqual(self.item.instructions, "Original instructions")

    async def test_doctor_organization_grants_need_current_approved_membership(self):
        org = Organization(name="Synthetic clinic", type="clinic", status="active")
        self.db.add(org)
        await self.db.flush()
        grant = await self.grant()
        grant.organization_id = org.id
        membership = OrganizationMembership(
            user_id=self.doctor.id, organization_id=org.id, role="doctor", status="approved"
        )
        self.db.add(membership)
        await self.db.commit()
        self.actor = self.doctor
        self.assertEqual((await self.client.get(self.path)).status_code, 200)
        membership.status = "suspended"
        await self.db.commit()
        self.assertEqual((await self.client.get(self.path)).status_code, 404)
        # A different valid personal grant still works despite the suspended org.
        await self.grant()
        self.assertEqual((await self.client.get(self.path)).status_code, 200)
