"""ML-48 integration checks against a separately provisioned PostgreSQL database."""

import os
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, skipUnless
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.database.session import get_session
from app.main import app
from app.modules.audit.models import AuditEvent
from app.modules.documents.enum import RecordSource
from app.modules.documents.models import Document
from app.modules.identity.auth import get_current_user
from app.modules.identity.models import Role, User, UserRole
from app.modules.identity.request_limits import require_authenticated_rate_limit
from app.modules.organizations.models import Organization
from app.modules.profiles.models import ProfessionalProfile
from app.modules.verification import service
from app.modules.verification.models import VerificationApplication, VerificationDecision

TEST_DATABASE_URL = os.getenv("ML48_TEST_DATABASE_URL")
BASE_PATH = "/api/v1/admin/verification-applications"


@skipUnless(TEST_DATABASE_URL, "Set ML48_TEST_DATABASE_URL to a dedicated migrated test database")
class VerificationAdminPostgreSQLTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine(TEST_DATABASE_URL)
        self.connection = await self.engine.connect()
        self.transaction = await self.connection.begin()
        self.session = AsyncSession(
            bind=self.connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        self.roles = {}
        for code in ("admin", "patient", "doctor", "pharmacist", "lab_staff", "super_admin"):
            role = await self.session.scalar(select(Role).where(Role.code == code))
            if role is None:
                role = Role(code=code, label=code)
                self.session.add(role)
                await self.session.flush()
            self.roles[code] = role.id

        self.reviewer = await self._create_user("ML48 reviewer", "admin")
        self.applicant = await self._create_user("ML48 applicant")
        self.applicant_id = self.applicant.id
        self.profile = ProfessionalProfile(
            user_id=self.applicant.id,
            role="doctor",
            doctor_reference=uuid4().hex,
            registration_number="SLMC-ML48-TEST",
            verification_status="pending",
        )
        self.session.add(self.profile)
        self.application = VerificationApplication(
            user_id=self.applicant.id,
            role="doctor",
            registration_number="SLMC-ML48-TEST",
            status="pending",
        )
        self.session.add(self.application)
        await self.session.flush()
        self.application_id = self.application.id
        await self.session.commit()
        self.actor = self.reviewer

        async def session_override():
            yield self.session

        async def current_user_override():
            return self.actor

        async def rate_limit_override():
            return None

        self.overrides = patch.dict(
            app.dependency_overrides,
            {
                get_session: session_override,
                get_current_user: current_user_override,
                require_authenticated_rate_limit: rate_limit_override,
            },
        )
        self.overrides.start()
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.overrides.stop()
        await self.session.close()
        await self.transaction.rollback()
        await self.connection.close()
        await self.engine.dispose()

    async def _create_user(self, name, role=None):
        user = User(firebase_uid=str(uuid4()), display_name=name, status="active")
        self.session.add(user)
        await self.session.flush()
        if role is not None:
            self.session.add(UserRole(user_id=user.id, role_id=self.roles[role]))
            await self.session.flush()
        return user

    async def test_approve_persists_application_role_history_and_audit(self):
        response = await self.client.get(
            BASE_PATH,
            params={"status": "pending", "role": "doctor", "page": 1, "page_size": 10},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertGreaterEqual(response.json()["total"], 1)
        self.assertIn(
            str(self.application_id),
            {item["id"] for item in response.json()["items"]},
        )

        response = await self.client.get(f"{BASE_PATH}/{self.application_id}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["evidence_document_ids"], [])

        response = await self.client.post(
            f"{BASE_PATH}/{self.application_id}/decision",
            json={"status": "approved", "reason": "SLMC details manually checked"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["status"], "approved")
        self.assertEqual(response.json()["reviewed_by"], str(self.reviewer.id))
        self.assertEqual(response.json()["review_reason"], "SLMC details manually checked")

        persisted_application = await self.session.get(VerificationApplication, self.application_id)
        persisted_profile = await self.session.get(ProfessionalProfile, self.applicant.id)
        decision = await self.session.scalar(
            select(VerificationDecision).where(
                VerificationDecision.application_id == self.application_id
            )
        )
        role_assignment = await self.session.get(
            UserRole, (self.applicant.id, self.roles["doctor"])
        )
        audit_event = await self.session.scalar(
            select(AuditEvent).where(
                AuditEvent.target_id == self.application_id,
                AuditEvent.action == "verification_application.decided",
            )
        )
        self.assertEqual(persisted_application.status, "approved")
        self.assertEqual(persisted_profile.verification_status, "approved")
        self.assertEqual(decision.reviewer_user_id, self.reviewer.id)
        self.assertEqual(decision.reason, "SLMC details manually checked")
        self.assertEqual(decision.decided_at, persisted_application.reviewed_at)
        self.assertEqual(role_assignment.assigned_by, self.reviewer.id)
        self.assertIsNone(role_assignment.revoked_at)
        self.assertEqual(audit_event.actor_user_id, self.reviewer.id)
        self.assertNotIn("SLMC details manually checked", str(audit_event.metadata_))

        response = await self.client.get(f"{BASE_PATH}/{self.application_id}/decisions")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["total"], 1)
        self.assertEqual(response.json()["items"][0]["reviewer_user_id"], str(self.reviewer.id))

    async def test_pending_applicant_submits_and_reads_own_evidence_without_role(self):
        await self.session.delete(self.application)
        await self.session.flush()
        evidence = Document(
            owner_user_id=self.applicant.id,
            uploaded_by=self.applicant.id,
            patient_user_id=None,
            source=RecordSource.DOCTOR,
            document_type="verification_evidence",
            original_filename="registration.pdf",
            storage_key=f"test/{uuid4()}",
            media_type="application/pdf",
            byte_size=1,
        )
        self.session.add(evidence)
        await self.session.flush()
        self.actor = self.applicant

        response = await self.client.post(
            "/api/v1/verification-applications",
            json={
                "role": "doctor",
                "registration_number": "SLMC-ML48-PENDING",
                "evidence_document_ids": [str(evidence.id)],
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["status"], "pending")
        self.assertEqual(response.json()["evidence_document_ids"], [str(evidence.id)])
        self.assertIsNone(
            await self.session.scalar(
                select(UserRole).where(
                    UserRole.user_id == self.applicant.id,
                    UserRole.role_id == self.roles["doctor"],
                    UserRole.revoked_at.is_(None),
                )
            )
        )

        listed = await self.client.get("/api/v1/me/verification-applications")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json()["items"][0]["evidence_document_ids"], [str(evidence.id)])

        metadata = await self.client.get(f"/api/v1/documents/{evidence.id}")
        self.assertEqual(metadata.status_code, 200, metadata.text)
        self.assertEqual(metadata.json()["original_filename"], "registration.pdf")

        organization = Organization(
            name=f"ML-50 pending applicant lab {uuid4()}",
            type="laboratory",
            status="active",
        )
        self.session.add(organization)
        await self.session.flush()
        membership = await self.client.post(
            "/api/v1/me/memberships",
            json={"organization_id": str(organization.id), "role": "doctor"},
        )
        self.assertEqual(membership.status_code, 201, membership.text)
        self.assertEqual(membership.json()["status"], "pending")
        self.assertIsNone(
            await self.session.scalar(
                select(UserRole).where(
                    UserRole.user_id == self.applicant.id,
                    UserRole.role_id == self.roles["doctor"],
                    UserRole.revoked_at.is_(None),
                )
            )
        )

        # The applicant has no patient or professional role, so a protected
        # clinical workflow must remain unavailable while verification is pending.
        denied_clinical_access = await self.client.post(
            "/api/v1/lab-upload-requests",
            json={"laboratory_id": str(organization.id)},
        )
        self.assertEqual(
            denied_clinical_access.status_code, 403, denied_clinical_access.text
        )
        self.assertEqual(denied_clinical_access.json()["code"], "FORBIDDEN")

        other_user = await self._create_user("ML48 unrelated applicant")
        self.actor = other_user
        denied = await self.client.get(f"/api/v1/documents/{evidence.id}")
        self.assertEqual(denied.status_code, 404, denied.text)

    async def test_rejected_applicant_remains_without_role_and_cannot_use_clinical_flow(self):
        response = await self.client.post(
            f"{BASE_PATH}/{self.application_id}/decision",
            json={"status": "rejected", "reason": "Synthetic evidence mismatch"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["status"], "rejected")

        await self.session.refresh(self.profile)
        self.assertEqual(self.profile.verification_status, "rejected")
        self.assertIsNone(
            await self.session.scalar(
                select(UserRole).where(
                    UserRole.user_id == self.applicant_id,
                    UserRole.role_id == self.roles["doctor"],
                    UserRole.revoked_at.is_(None),
                )
            )
        )

        organization = Organization(
            name=f"ML-50 rejected applicant lab {uuid4()}",
            type="laboratory",
            status="active",
        )
        self.session.add(organization)
        await self.session.flush()
        self.actor = self.applicant
        denied = await self.client.post(
            "/api/v1/lab-upload-requests",
            json={"laboratory_id": str(organization.id)},
        )
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertEqual(denied.json()["code"], "FORBIDDEN")

    async def test_non_admin_and_self_reviewer_are_denied(self):
        self.actor = self.applicant
        response = await self.client.get(BASE_PATH)
        self.assertEqual(response.status_code, 403, response.text)

        self.actor = self.reviewer
        self.application.user_id = self.reviewer.id
        await self.session.flush()
        response = await self.client.post(
            f"{BASE_PATH}/{self.application_id}/decision",
            json={"status": "approved", "reason": "Should be denied"},
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(
            await self.session.scalar(
                select(VerificationDecision.id).where(
                    VerificationDecision.application_id == self.application_id
                )
            ),
            None,
        )

    async def test_invalid_and_already_decided_requests_do_not_write_history(self):
        response = await self.client.get(BASE_PATH, params={"unknown_filter": "secret"})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertNotIn("unknown_filter", response.text)

        response = await self.client.post(
            f"{BASE_PATH}/{self.application_id}/decision",
            json={"status": "invalid", "reason": "Decision"},
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.application.status = "approved"
        await self.session.flush()
        response = await self.client.post(
            f"{BASE_PATH}/{self.application_id}/decision",
            json={"status": "rejected", "reason": "Second decision"},
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(
            await self.session.scalar(
                select(VerificationDecision.id).where(
                    VerificationDecision.application_id == self.application_id
                )
            ),
            None,
        )

    async def test_registration_number_mismatch_cannot_be_approved(self):
        self.profile.registration_number = "SLMC-ML48-UPDATED"
        await self.session.flush()

        response = await self.client.post(
            f"{BASE_PATH}/{self.application_id}/decision",
            json={"status": "approved", "reason": "Manually checked"},
        )

        self.assertEqual(response.status_code, 409, response.text)
        await self.session.refresh(self.application)
        await self.session.refresh(self.profile)
        self.assertEqual(self.application.status, "pending")
        self.assertEqual(self.profile.verification_status, "pending")
        self.assertIsNone(
            await self.session.scalar(
                select(UserRole).where(
                    UserRole.user_id == self.applicant_id,
                    UserRole.role_id == self.roles["doctor"],
                )
            )
        )
        self.assertIsNone(
            await self.session.scalar(
                select(VerificationDecision.id).where(
                    VerificationDecision.application_id == self.application_id
                )
            )
        )
        self.assertIsNone(
            await self.session.scalar(
                select(AuditEvent.id).where(
                    AuditEvent.target_id == self.application_id,
                    AuditEvent.action == "verification_application.decided",
                )
            )
        )

    async def test_audit_failure_rolls_back_application_and_history(self):
        request = SimpleNamespace(state=SimpleNamespace(request_id="ml48-pg-audit-failure"))
        with patch.object(
            service,
            "append_event",
            new_callable=AsyncMock,
            side_effect=RuntimeError("synthetic audit failure"),
        ):
            with self.assertRaises(RuntimeError):
                await service.decide_application(
                    self.session,
                    self.reviewer,
                    request,
                    self.application_id,
                    service.VerificationDecisionInput(
                        status="rejected", reason="Synthetic rejection"
                    ),
                )

        await self.session.refresh(self.application)
        self.assertEqual(self.application.status, "pending")
        self.assertEqual(
            await self.session.scalar(
                select(VerificationDecision.id).where(
                    VerificationDecision.application_id == self.application_id
                )
            ),
            None,
        )
