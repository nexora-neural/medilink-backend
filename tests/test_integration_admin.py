"""ML-103 integration-client administration tests against PostgreSQL."""

import hashlib
import os
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, skipUnless
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.database.session import get_session
from app.main import app
from app.modules.audit.models import AuditEvent
from app.modules.identity.auth import AuthenticationError, get_verified_identity
from app.modules.identity.models import AccountSupportDecisionReason, Role, User, UserRole
from app.modules.identity.request_limits import require_authenticated_rate_limit
from app.modules.integrations.auth import get_active_integration_client
from app.modules.integrations.models import IntegrationClient, IntegrationSubmission
from app.modules.integrations.schemas import IntegrationSubmissionRead
from app.modules.laboratory.models import LabUploadRequest, UploadRequestStatus
from app.modules.organizations.models import Organization, OrganizationMembership
from app.modules.profiles.models import PatientProfile, ProfessionalProfile
from app.modules.verification.models import VerificationApplication

TEST_DATABASE_URL = os.getenv("ML103_TEST_DATABASE_URL")
CLIENTS = "/api/v1/admin/integration-clients"
SUBMISSIONS = "/api/v1/admin/integration-submissions"


class IntegrationAdminAuthenticationTests(IsolatedAsyncioTestCase):
    async def test_missing_identity_is_rejected(self):
        session = AsyncMock()
        session_context = MagicMock()
        session_context.__aenter__ = AsyncMock(return_value=session)
        session_context.__aexit__ = AsyncMock(return_value=False)
        session_factory = MagicMock(return_value=session_context)

        with (
            patch.object(
                app.state,
                "audit_session_factory",
                session_factory,
                create=True,
            ),
            patch(
                "app.modules.audit.service.append_event",
                new_callable=AsyncMock,
            ) as append_event,
        ):
            async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://test",
            ) as client:
                response = await client.get(CLIENTS)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.json()["code"],
            "AUTHENTICATION_REQUIRED",
        )
        session_factory.assert_called_once_with()
        append_event.assert_awaited_once()
        self.assertIs(append_event.await_args.args[0], session)
        self.assertEqual(
            append_event.await_args.kwargs["action"],
            "api.request",
        )
        self.assertEqual(
            append_event.await_args.kwargs["outcome"],
            "denied",
        )
        session.commit.assert_awaited_once()
    async def test_integration_key_requires_active_unexpired_write_scope(self):
        now = datetime.now(UTC)
        client = SimpleNamespace(
            id=uuid4(),
            status="active",
            expires_at=now + timedelta(days=1),
            scopes=["reports:write"],
        )

        class FakeSession:
            async def scalar(self, statement):
                return client

        request = SimpleNamespace(state=SimpleNamespace(request_id="test-request"))
        with patch(
            "app.modules.integrations.auth.require_integration_rate_limit", return_value=None
        ):
            resolved = await get_active_integration_client(
                request, "secret-key", FakeSession()
            )
        self.assertIs(resolved, client)
        self.assertEqual(request.state.audit_actor_client_id, client.id)
        for status, expires_at, scopes in (
            ("suspended", now + timedelta(days=1), ["reports:write"]),
            ("revoked", now + timedelta(days=1), ["reports:write"]),
            ("active", now - timedelta(seconds=1), ["reports:write"]),
            ("active", now + timedelta(days=1), ["patients:read"]),
        ):
            client.status, client.expires_at, client.scopes = status, expires_at, scopes
            with self.assertRaises(HTTPException) as raised:
                await get_active_integration_client(request, "secret-key", FakeSession())
            self.assertEqual(raised.exception.status_code, 401)



