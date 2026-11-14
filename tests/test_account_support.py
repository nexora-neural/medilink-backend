"""ML-35 request validation and PostgreSQL account-support integration tests."""

import os
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, skipUnless
from unittest.mock import patch
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.database.session import get_session
from app.main import app
from app.modules.audit.models import AuditEvent
from app.modules.identity.auth import get_verified_identity
from app.modules.identity.models import AccountSupportDecisionReason, Role, User, UserRole
from app.modules.identity.request_limits import require_authenticated_rate_limit

TEST_DATABASE_URL = os.getenv("ML35_TEST_DATABASE_URL")
USERS = "/api/v1/admin/users"
APPOINT = "/api/v1/admin/role-assignments"
MFA = {"sign_in_second_factor": "phone"}


class AccountSupportAuthTests(IsolatedAsyncioTestCase):
    async def test_unauthenticated_search_is_rejected_without_database(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(USERS)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "AUTHENTICATION_REQUIRED")


@skipUnless(TEST_DATABASE_URL, "Set ML35_TEST_DATABASE_URL to a dedicated migrated test database")
class AccountSupportTests(IsolatedAsyncioTestCase):
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
        for code in ("patient", "admin", "super_admin"):
            role = await self.session.scalar(select(Role).where(Role.code == code))
            if role is None:
                role = Role(code=code, label=code)
                self.session.add(role)
                await self.session.flush()
            self.roles[code] = role.id

        self.patient = await self.create_user("ML35 Sample Patient", "patient")
        self.admin = await self.create_user("ML35 Sample Admin", "admin")
        self.super_admin = await self.create_user("ML35 Sample Super Admin", "super_admin")
        await self.session.commit()
        self.actor = self.admin
        self.firebase = MFA

        async def get_test_session():
            yield self.session

        async def verified_identity():
            return {"uid": self.actor.firebase_uid, "firebase": self.firebase}

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

    async def test_account_search_is_bounded_and_audited_without_raw_search_terms(self):
        query = "ML35 Sample P"
        response = await self.client.get(
            USERS, params={"q": query, "status": "active", "page_size": 1}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        page = response.json()
        self.assertEqual((page["page"], page["page_size"], page["total"]), (1, 1, 1))
        account = page["items"][0]
        self.assertEqual(account["id"], str(self.patient.id))
        self.assertNotIn("firebase_uid", account)
        self.assertNotIn("patient_profile", account)
        event = await self.event_for(response)
        self.assertEqual(event.action, "account.support.search")
        self.assertNotIn(query, str(event.metadata_))
        response = await self.client.get(USERS, params={"q": "ML35 Sample %"})
        self.assertEqual(response.json()["total"], 0)

    async def test_query_and_body_validation_are_contract_shaped(self):
        for params in (
            {"unknown_secret": "secret"},
            {"page_size": 101},
            {"status": "invalid"},
            {"q": "\0"},
        ):
            response = await self.client.get(USERS, params=params)
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json()["code"], "VALIDATION_ERROR")
            self.assertNotIn("unknown_secret", response.text)
        response = await self.client.post(
            APPOINT,
            json={"user_id": str(self.patient.id), "role": "super_admin", "reason": "test"},
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "VALIDATION_ERROR")

    async def test_admin_can_suspend_patient_but_cannot_modify_privileged_account(self):
        patient_id = self.patient.id
        data = {"status": "suspended", "reason": "Synthetic support decision"}
        response = await self.client.post(f"{USERS}/{patient_id}/status", json=data)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["status"], "suspended")
        event = await self.event_for(response)
        self.assertEqual(event.metadata_, {"previous_status": "active", "status": "suspended"})
        reason = await self.session.get(AccountSupportDecisionReason, event.id)
        self.assertEqual(reason.reason, data["reason"])
        response = await self.client.post(f"{USERS}/{patient_id}/status", json=data)
        self.assertEqual(response.status_code, 409)
        for target in (self.admin, self.super_admin):
            response = await self.client.post(f"{USERS}/{target.id}/status", json=data)
            self.assertEqual(response.status_code, 403)
            event = await self.event_for(response)
            self.assertEqual(event.outcome, "denied")
            self.assertIsNone(await self.session.get(AccountSupportDecisionReason, event.id))
        response = await self.client.post(f"{USERS}/{uuid4()}/status", json=data)
        self.assertEqual(response.status_code, 404)

    async def test_role_appointment_and_revoke_require_super_admin_and_mfa(self):
        patient_id = self.patient.id
        reason = {"reason": "Synthetic decision"}
        appointment = {"user_id": str(patient_id), "role": "admin", **reason}
        revoke = f"{USERS}/{patient_id}/admin-role/revoke"
        for path, body in ((APPOINT, appointment), (revoke, reason)):
            response = await self.client.post(path, json=body)
            self.assertEqual(response.status_code, 403)

        self.actor = self.super_admin
        self.firebase = {}
        for path, body in ((APPOINT, appointment), (revoke, reason)):
            response = await self.client.post(path, json=body)
            self.assertEqual(response.status_code, 403)
            self.assertIn("Multi-factor", response.json()["message"])

        self.firebase = MFA
        response = await self.client.post(APPOINT, json=appointment)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["status"], "active")
        response = await self.client.post(APPOINT, json=appointment)
        self.assertEqual(response.status_code, 409)

        self.actor = self.patient
        response = await self.client.get(USERS)
        self.assertEqual(response.status_code, 200)

        self.actor = self.super_admin
        response = await self.client.post(revoke, json=reason)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["status"], "revoked")
        assignment = await self.session.get(UserRole, (patient_id, self.roles["admin"]))
        self.assertIsNotNone(assignment.revoked_at)
        event = await self.event_for(response)
        self.assertEqual(event.action, "account.admin.revoked")
        self.assertEqual(event.metadata_["previous_assigned_by"], str(self.super_admin.id))

        self.actor = self.patient
        response = await self.client.get(USERS)
        self.assertEqual(response.status_code, 403)
        response = await self.client.get("/api/v1/me")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["roles"], ["patient"])

        self.actor = self.super_admin
        response = await self.client.post(revoke, json=reason)
        self.assertEqual(response.status_code, 409)
        response = await self.client.post(APPOINT, json=appointment)
        self.assertEqual(response.status_code, 201)
        assignment = await self.session.get(UserRole, (patient_id, self.roles["admin"]))
        self.assertIsNone(assignment.revoked_at)

    async def test_cannot_suspend_final_active_super_admin(self):
        self.actor = self.super_admin
        response = await self.client.post(
            f"{USERS}/{self.super_admin.id}/status",
            json={"status": "disabled", "reason": "Synthetic decision"},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "INVALID_STATE")

    async def test_audit_failure_rolls_back_account_status_and_event(self):
        patient_id = self.patient.id
        with patch(
            "app.modules.identity.service.AccountSupportDecisionReason",
            side_effect=RuntimeError("private database detail"),
        ):
            response = await self.client.post(
                f"{USERS}/{patient_id}/status",
                json={"status": "suspended", "reason": "Synthetic decision"},
            )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["code"], "INTERNAL_ERROR")
        self.assertNotIn("private database detail", response.text)
        patient = await self.session.get(User, patient_id)
        self.assertEqual(patient.status, "active")
        self.assertIsNone(await self.event_for(response))

    async def test_search_reports_current_admin_assignments(self):
        response = await self.client.get(USERS, params={"q": "ML35 Sample", "page_size": 100})
        self.assertEqual(response.status_code, 200, response.text)

        accounts = {item["id"]: item for item in response.json()["items"]}
        self.assertIs(accounts[str(self.patient.id)]["is_admin"], False)
        self.assertIs(accounts[str(self.admin.id)]["is_admin"], True)
        self.assertIs(accounts[str(self.super_admin.id)]["is_admin"], False)

    async def test_admin_state_is_refreshed_after_appointment_and_revocation(self):
        self.actor = self.super_admin
        patient_id = str(self.patient.id)
        reason = {"reason": "Synthetic role-state verification"}

        response = await self.client.post(
            APPOINT,
            json={"user_id": patient_id, "role": "admin", **reason},
        )
        self.assertEqual(response.status_code, 201, response.text)

        response = await self.client.get(USERS, params={"q": patient_id})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIs(response.json()["items"][0]["is_admin"], True)

        response = await self.client.post(
            f"{USERS}/{patient_id}/status",
            json={"status": "suspended", **reason},
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertIs(response.json()["is_admin"], True)

        response = await self.client.post(
            f"{USERS}/{patient_id}/admin-role/revoke",
            json=reason,
        )
        self.assertEqual(response.status_code, 201, response.text)

        response = await self.client.get(USERS, params={"q": patient_id})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIs(response.json()["items"][0]["is_admin"], False)

        response = await self.client.post(
            f"{USERS}/{patient_id}/status",
            json={"status": "active", **reason},
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertIs(response.json()["is_admin"], False)
