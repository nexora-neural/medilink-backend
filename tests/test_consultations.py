"""ML-64 consultation validation and start-service tests."""

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError

from app.modules.consent.models import AccessGrant
from app.modules.consultations import service
from app.modules.consultations.models import Consultation
from app.modules.consultations.schemas import (
    ConsultationEnd,
    ConsultationInput,
    ConsultationListQuery,
)


class ConsultationValidationTests(TestCase):
    def test_start_accepts_only_a_grant_id(self):
        grant_id = uuid4()
        body = ConsultationInput(grant_id=grant_id)

        self.assertEqual(body.grant_id, grant_id)

        for field in (
            "patient_user_id",
            "doctor_user_id",
            "organization_id",
            "started_at",
        ):
            with self.subTest(field=field):
                with self.assertRaises(ValidationError):
                    ConsultationInput.model_validate(
                        {"grant_id": str(grant_id), field: str(uuid4())}
                    )

    def test_start_rejects_missing_or_invalid_grant_id(self):
        for payload in ({}, {"grant_id": "invalid"}):
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    ConsultationInput.model_validate(payload)

    def test_end_accepts_an_optional_summary(self):
        self.assertEqual(ConsultationEnd().summary, "")
        self.assertEqual(
            ConsultationEnd(summary="Synthetic summary.").summary,
            "Synthetic summary.",
        )

    def test_end_rejects_invalid_summary_and_extra_fields(self):
        for payload in (
            {"summary": "x" * 4001},
            {"summary": None},
            {"summary": 123},
            {"ended_at": "2026-11-21T12:00:00Z"},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    ConsultationEnd.model_validate(payload)


class ConsultationStartTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = datetime.now(UTC)
        self.doctor = SimpleNamespace(id=uuid4(), status="active")
        self.patient = SimpleNamespace(id=uuid4(), status="active")
        self.db = AsyncMock()
        self.db.get.return_value = self.patient

        self.verified = self.enterContext(patch.object(service, "require_verified_professional"))
        self.membership = self.enterContext(patch.object(service, "require_verified_membership"))
        self.lock = self.enterContext(patch.object(service.repository, "lock_doctor_grant"))
        self.categories = self.enterContext(
            patch.object(
                service.consent_repository,
                "categories_by_grant",
            )
        )
        self.insert = self.enterContext(
            patch.object(
                service.repository,
                "insert_consultation",
                side_effect=self.assign_consultation_id,
            )
        )
        self.audit = self.enterContext(patch.object(service, "append_event"))
        clock = self.enterContext(patch.object(service, "datetime"))
        clock.now.return_value = self.now

        self.grant = AccessGrant(
            id=uuid4(),
            patient_user_id=self.patient.id,
            professional_user_id=self.doctor.id,
            organization_id=None,
            status="active",
            allow_consultation=True,
            starts_at=self.now - timedelta(hours=1),
            expires_at=self.now + timedelta(hours=1),
            revoked_at=None,
        )
        self.lock.return_value = self.grant
        self.categories.return_value = {self.grant.id: ["history_allergies"]}

    async def assign_consultation_id(self, db, consultation):
        consultation.id = uuid4()

    async def start(self):
        return await service.start_consultation(
            self.db,
            self.doctor,
            ConsultationInput(grant_id=self.grant.id),
            "test-request",
        )

    def assert_no_write(self):
        self.insert.assert_not_awaited()
        self.audit.assert_not_awaited()
        self.db.commit.assert_not_awaited()

    async def test_start_derives_identity_and_context_from_grant(self):
        result = await self.start()

        self.verified.assert_awaited_once_with(self.db, self.doctor, "doctor")
        self.lock.assert_awaited_once_with(self.db, self.doctor.id, self.grant.id)
        self.assertEqual(result.patient_user_id, self.patient.id)
        self.assertEqual(result.doctor_user_id, self.doctor.id)
        self.assertEqual(result.grant_id, self.grant.id)
        self.assertIsNone(result.organization_id)
        self.assertEqual(result.started_at, self.now)
        self.assertIsNone(result.ended_at)
        self.assertIsNone(result.summary)
        self.db.commit.assert_awaited_once()
        self.assertEqual(
            self.audit.await_args.kwargs["action"],
            "consultation.started",
        )
        self.assertEqual(
            self.audit.await_args.kwargs["metadata"],
            {},
        )

    async def test_start_checks_membership_for_the_grant_organization(self):
        self.grant.organization_id = uuid4()

        result = await self.start()

        self.membership.assert_awaited_once_with(
            self.db,
            self.doctor,
            "doctor",
            self.grant.organization_id,
        )
        self.assertEqual(
            result.organization_id,
            self.grant.organization_id,
        )

    async def test_missing_or_other_doctor_grant_returns_not_found(self):
        self.lock.return_value = None

        with self.assertRaises(HTTPException) as raised:
            await self.start()

        self.assertEqual(raised.exception.status_code, 404)
        self.assert_no_write()

    async def test_unverified_doctor_is_rejected_before_grant_lookup(self):
        self.verified.side_effect = HTTPException(403, "Access denied.")

        with self.assertRaises(HTTPException) as raised:
            await self.start()

        self.assertEqual(raised.exception.status_code, 403)
        self.lock.assert_not_awaited()
        self.assert_no_write()

    async def test_unapproved_membership_blocks_start(self):
        self.grant.organization_id = uuid4()
        self.membership.side_effect = HTTPException(403, "Access denied.")

        with self.assertRaises(HTTPException) as raised:
            await self.start()

        self.assertEqual(raised.exception.status_code, 403)
        self.assert_no_write()

    async def test_read_only_grant_cannot_start_consultation(self):
        self.grant.allow_consultation = False

        with self.assertRaises(HTTPException) as raised:
            await self.start()

        self.assertEqual(raised.exception.status_code, 403)
        self.assert_no_write()

    async def test_expired_revoked_and_future_grants_are_rejected(self):
        cases = [
            {"expires_at": self.now},
            {"expires_at": self.now - timedelta(seconds=1)},
            {"status": "expired"},
            {"status": "revoked", "revoked_at": self.now},
            {"revoked_at": self.now},
            {"starts_at": self.now + timedelta(seconds=1)},
        ]

        for changes in cases:
            with self.subTest(changes=changes):
                self.grant.status = "active"
                self.grant.revoked_at = None
                self.grant.starts_at = self.now - timedelta(hours=1)
                self.grant.expires_at = self.now + timedelta(hours=1)

                for key, value in changes.items():
                    setattr(self.grant, key, value)

                with self.assertRaises(HTTPException) as raised:
                    await self.start()

                self.assertEqual(raised.exception.status_code, 403)
                self.assert_no_write()

    async def test_grant_without_categories_cannot_start_consultation(self):
        self.categories.return_value = {}

        with self.assertRaises(HTTPException) as raised:
            await self.start()

        self.assertEqual(raised.exception.status_code, 403)
        self.assert_no_write()

    async def test_inactive_patient_blocks_start(self):
        self.patient.status = "inactive"

        with self.assertRaises(HTTPException) as raised:
            await self.start()

        self.assertEqual(raised.exception.status_code, 403)
        self.assert_no_write()


class ConsultationEndTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = datetime.now(UTC)
        self.doctor = SimpleNamespace(id=uuid4(), status="active")
        self.patient = SimpleNamespace(id=uuid4(), status="active")
        self.db = AsyncMock()
        self.db.get.return_value = self.patient

        self.grant = AccessGrant(
            id=uuid4(),
            patient_user_id=self.patient.id,
            professional_user_id=self.doctor.id,
            organization_id=None,
            status="active",
            allow_consultation=True,
            starts_at=self.now - timedelta(hours=1),
            expires_at=self.now + timedelta(hours=1),
            revoked_at=None,
        )

        self.consultation = Consultation(
            id=uuid4(),
            patient_user_id=self.patient.id,
            doctor_user_id=self.doctor.id,
            organization_id=None,
            grant_id=self.grant.id,
            started_at=self.now - timedelta(minutes=10),
            ended_at=None,
            summary=None,
        )

        self.verified = self.enterContext(patch.object(service, "require_verified_professional"))
        self.membership = self.enterContext(patch.object(service, "require_verified_membership"))
        self.find = self.enterContext(
            patch.object(
                service.repository,
                "find_owned_consultation",
                return_value=self.consultation,
            )
        )
        self.lock = self.enterContext(
            patch.object(
                service.repository,
                "lock_doctor_grant",
                return_value=self.grant,
            )
        )
        self.categories = self.enterContext(
            patch.object(
                service.consent_repository,
                "categories_by_grant",
                return_value={self.grant.id: ["history_allergies"]},
            )
        )
        self.audit = self.enterContext(patch.object(service, "append_event"))

        clock = self.enterContext(patch.object(service, "datetime"))
        clock.now.return_value = self.now

    async def end(self, summary="Synthetic consultation summary."):
        return await service.end_consultation(
            self.db,
            self.doctor,
            self.consultation.id,
            ConsultationEnd(summary=summary),
            "test-request",
        )

    def assert_no_write(self):
        self.db.flush.assert_not_awaited()
        self.db.commit.assert_not_awaited()
        self.audit.assert_not_awaited()

    async def test_owner_ends_consultation_and_trims_summary(self):
        result = await self.end("  Synthetic consultation summary.  ")

        self.assertEqual(result.ended_at, self.now)
        self.assertEqual(
            result.summary,
            "Synthetic consultation summary.",
        )
        self.db.flush.assert_awaited_once()
        self.db.commit.assert_awaited_once()
        self.assertEqual(
            self.audit.await_args.kwargs["action"],
            "consultation.ended",
        )
        self.assertEqual(self.audit.await_args.kwargs["metadata"], {})

    async def test_blank_summary_is_saved_as_none(self):
        result = await self.end("   ")

        self.assertIsNone(result.summary)
        self.assertEqual(result.ended_at, self.now)

    async def test_repeated_end_preserves_original_summary_and_time(self):
        first = await self.end("Original synthetic summary.")
        original_time = first.ended_at

        second = await self.end("Replacement summary must not be saved.")

        self.assertEqual(second.ended_at, original_time)
        self.assertEqual(second.summary, "Original synthetic summary.")
        self.db.commit.assert_awaited_once()
        self.audit.assert_awaited_once()

    async def test_missing_or_other_doctor_consultation_is_not_found(self):
        self.find.return_value = None

        with self.assertRaises(HTTPException) as raised:
            await self.end()

        self.assertEqual(raised.exception.status_code, 404)
        self.lock.assert_not_awaited()
        self.assert_no_write()

    async def test_unverified_doctor_is_rejected_before_lookup(self):
        self.verified.side_effect = HTTPException(403, "Access denied.")

        with self.assertRaises(HTTPException) as raised:
            await self.end()

        self.assertEqual(raised.exception.status_code, 403)
        self.find.assert_not_awaited()
        self.assert_no_write()

    async def test_missing_grant_blocks_end(self):
        self.lock.return_value = None

        with self.assertRaises(HTTPException) as raised:
            await self.end()

        self.assertEqual(raised.exception.status_code, 403)
        self.assert_no_write()

    async def test_expired_grant_blocks_end(self):
        self.grant.expires_at = self.now

        with self.assertRaises(HTTPException) as raised:
            await self.end()

        self.assertEqual(raised.exception.status_code, 403)
        self.assertIsNone(self.consultation.ended_at)
        self.assert_no_write()

    async def test_revoked_grant_blocks_end(self):
        self.grant.status = "revoked"
        self.grant.revoked_at = self.now

        with self.assertRaises(HTTPException) as raised:
            await self.end()

        self.assertEqual(raised.exception.status_code, 403)
        self.assert_no_write()

    async def test_read_only_grant_blocks_end(self):
        self.grant.allow_consultation = False

        with self.assertRaises(HTTPException) as raised:
            await self.end()

        self.assertEqual(raised.exception.status_code, 403)
        self.assert_no_write()

    async def test_organization_membership_is_rechecked(self):
        organization_id = uuid4()
        self.grant.organization_id = organization_id
        self.consultation.organization_id = organization_id
        self.membership.side_effect = HTTPException(403, "Access denied.")

        with self.assertRaises(HTTPException) as raised:
            await self.end()

        self.assertEqual(raised.exception.status_code, 403)
        self.membership.assert_awaited_once_with(
            self.db,
            self.doctor,
            "doctor",
            organization_id,
        )
        self.assert_no_write()

    async def test_mismatched_patient_context_blocks_end(self):
        self.consultation.patient_user_id = uuid4()

        with self.assertRaises(HTTPException) as raised:
            await self.end()

        self.assertEqual(raised.exception.status_code, 403)
        self.assert_no_write()

    async def test_summary_is_unchanged_when_a_concurrent_request_ended_first(self):
        original_time = self.now - timedelta(seconds=1)

        async def find_consultation(db, doctor_id, consultation_id, *, lock=False):
            if lock:
                self.consultation.ended_at = original_time
                self.consultation.summary = "Summary saved by first request."
            return self.consultation

        self.find.side_effect = find_consultation

        result = await self.end("Second request summary.")

        self.assertEqual(result.ended_at, original_time)
        self.assertEqual(
            result.summary,
            "Summary saved by first request.",
        )
        self.assert_no_write()


class ConsultationReadTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = datetime.now(UTC)
        self.doctor = SimpleNamespace(id=uuid4(), status="active")
        self.patient = SimpleNamespace(id=uuid4(), status="active")
        self.db = AsyncMock()
        self.db.get.return_value = self.patient

    def consultation(self):
        return Consultation(
            id=uuid4(),
            patient_user_id=self.patient.id,
            doctor_user_id=self.doctor.id,
            organization_id=None,
            grant_id=uuid4(),
            started_at=self.now - timedelta(minutes=10),
            ended_at=None,
            summary=None,
        )

    async def test_queue_returns_minimal_identity_and_read_only_permission(self):
        grant = AccessGrant(
            id=uuid4(),
            patient_user_id=self.patient.id,
            professional_user_id=self.doctor.id,
            organization_id=None,
            status="active",
            allow_consultation=False,
            starts_at=self.now - timedelta(hours=1),
            expires_at=self.now + timedelta(hours=1),
            revoked_at=None,
        )
        query = ConsultationListQuery(page=2, page_size=10)

        with (
            patch.object(service, "require_verified_professional"),
            patch.object(
                service.repository,
                "list_doctor_queue",
                return_value=[(grant, "Synthetic patient", date(1990, 1, 1))],
            ) as queue,
            patch.object(
                service.consent_repository,
                "categories_by_grant",
                return_value={grant.id: ["reports"]},
            ),
        ):
            queue.return_value = (
                [(grant, "Synthetic patient", date(1990, 1, 1))],
                11,
            )

            result = await service.list_patient_queue(self.db, self.doctor, query)

        self.assertEqual(result.page, 2)
        self.assertEqual(result.page_size, 10)
        self.assertEqual(result.total, 11)
        self.assertEqual(len(result.items), 1)

        entry = result.items[0]
        self.assertEqual(entry.patient.patient_user_id, self.patient.id)
        self.assertEqual(entry.patient.display_name, "Synthetic patient")
        self.assertEqual(entry.categories, ["reports"])
        self.assertFalse(entry.allow_consultation)
        self.assertEqual(
            set(entry.patient.model_dump()),
            {"patient_user_id", "display_name", "date_of_birth"},
        )
        self.assertEqual(queue.await_args.args[:3], (self.db, self.doctor.id, query))
        self.db.commit.assert_not_awaited()

    async def test_unverified_doctor_cannot_load_queue(self):
        with (
            patch.object(
                service,
                "require_verified_professional",
                side_effect=HTTPException(403, "Access denied."),
            ),
            patch.object(
                service.repository,
                "list_doctor_queue",
            ) as queue,
        ):
            with self.assertRaises(HTTPException) as raised:
                await service.list_patient_queue(
                    self.db,
                    self.doctor,
                    ConsultationListQuery(),
                )

        self.assertEqual(raised.exception.status_code, 403)
        queue.assert_not_awaited()

    async def test_patient_lists_own_consultations(self):
        consultation = self.consultation()
        query = ConsultationListQuery()

        with (
            patch.object(service, "require_patient") as patient_check,
            patch.object(
                service,
                "require_verified_professional",
            ) as doctor_check,
            patch.object(
                service.repository,
                "list_patient_consultations",
                return_value=([consultation], 1),
            ) as listing,
        ):
            result = await service.list_consultations(
                self.db,
                self.patient,
                self.patient.id,
                query,
            )

        patient_check.assert_awaited_once_with(self.db, self.patient)
        doctor_check.assert_not_awaited()
        listing.assert_awaited_once_with(
            self.db,
            self.patient.id,
            query,
            doctor_id=None,
        )
        self.assertEqual(result.items[0].id, consultation.id)
        self.db.commit.assert_not_awaited()

    async def test_doctor_list_is_scoped_to_authenticated_doctor(self):
        query = ConsultationListQuery()

        with (
            patch.object(service, "require_verified_professional"),
            patch.object(
                service.repository,
                "list_patient_consultations",
                return_value=([], 0),
            ) as listing,
        ):
            result = await service.list_consultations(
                self.db,
                self.doctor,
                self.patient.id,
                query,
            )

        listing.assert_awaited_once_with(
            self.db,
            self.patient.id,
            query,
            doctor_id=self.doctor.id,
        )
        self.assertEqual(result.items, [])
        self.assertEqual(result.total, 0)

    async def test_non_doctor_cannot_list_another_patient_contexts(self):
        with (
            patch.object(
                service,
                "require_verified_professional",
                side_effect=HTTPException(403, "Access denied."),
            ),
            patch.object(
                service.repository,
                "list_patient_consultations",
            ) as listing,
        ):
            with self.assertRaises(HTTPException) as raised:
                await service.list_consultations(
                    self.db,
                    self.doctor,
                    self.patient.id,
                    ConsultationListQuery(),
                )

        self.assertEqual(raised.exception.status_code, 403)
        listing.assert_not_awaited()

    async def test_inactive_patient_blocks_doctor_listing(self):
        self.patient.status = "inactive"

        with (
            patch.object(service, "require_verified_professional"),
            patch.object(
                service.repository,
                "list_patient_consultations",
            ) as listing,
        ):
            with self.assertRaises(HTTPException) as raised:
                await service.list_consultations(
                    self.db,
                    self.doctor,
                    self.patient.id,
                    ConsultationListQuery(),
                )

        self.assertEqual(raised.exception.status_code, 403)
        listing.assert_not_awaited()