@skipUnless(TEST_DATABASE_URL, "Set ML103_TEST_DATABASE_URL to a dedicated migrated test database")
class IntegrationAdminPostgresTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine(TEST_DATABASE_URL)
        self.addAsyncCleanup(self.engine.dispose)
        self.connection = await self.engine.connect()
        self.addAsyncCleanup(self.connection.close)
        self.transaction = await self.connection.begin()
        self.addAsyncCleanup(self.transaction.rollback)
        self.session = AsyncSession(
            bind=self.connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        self.addAsyncCleanup(self.session.close)
        self.roles = {}
        for code in ("admin", "super_admin", "patient"):
            role = await self.session.scalar(select(Role).where(Role.code == code))
            if role is None:
                role = Role(code=code, label=code)
                self.session.add(role)
                await self.session.flush()
            self.roles[code] = role.id
        self.super_admin = await self.create_user("ML-103 Super Admin", "super_admin")
        self.admin = await self.create_user("ML-103 Admin", "admin")
        self.actor = self.super_admin
        self.organization = Organization(
            name=f"ML-103 Laboratory {uuid4()}", type="laboratory", status="active"
        )
        self.session.add(self.organization)
        await self.session.flush()
        self.lab_staff = await self.create_user("ML-103 Approved Lab Staff", "super_admin")
        self.session.add(
            ProfessionalProfile(
                user_id=self.lab_staff.id,
                role="lab_staff",
                registration_number="ML103-STAFF-REG",
                verification_status="approved",
            )
        )
        self.session.add(
            OrganizationMembership(
                organization_id=self.organization.id,
                user_id=self.lab_staff.id,
                role="lab_staff",
                status="approved",
                verified_at=datetime.now(UTC),
            )
        )
        application = VerificationApplication(
            user_id=self.lab_staff.id,
            organization_id=self.organization.id,
            role="lab_staff",
            registration_number="ML103-REG",
            status="approved",
            reviewed_by=self.super_admin.id,
            reviewed_at=datetime.now(UTC),
        )
        self.session.add(application)
        await self.session.commit()

        async def get_test_session():
            yield self.session

        async def verified_identity():
            return {"uid": self.actor.firebase_uid}

        async def rate_limit():
            pass

        overrides = patch.dict(
            app.dependency_overrides,
            {
                get_session: get_test_session,
                get_verified_identity: verified_identity,
                require_authenticated_rate_limit: rate_limit,
            },
        )
        overrides.start()
        self.addCleanup(overrides.stop)
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)

    async def create_user(self, name, role):
        user = User(firebase_uid=str(uuid4()), display_name=name, status="active")
        self.session.add(user)
        await self.session.flush()
        self.session.add(UserRole(user_id=user.id, role_id=self.roles[role]))
        await self.session.flush()
        return SimpleNamespace(id=user.id, firebase_uid=user.firebase_uid)

    async def event_for(self, response):
        return await self.session.scalar(
            select(AuditEvent).where(AuditEvent.request_id == response.headers["X-Request-ID"])
        )

    def create_payload(self):
        return {
            "organization_id": str(self.organization.id),
            "name": "Approved Laboratory Integration",
            "scopes": ["reports:write"],
            "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
        }

    async def test_eligible_labs_returns_only_option_fields_and_validates_pagination(self):
        path = f"{CLIENTS}/eligible-laboratories"
        response = await self.client.get(path, params={"page": 1, "page_size": 1})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {
            "items": [{"id": str(self.organization.id), "name": self.organization.name}],
            "page": 1, "page_size": 1, "total": 1,
        })
        second = await self.client.get(path, params={"page": 2, "page_size": 1})
        self.assertEqual(second.json()["items"], [])
        self.assertEqual(second.json()["total"], 1)
        for params in ({"page": 0}, {"page_size": 101}, {"unexpected": "value"}):
            with self.subTest(params=params):
                response = await self.client.get(path, params=params)
                self.assertEqual(response.status_code, 422)

    async def test_eligible_labs_requires_active_super_admin(self):
        path = f"{CLIENTS}/eligible-laboratories"
        self.actor = self.admin
        response = await self.client.get(path)
        self.assertEqual(response.status_code, 403)
        self.actor = self.super_admin
        user = await self.session.get(User, self.super_admin.id)
        user.status = "suspended"
        await self.session.flush()
        response = await self.client.get(path)
        self.assertEqual(response.status_code, 403)

    async def test_selector_and_creation_share_all_lab_and_staff_eligibility_checks(self):
        path = f"{CLIENTS}/eligible-laboratories"
        staff = await self.session.get(User, self.lab_staff.id)
        membership = await self.session.scalar(select(OrganizationMembership).where(
            OrganizationMembership.user_id == self.lab_staff.id
        ))
        profile = await self.session.get(ProfessionalProfile, self.lab_staff.id)
        application = await self.session.scalar(select(VerificationApplication).where(
            VerificationApplication.user_id == self.lab_staff.id
        ))
        cases = [
            (self.organization, "status", "suspended"),
            (self.organization, "status", "pending"),
            (self.organization, "type", "clinic"),
            (staff, "status", "suspended"),
            (membership, "status", "pending"),
            (membership, "status", "suspended"),
            (membership, "role", "doctor"),
            (profile, "verification_status", "pending"),
            (profile, "role", "pharmacist"),
            (application, "status", "rejected"),
            (application, "role", "doctor"),
            (application, "organization_id", None),
        ]
        for row, field, value in cases:
            original = getattr(row, field)
            with self.subTest(model=type(row).__name__, field=field, value=value):
                setattr(row, field, value)
                await self.session.flush()
                response = await self.client.get(path)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["items"], [])
                self.assertEqual(response.json()["total"], 0)
                response = await self.client.post(CLIENTS, json=self.create_payload())
                self.assertEqual(response.status_code, 422, response.text)
                # create_client rolls back failed mutations; reload the objects before restoring.
                for persisted in (self.organization, staff, membership, profile, application):
                    await self.session.refresh(persisted)
                setattr(row, field, original)
                await self.session.flush()
        response = await self.client.get(path)
        self.assertEqual(response.json()["total"], 1)

    async def test_eligible_labs_deduplicates_multiple_approved_staff(self):
        extra = await self.create_user("ML-114 Second Approved Staff", "super_admin")
        self.session.add_all([
            ProfessionalProfile(user_id=extra.id, role="lab_staff", verification_status="approved"),
            OrganizationMembership(
                organization_id=self.organization.id, user_id=extra.id,
                role="lab_staff", status="approved", verified_at=datetime.now(UTC),
            ),
            VerificationApplication(
                organization_id=self.organization.id, user_id=extra.id,
                role="lab_staff", status="approved", registration_number="ML114-SECOND",
                reviewed_by=self.super_admin.id, reviewed_at=datetime.now(UTC),
            ),
        ])
        await self.session.flush()
        response = await self.client.get(f"{CLIENTS}/eligible-laboratories")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["total"], 1)
        self.assertEqual(len(response.json()["items"]), 1)

    async def test_create_list_rotate_suspend_and_revoke_lifecycle(self):
        response = await self.client.post(CLIENTS, json=self.create_payload())
        self.assertEqual(response.status_code, 201, response.text)
        created = response.json()
        credential = created["credential"]
        client_id = created["client"]["id"]
        self.assertNotIn("credential_hash", str(created))
        row = await self.session.get(IntegrationClient, client_id)
        self.assertEqual(row.credential_hash, hashlib.sha256(credential.encode()).hexdigest())
        event = await self.event_for(response)
        self.assertEqual(event.action, "integration_client.created")

        response = await self.client.get(CLIENTS)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("credential", str(response.json()))
        self.assertNotIn("credential_hash", str(response.json()))

        response = await self.client.post(
            f"{CLIENTS}/{client_id}/rotate",
            json={
                "expires_at": (datetime.now(UTC) + timedelta(days=60)).isoformat(),
                "reason": "Scheduled rotation",
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        rotated = response.json()["credential"]
        await self.session.refresh(row)
        self.assertNotEqual(credential, rotated)
        self.assertEqual(row.credential_hash, hashlib.sha256(rotated.encode()).hexdigest())

        response = await self.client.post(
            f"{CLIENTS}/{client_id}/suspend", json={"reason": "Temporary security hold"}
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["status"], "suspended")
        response = await self.client.post(
            f"{CLIENTS}/{client_id}/revoke", json={"reason": "Integration retired"}
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["status"], "revoked")
        self.assertIsNotNone(response.json()["revoked_at"])
        event = await self.event_for(response)
        self.assertIsNotNone(event)
        audit_reason = await self.session.get(AccountSupportDecisionReason, event.id)
        self.assertEqual(audit_reason.reason, "Integration retired")

    async def test_permissions_inputs_and_state_conflicts_are_contract_shaped(self):
        self.actor = self.admin
        response = await self.client.post(CLIENTS, json=self.create_payload())
        self.assertEqual(response.status_code, 403)
        self.actor = self.super_admin

        invalid_scope = self.create_payload()
        invalid_scope["scopes"] = ["reports:write", "patients:read"]
        response = await self.client.post(CLIENTS, json=invalid_scope)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "VALIDATION_ERROR")

        bad_expiry = self.create_payload()
        bad_expiry["expires_at"] = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        response = await self.client.post(CLIENTS, json=bad_expiry)
        self.assertEqual(response.status_code, 422)

        response = await self.client.post(CLIENTS, json=self.create_payload())
        client_id = response.json()["client"]["id"]
        response = await self.client.post(
            f"{CLIENTS}/{client_id}/revoke", json={"reason": "Retired"}
        )
        self.assertEqual(response.status_code, 201)
        response = await self.client.post(
            f"{CLIENTS}/{client_id}/suspend", json={"reason": "Cannot alter revoked"}
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "INVALID_STATE")

    async def test_rotate_cannot_reactivate_a_suspended_client(self):
        response = await self.client.post(CLIENTS, json=self.create_payload())
        client_id = response.json()["client"]["id"]
        response = await self.client.post(
            f"{CLIENTS}/{client_id}/suspend", json={"reason": "Security hold"}
        )
        self.assertEqual(response.status_code, 201)
        response = await self.client.post(
            f"{CLIENTS}/{client_id}/rotate",
            json={
                "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
                "reason": "Attempted rotation while suspended",
            },
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "INVALID_STATE")

    async def test_api_key_access_through_protected_http_endpoint(self):
        response = await self.client.post(CLIENTS, json=self.create_payload())
        self.assertEqual(response.status_code, 201, response.text)
        client_id = response.json()["client"]["id"]
        credential = response.json()["credential"]

        protected_app = FastAPI()
        protected_app.add_exception_handler(
            AuthenticationError, app.exception_handlers[AuthenticationError]
        )

        async def get_test_session():
            yield self.session

        protected_app.dependency_overrides[get_session] = get_test_session

        @protected_app.get("/protected-report-write")
        async def protected_report_write(
            integration_client=Depends(get_active_integration_client),
        ):
            return {"client_id": str(integration_client.id)}

        async with AsyncClient(
            transport=ASGITransport(app=protected_app, raise_app_exceptions=False),
            base_url="http://test",
        ) as probe:
            with patch(
                "app.modules.integrations.auth.require_integration_rate_limit",
                return_value=None,
            ):
                response = await probe.get(
                    "/protected-report-write", headers={"X-API-Key": credential}
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["client_id"], client_id)

                rotate_response = await self.client.post(
                    f"{CLIENTS}/{client_id}/rotate",
                    json={
                        "expires_at": (datetime.now(UTC) + timedelta(days=60)).isoformat(),
                        "reason": "Verify immediate credential invalidation",
                    },
                )
                self.assertEqual(rotate_response.status_code, 201, rotate_response.text)
                rotated_credential = rotate_response.json()["credential"]
                self.assertNotEqual(credential, rotated_credential)

                old_key_response = await probe.get(
                    "/protected-report-write", headers={"X-API-Key": credential}
                )
                self.assertEqual(old_key_response.status_code, 401, old_key_response.text)
                new_key_response = await probe.get(
                    "/protected-report-write", headers={"X-API-Key": rotated_credential}
                )
                self.assertEqual(new_key_response.status_code, 200, new_key_response.text)
                self.assertEqual(new_key_response.json()["client_id"], client_id)

                credential = rotated_credential
                admin_response = await self.client.post(
                    f"{CLIENTS}/{client_id}/suspend", json={"reason": "Testing suspension"}
                )
                self.assertEqual(admin_response.status_code, 201, admin_response.text)
                denied = await probe.get(
                    "/protected-report-write", headers={"X-API-Key": credential}
                )
                self.assertEqual(denied.status_code, 401)

                row = await self.session.get(IntegrationClient, client_id)
                row.status = "active"
                row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
                await self.session.commit()
                expired = await probe.get(
                    "/protected-report-write", headers={"X-API-Key": credential}
                )
                self.assertEqual(expired.status_code, 401)

                row.expires_at = datetime.now(UTC) + timedelta(days=30)
                await self.session.commit()
                admin_response = await self.client.post(
                    f"{CLIENTS}/{client_id}/revoke", json={"reason": "Testing revocation"}
                )
                self.assertEqual(admin_response.status_code, 201, admin_response.text)
                revoked = await probe.get(
                    "/protected-report-write", headers={"X-API-Key": credential}
                )
                self.assertEqual(revoked.status_code, 401)

    async def test_submission_admin_list_filters_and_does_not_expose_payloads(self):
        client = IntegrationClient(
            organization_id=self.organization.id,
            name="ML-103 Submission Client",
            credential_hash=hashlib.sha256(str(uuid4()).encode()).hexdigest(),
            scopes=["reports:write"],
            status="active",
            expires_at=datetime.now(UTC) + timedelta(days=30),
            created_by=self.super_admin.id,
        )
        self.session.add(client)
        await self.session.flush()
        patient = await self.create_user("ML-103 Submission Patient", "patient")
        self.session.add(
            PatientProfile(user_id=patient.id, date_of_birth=date(1990, 1, 1), sex="female")
        )
        await self.session.flush()
        upload_request = LabUploadRequest(
            patient_user_id=patient.id,
            laboratory_id=self.organization.id,
            request_token_hash=hashlib.sha256(str(uuid4()).encode()).hexdigest(),
            expires_at=datetime.now(UTC) + timedelta(days=7),
            status=UploadRequestStatus.ACTIVE,
        )
        self.session.add(upload_request)
        await self.session.flush()
        submission = IntegrationSubmission(
            client_id=client.id,
            upload_request_id=upload_request.id,
            external_report_id="LAB-ML103-001",
            idempotency_key="synthetic-idempotency-key",
            status="rejected",
            error_code="INVALID_REQUEST",
        )
        self.session.add(submission)
        await self.session.commit()
        report_response = IntegrationSubmissionRead.model_validate(submission)
        self.assertEqual(report_response.idempotency_key, "synthetic-idempotency-key")
        response = await self.client.get(SUBMISSIONS, params={"status": "rejected"})
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["items"][0]["error_code"], "INVALID_REQUEST")
        self.assertNotIn("patient", str(payload).lower())
        self.assertNotIn("idempotency_key", payload["items"][0])
        self.assertNotIn("synthetic-idempotency-key", str(payload))
        response = await self.client.get(SUBMISSIONS, params={"status": "unknown"})
        self.assertEqual(response.status_code, 422)
        response = await self.client.get(SUBMISSIONS, params={"unknown": "secret"})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("secret", response.text)
