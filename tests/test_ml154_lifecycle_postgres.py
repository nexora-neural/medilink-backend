"""Real API/database lifecycle, history, clinical boundaries and rollback tests."""

from datetime import time, timedelta
from unittest import skipUnless
from unittest.mock import patch
from uuid import UUID, uuid4

from sqlalchemy import func, select

from app.modules.audit.models import AuditEvent
from app.modules.consent.models import GrantCategory
from app.modules.identity.models import UserRole
from app.modules.medication.models import (
    DoseOccurrence,
    MedicationLog,
    MedicationSchedule,
    ScheduleTime,
)
from app.modules.notifications.models import Notification
from app.modules.prescriptions import lifecycle, service
from app.modules.prescriptions.core_models import Prescription
from app.modules.prescriptions.lifecycle_models import PrescriptionLifecycleEvent
from app.modules.prescriptions.models import PrescriptionSharesModel
from tests.ml154_fixtures import URL, LifecycleFixture


@skipUnless(URL, "Set ML154_TEST_DATABASE_URL to a migrated disposable PostgreSQL database")
class LifecyclePostgresTests(LifecycleFixture):
    async def event_count(self, rx_id=None):
        return await self.db.scalar(
            select(func.count())
            .select_from(PrescriptionLifecycleEvent)
            .where(PrescriptionLifecycleEvent.prescription_id == (rx_id or self.rx_id))
        )

    async def test_three_version_actions_create_review_required_linked_drafts(self):
        for action, label in [
            ("versions", "version"),
            ("renewals", "renewal"),
            ("reissues", "reissue"),
        ]:
            old = await self.make_rx()
            await self.db.commit()
            response = await self.client.post(
                f"/api/v1/prescriptions/{old.id}/{action}", json=self.payload()
            )
            self.assertEqual(response.status_code, 201, response.text)
            body = response.json()
            rx = body["prescription"]
            self.assertEqual(rx["previous_prescription_id"], str(old.id))
            self.assertEqual(rx["source"], "patient")
            self.assertEqual(rx["status"], "draft")
            self.assertIsNone(rx["issued_at"])
            self.assertIsNone(rx["prescriber_user_id"])
            self.assertEqual(rx["review_status"], "not_reviewed")
            self.assertEqual(body["lifecycle_events"][0]["action"], label)
            self.assertEqual(body["lifecycle_events"][0]["reason"], self.payload()["reason"])
            self.assertEqual(old.status, "active")
            self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_confirming_patient_replacement_supersedes_only_then_and_preserves_items(self):
        item = await self.first_item()
        old_item_id, old_dose = item.id, item.dose_text
        response = await self.client.post(self.path + "/versions", json=self.payload())
        self.assertEqual(response.status_code, 201, response.text)
        child_id = response.json()["prescription"]["id"]
        self.assertEqual(self.rx.status, "active")
        self.assertEqual(
            (await self.client.post(f"/api/v1/prescriptions/{child_id}/confirm")).status_code, 200
        )
        self.assertEqual(self.rx.status, "superseded")
        self.assertEqual((await self.first_item()).id, old_item_id)
        self.assertEqual((await self.first_item()).dose_text, old_dose)
        self.assertEqual((await self.client.post(self.path + "/confirm")).status_code, 409)
        history = await self.client.get(f"/api/v1/prescriptions/{child_id}/history")
        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual(history.json()["total"], 2)
        self.assertEqual(
            {row["prescription"]["source"] for row in history.json()["items"]}, {"patient"}
        )
        self.assertEqual(await self.event_count(), 1)

    async def test_duplicate_pending_draft_and_stale_version_are_rejected(self):
        response = await self.client.post(
            self.path + "/versions", json=self.payload(), headers={"If-Match": "9"}
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "STALE_VERSION")
        self.assertEqual(
            (await self.client.post(self.path + "/versions", json=self.payload())).status_code, 201
        )
        self.assertEqual(
            (await self.client.post(self.path + "/renewals", json=self.payload())).status_code, 409
        )
        self.assertEqual(self.rx.version, 1)

    async def test_doctor_correction_requires_separate_issue_then_supersedes(self):
        old = await self.make_rx(doctor=True)
        await self.db.commit()
        self.actor = self.doctor
        response = await self.client.post(
            f"/api/v1/prescriptions/{old.id}/versions", json=self.payload(doctor=True)
        )
        self.assertEqual(response.status_code, 201, response.text)
        child_id = response.json()["prescription"]["id"]
        self.assertEqual(old.status, "active")
        issued = await self.client.post(f"/api/v1/prescriptions/{child_id}/issue")
        self.assertEqual(issued.status_code, 200, issued.text)
        self.assertEqual(old.status, "superseded")
        self.assertEqual(issued.json()["prescription"]["source"], "doctor")
        self.assertEqual(issued.json()["prescription"]["prescriber_user_id"], str(self.doctor.id))
        self.assertEqual(issued.json()["prescription"]["review_status"], "not_reviewed")

    async def test_doctor_reissue_of_patient_evidence_is_a_separate_order(self):
        self.actor = self.doctor
        response = await self.client.post(self.path + "/reissues", json=self.payload(doctor=True))
        self.assertEqual(response.status_code, 201, response.text)
        new = response.json()["prescription"]
        self.assertEqual(new["source"], "doctor")
        self.assertEqual(self.rx.source, "patient")
        self.assertIsNone(self.rx.prescriber_user_id)
        self.assertEqual(new["previous_prescription_id"], str(self.rx_id))
        self.assertEqual(self.rx.status, "active")
        issued = await self.client.post(f"/api/v1/prescriptions/{new['id']}/issue")
        self.assertEqual(issued.status_code, 200, issued.text)
        self.assertEqual(self.rx.source, "patient")
        self.assertIsNone(self.rx.reviewed_by)
        self.assertEqual(self.rx.status, "superseded")

    async def test_patient_and_nonowner_doctor_cannot_terminate_doctor_order(self):
        old = await self.make_rx(doctor=True)
        await self.db.commit()
        path = f"/api/v1/prescriptions/{old.id}/cancel"
        self.assertEqual(
            (await self.client.post(path, json={"reason": "Synthetic"})).status_code, 403
        )
        old.prescriber_user_id = self.admin.id
        await self.db.commit()
        self.actor = self.doctor
        self.assertEqual(
            (await self.client.post(path, json={"reason": "Synthetic"})).status_code, 403
        )
        self.assertEqual(old.status, "active")

    async def test_doctor_cannot_version_patient_evidence_without_explicit_reissue(self):
        self.actor = self.doctor
        for action in ["versions", "renewals"]:
            response = await self.client.post(
                self.path + "/" + action, json=self.payload(doctor=True)
            )
            self.assertEqual(response.status_code, 403)

    async def test_renewal_from_completed_order_requires_fresh_review_and_issue(self):
        old = await self.make_rx(doctor=True, status="completed")
        await self.db.commit()
        self.actor = self.doctor
        response = await self.client.post(
            f"/api/v1/prescriptions/{old.id}/renewals", json=self.payload(doctor=True)
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["prescription"]["status"], "draft")
        self.assertEqual(old.status, "completed")

    async def test_terminal_state_matrix_and_same_reason_retry_are_safe(self):
        for action, target in [
            ("cancel", "cancelled"),
            ("discontinue", "discontinued"),
            ("complete", "completed"),
        ]:
            old = await self.make_rx()
            await self.db.commit()
            path = f"/api/v1/prescriptions/{old.id}/{action}"
            first = await self.client.post(path, json={"reason": "Private reason"})
            self.assertEqual(first.status_code, 201, first.text)
            self.assertEqual(first.json()["prescription"]["status"], target)
            self.assertEqual(first.json()["prescription"]["version"], 2)
            retry = await self.client.post(path, json={"reason": "Private reason"})
            self.assertEqual(retry.json(), first.json())
            self.assertEqual(
                (await self.client.post(path, json={"reason": "Changed reason"})).status_code, 409
            )
            other_action = "complete" if action != "complete" else "cancel"
            self.assertEqual(
                (
                    await self.client.post(
                        f"/api/v1/prescriptions/{old.id}/{other_action}",
                        json={"reason": "Private reason"},
                    )
                ).status_code,
                409,
            )
            self.assertEqual(await self.event_count(old.id), 1)

    async def test_drafts_cancelled_and_superseded_cannot_be_published_again(self):
        for state in ["draft", "cancelled", "discontinued", "superseded"]:
            old = await self.make_rx(status=state)
            await self.db.commit()
            response = await self.client.post(
                f"/api/v1/prescriptions/{old.id}/versions", json=self.payload()
            )
            self.assertEqual(response.status_code, 409, response.text)
        draft = await self.make_rx(status="draft")
        await self.db.commit()
        self.assertEqual(
            (
                await self.client.post(
                    f"/api/v1/prescriptions/{draft.id}/complete", json={"reason": "Test"}
                )
            ).status_code,
            409,
        )

    async def test_other_patient_and_privileged_clinical_access_are_denied(self):
        for actor in [self.other, self.admin]:
            self.actor = actor
            self.assertEqual((await self.client.get(self.path + "/history")).status_code, 404)
            self.assertEqual(
                (
                    await self.client.post(self.path + "/cancel", json={"reason": "Test"})
                ).status_code,
                404,
            )
        self.assertEqual(self.rx.status, "active")

    async def test_doctor_mutations_recheck_verification_grant_and_consultation(self):
        old = await self.make_rx(doctor=True)
        await self.db.commit()
        self.actor = self.doctor
        path = f"/api/v1/prescriptions/{old.id}/cancel"
        self.grant.expires_at = self.now - timedelta(seconds=1)
        await self.db.commit()
        self.assertEqual((await self.client.post(path, json={"reason": "Test"})).status_code, 403)
        self.grant.expires_at = self.now + timedelta(hours=1)
        self.consult.ended_at = self.now
        await self.db.commit()
        self.assertEqual((await self.client.post(path, json={"reason": "Test"})).status_code, 403)
        self.consult.ended_at = None
        self.grant.revoked_at = self.now
        self.grant.status = "revoked"
        await self.db.commit()
        self.assertEqual((await self.client.post(path, json={"reason": "Test"})).status_code, 403)

    async def test_history_needs_current_read_grant_but_not_consultation_write(self):
        self.actor = self.doctor
        self.grant.allow_consultation = False
        self.consult.ended_at = self.now
        await self.db.commit()
        self.assertEqual((await self.client.get(self.path + "/history")).status_code, 200)
        category = await self.db.scalar(
            select(GrantCategory).where(GrantCategory.grant_id == self.grant.id)
        )
        category.category = "history_allergies"
        await self.db.commit()
        self.assertEqual((await self.client.get(self.path + "/history")).status_code, 404)

    async def test_revoked_patient_role_and_inactive_account_are_rechecked(self):
        assignment = await self.db.scalar(
            select(UserRole).where(UserRole.user_id == self.patient_id)
        )
        assignment.revoked_at = self.now
        await self.db.commit()
        self.assertEqual(
            (await self.client.post(self.path + "/cancel", json={"reason": "Test"})).status_code,
            403,
        )
        assignment.revoked_at = None
        self.patient.status = "suspended"
        await self.db.commit()
        self.assertEqual(
            (await self.client.post(self.path + "/versions", json=self.payload())).status_code, 403
        )

    async def test_reason_history_is_private_and_audit_contains_only_safe_metadata(self):
        secret = "Private synthetic clinical reason not for generic audit"
        response = await self.client.post(self.path + "/cancel", json={"reason": secret})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["lifecycle_events"][0]["reason"], secret)
        self.assertEqual(response.json()["prescription"]["status_reason"], secret)
        events = (
            await self.db.scalars(select(AuditEvent).where(AuditEvent.target_id == self.rx_id))
        ).all()
        self.assertTrue(events)
        self.assertNotIn(secret, str([row.metadata_ for row in events]))
        self.assertNotIn("firebase_uid", response.text)

    async def test_history_traverses_ancestors_descendants_and_bounded_paging(self):
        first = await self.client.post(self.path + "/versions", json=self.payload())
        child_id = first.json()["prescription"]["id"]
        self.assertEqual(
            (await self.client.post(f"/api/v1/prescriptions/{child_id}/confirm")).status_code, 200
        )
        again = await self.client.post(
            f"/api/v1/prescriptions/{child_id}/renewals", json=self.payload()
        )
        self.assertEqual(again.status_code, 201, again.text)
        leaf = again.json()["prescription"]["id"]
        page = await self.client.get(f"/api/v1/prescriptions/{leaf}/history?page_size=1")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertEqual(page.json()["total"], 3)
        self.assertEqual(len(page.json()["items"]), 1)
        empty = await self.client.get(f"/api/v1/prescriptions/{leaf}/history?page=10&page_size=1")
        self.assertEqual(empty.json()["items"], [])
        self.assertEqual(empty.json()["total"], 3)

    async def test_history_terminates_cycles_and_does_not_follow_foreign_patient_edges(self):
        other_rx = Prescription(
            patient_user_id=self.other.id,
            created_by=self.other.id,
            source="patient",
            status="draft",
        )
        self.db.add(other_rx)
        await self.db.flush()
        self.rx.previous_prescription_id = other_rx.id
        await self.db.commit()
        response = await self.client.get(self.path + "/history")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["total"], 1)
        self.rx.previous_prescription_id = self.rx_id
        await self.db.commit()
        response = await self.client.get(self.path + "/history")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["total"], 1)

    async def test_status_audit_failure_rolls_back_state_private_history_and_version(self):
        with patch.object(lifecycle, "append_event", side_effect=RuntimeError("private-failure")):
            response = await self.client.post(self.path + "/cancel", json={"reason": "Test"})
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("private-failure", response.text)
        self.assertEqual(self.rx.status, "active")
        self.assertEqual(self.rx.version, 1)
        self.assertEqual(await self.event_count(), 0)

    async def test_version_audit_failure_leaves_no_linked_draft(self):
        with patch.object(lifecycle, "append_event", side_effect=RuntimeError("synthetic")):
            response = await self.client.post(self.path + "/versions", json=self.payload())
        self.assertEqual(response.status_code, 500)
        count = await self.db.scalar(
            select(func.count())
            .select_from(Prescription)
            .where(Prescription.previous_prescription_id == self.rx_id)
        )
        self.assertEqual(count, 0)
        self.assertEqual(self.rx.status, "active")

    async def make_dependents(self):
        item = await self.first_item()
        schedule = MedicationSchedule(
            patient_user_id=self.patient_id,
            prescription_item_id=item.id,
            timezone="UTC",
            starts_at=self.now - timedelta(days=1),
            ends_at=self.now + timedelta(days=4),
            confirmed_at=self.now - timedelta(days=1),
            status="active",
        )
        self.db.add(schedule)
        await self.db.flush()
        self.db.add(ScheduleTime(schedule_id=schedule.id, time_of_day=time(8)))
        rows = []
        for offset, status in [(-1, "taken"), (-2, "unreported"), (1, "upcoming"), (2, "taken")]:
            row = DoseOccurrence(
                schedule_id=schedule.id,
                scheduled_for=self.now + timedelta(hours=offset),
                status=status,
            )
            self.db.add(row)
            await self.db.flush()
            if status == "taken":
                self.db.add(
                    MedicationLog(
                        dose_occurrence_id=row.id,
                        recorded_by=self.patient_id,
                        reported_taken_at=self.now,
                        response="taken",
                        recorded_at=self.now,
                    )
                )
            rows.append(row)
        share = PrescriptionSharesModel(
            prescription_id=self.rx_id,
            patient_user_id=self.patient_id,
            claim_token_hash=str(uuid4()),
            claim_expires_at=self.now + timedelta(minutes=15),
        )
        self.db.add(share)
        await self.db.commit()
        return schedule, rows, share

    async def test_termination_stops_future_reminders_and_revokes_shares_preserving_logs(self):
        schedule, rows, share = await self.make_dependents()
        before = [row.scheduled_for for row in rows]
        response = await self.client.post(
            self.path + "/discontinue", json={"reason": "Private stop reason"}
        )
        self.assertEqual(response.status_code, 201, response.text)
        await self.db.refresh(schedule)
        await self.db.refresh(share)
        for row in rows:
            await self.db.refresh(row)
        self.assertEqual(schedule.status, "cancelled")
        self.assertEqual(share.status, "revoked")
        self.assertIsNotNone(share.revoked_at)
        self.assertEqual(
            [row.status for row in rows], ["taken", "unreported", "cancelled", "taken"]
        )
        self.assertEqual([row.scheduled_for for row in rows], before)
        self.assertEqual(
            await self.db.scalar(
                select(func.count())
                .select_from(MedicationLog)
                .where(MedicationLog.dose_occurrence_id.in_([row.id for row in rows]))
            ),
            2,
        )
        from app.modules.medication.occurrence_worker import process_active_schedules

        notifications_before = await self.db.scalar(select(func.count()).select_from(Notification))
        await process_active_schedules(self.db, now=self.now + timedelta(hours=3))
        self.assertEqual(
            await self.db.scalar(select(func.count()).select_from(Notification)),
            notifications_before,
        )

    async def test_replacement_activation_failure_restores_predecessor_and_dependents(self):
        schedule, rows, share = await self.make_dependents()
        created = await self.client.post(self.path + "/versions", json=self.payload())
        child_id = created.json()["prescription"]["id"]
        with patch.object(service, "append_event", side_effect=RuntimeError("synthetic")):
            response = await self.client.post(f"/api/v1/prescriptions/{child_id}/confirm")
        self.assertEqual(response.status_code, 500)
        await self.db.refresh(schedule)
        await self.db.refresh(share)
        await self.db.refresh(rows[2])
        self.assertEqual(self.rx.status, "active")
        self.assertEqual(schedule.status, "active")
        self.assertEqual(share.status, "created")
        self.assertEqual(rows[2].status, "upcoming")
        self.assertEqual(await self.event_count(), 0)
        child = await self.db.get(Prescription, UUID(child_id))
        self.assertEqual(child.status, "draft")

    async def test_version_creation_revalidates_evidence_references(self):
        body = self.payload() | {"original_document_id": str(uuid4())}
        response = await self.client.post(self.path + "/versions", json=body)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.rx.status, "active")

    async def test_stale_terminal_action_rejects_without_changing_state(self):
        response = await self.client.post(
            self.path + "/cancel", json={"reason": "Test"}, headers={"If-Match": "9"}
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "STALE_VERSION")
        self.assertEqual(self.rx.status, "active")
