"""Prescription draft service orchestration tests."""

from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from app.modules.identity.auth import AuthenticationError
from app.modules.prescriptions import service
from app.modules.prescriptions.schemas import PrescriptionInput


class PrescriptionDraftServiceTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.user = SimpleNamespace(id=uuid4())
        self.request = SimpleNamespace(state=SimpleNamespace(request_id="test-prescription"))
        self.session = SimpleNamespace(
            commit=AsyncMock(),
            rollback=AsyncMock(),
        )
        self.prescription = SimpleNamespace(
            id=uuid4(),
            patient_user_id=self.user.id,
            prescriber_user_id=None,
            consultation_id=None,
            source="patient",
            status="draft",
            issued_at=None,
            version=1,
        )
        self.payload = PrescriptionInput(items=[])

    async def test_delete_owned_draft_commits_and_audits(self):
        with (
            patch.object(
                service.repository,
                "lock_prescription",
                AsyncMock(return_value=self.prescription),
            ),
            patch.object(
                service,
                "require_prescription_access",
                AsyncMock(return_value="patient"),
            ),
            patch.object(
                service.repository,
                "delete_draft_rows",
                AsyncMock(),
            ) as delete_rows,
            patch.object(service, "append_event", AsyncMock()) as audit,
        ):
            await service.delete_draft(
                session=self.session,
                user=self.user,
                request=self.request,
                prescription_id=self.prescription.id,
            )

        delete_rows.assert_awaited_once_with(self.session, self.prescription.id)
        self.assertEqual(
            audit.await_args.kwargs["action"],
            "prescription.draft.deleted",
        )
        self.session.commit.assert_awaited_once()
        self.session.rollback.assert_not_awaited()

    async def test_delete_another_patients_draft_is_denied(self):
        self.prescription.patient_user_id = uuid4()

        with (
            patch.object(
                service.repository,
                "lock_prescription",
                AsyncMock(return_value=self.prescription),
            ),
            patch.object(
                service.repository,
                "delete_draft_rows",
                AsyncMock(),
            ) as delete_rows,
        ):
            with self.assertRaises(AuthenticationError) as caught:
                await service.delete_draft(
                    session=self.session,
                    user=self.user,
                    request=self.request,
                    prescription_id=self.prescription.id,
                )

        self.assertEqual(caught.exception.status_code, 403)
        delete_rows.assert_not_awaited()
        self.session.commit.assert_not_awaited()
        self.session.rollback.assert_awaited_once()

    async def test_delete_issued_record_is_rejected(self):
        self.prescription.status = "active"

        with (
            patch.object(
                service.repository,
                "lock_prescription",
                AsyncMock(return_value=self.prescription),
            ),
            patch.object(
                service,
                "require_prescription_access",
                AsyncMock(return_value="patient"),
            ),
            patch.object(
                service.repository,
                "delete_draft_rows",
                AsyncMock(),
            ) as delete_rows,
        ):
            with self.assertRaises(AuthenticationError) as caught:
                await service.delete_draft(
                    session=self.session,
                    user=self.user,
                    request=self.request,
                    prescription_id=self.prescription.id,
                )

        self.assertEqual(caught.exception.status_code, 409)
        delete_rows.assert_not_awaited()
        self.session.commit.assert_not_awaited()
        self.session.rollback.assert_awaited_once()

    async def test_delete_linked_record_rolls_back(self):
        class ForeignKeyViolation(Exception):
            sqlstate = "23503"

        error = IntegrityError("DELETE", {}, ForeignKeyViolation())

        with (
            patch.object(
                service.repository,
                "lock_prescription",
                AsyncMock(return_value=self.prescription),
            ),
            patch.object(
                service,
                "require_prescription_access",
                AsyncMock(return_value="patient"),
            ),
            patch.object(
                service.repository,
                "delete_draft_rows",
                AsyncMock(side_effect=error),
            ),
            patch.object(service, "append_event", AsyncMock()) as audit,
        ):
            with self.assertRaises(AuthenticationError) as caught:
                await service.delete_draft(
                    session=self.session,
                    user=self.user,
                    request=self.request,
                    prescription_id=self.prescription.id,
                )

        self.assertEqual(caught.exception.status_code, 409)
        audit.assert_not_awaited()
        self.session.commit.assert_not_awaited()
        self.session.rollback.assert_awaited_once()

    async def test_patient_cannot_edit_doctor_source_draft(self):
        self.prescription.source = "doctor"
        self.prescription.prescriber_user_id = uuid4()

        with (
            patch.object(
                service.repository,
                "lock_prescription",
                AsyncMock(return_value=self.prescription),
            ),
            patch.object(
                service.repository,
                "delete_prescription_items",
                AsyncMock(),
            ) as delete_items,
        ):
            with self.assertRaises(AuthenticationError) as caught:
                await service.replace_prescription_draft(
                    session=self.session,
                    user=self.user,
                    request=self.request,
                    prescription_id=self.prescription.id,
                    expected_version=1,
                    payload=self.payload,
                )

        self.assertEqual(caught.exception.status_code, 403)
        delete_items.assert_not_awaited()
        self.session.commit.assert_not_awaited()
        self.session.rollback.assert_awaited_once()

    async def test_stale_update_does_not_replace_items(self):
        self.prescription.version = 2

        with (
            patch.object(
                service.repository,
                "lock_prescription",
                AsyncMock(return_value=self.prescription),
            ),
            patch.object(
                service,
                "require_prescription_access",
                AsyncMock(return_value="patient"),
            ),
            patch.object(
                service.repository,
                "delete_prescription_items",
                AsyncMock(),
            ) as delete_items,
        ):
            with self.assertRaises(AuthenticationError) as caught:
                await service.replace_prescription_draft(
                    session=self.session,
                    user=self.user,
                    request=self.request,
                    prescription_id=self.prescription.id,
                    expected_version=1,
                    payload=self.payload,
                )

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.detail["code"], "STALE_VERSION")
        delete_items.assert_not_awaited()
        self.session.commit.assert_not_awaited()
        self.session.rollback.assert_awaited_once()

    async def test_denied_create_does_not_insert(self):
        denied = service.forbidden(self.request)

        with (
            patch.object(
                service,
                "require_prescription_access",
                AsyncMock(side_effect=denied),
            ),
            patch.object(
                service.repository,
                "create_prescription_draft",
                AsyncMock(),
            ) as create,
        ):
            with self.assertRaises(AuthenticationError) as caught:
                await service.create_draft(
                    session=self.session,
                    user=self.user,
                    request=self.request,
                    patient_id=self.user.id,
                    payload=self.payload,
                )

        self.assertEqual(caught.exception.status_code, 403)
        create.assert_not_awaited()
        self.session.commit.assert_not_awaited()
        self.session.rollback.assert_awaited_once()
