"""ML-104 API regression tests against migrated PostgreSQL."""

import os
from datetime import UTC, date, datetime, timedelta
from unittest import IsolatedAsyncioTestCase, skipUnless
from unittest.mock import patch
from uuid import UUID, uuid4

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.database.session import get_session
from app.main import app
from app.modules.audit.models import AuditEvent
from app.modules.identity.auth import get_verified_identity
from app.modules.identity.models import Role, User, UserRole
from app.modules.identity.request_limits import (
    require_authenticated_rate_limit,
    require_resolution_rate_limit,
)
from app.modules.laboratory.models import LabUploadRequest
from app.modules.laboratory.service import hash_token
from app.modules.organizations.models import Organization, OrganizationMembership
from app.modules.profiles.models import PatientProfile, ProfessionalProfile

TEST_DATABASE_URL = os.getenv("ML104_TEST_DATABASE_URL")
CREATE_URL = "/api/v1/lab-upload-requests"
LIST_URL = "/api/v1/me/lab-upload-requests"
RESOLVE_URL = f"{CREATE_URL}/resolve"


@skipUnless(TEST_DATABASE_URL, "Set ML104_TEST_DATABASE_URL to a migrated test database")
class LabUploadRequestPostgresTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        engine = create_async_engine(TEST_DATABASE_URL)
        self.addAsyncCleanup(engine.dispose)
        connection = await engine.connect()
        self.addAsyncCleanup(connection.close)
        transaction = await connection.begin()
        self.addAsyncCleanup(transaction.rollback)

        self.db = AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        self.addAsyncCleanup(self.db.close)

        self.lab = Organization(name="ML-104 test lab", type="laboratory", status="active")
        self.other_lab = Organization(name="ML-104 other lab", type="laboratory", status="active")
        self.db.add_all([self.lab, self.other_lab])
        await self.db.flush()

        self.patient = await self.create_user("patient")
        self.other_patient = await self.create_user("patient")
        self.staff = await self.create_user("lab_staff", self.lab)
        self.other_staff = await self.create_user("lab_staff", self.other_lab)
        await self.db.commit()
        self.actor = self.patient

        async def session_override():
            yield self.db

        async def identity_override():
            return {"uid": self.actor.firebase_uid}

        async def allow_rate_limit():
            return None

        overrides = patch.dict(
            app.dependency_overrides,
            {
                get_session: session_override,
                get_verified_identity: identity_override,
                require_authenticated_rate_limit: allow_rate_limit,
                require_resolution_rate_limit: allow_rate_limit,
            },
        )
        overrides.start()
        self.addCleanup(overrides.stop)
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)

    async def create_user(self, code, laboratory=None):
        role = await self.db.scalar(select(Role).where(Role.code == code))
        if role is None:
            role = Role(code=code, label=code)
            self.db.add(role)

        user = User(firebase_uid=str(uuid4()), display_name="ML-104 test user", status="active")
        self.db.add(user)
        await self.db.flush()
        self.db.add(UserRole(user_id=user.id, role_id=role.id))

        if laboratory is None:
            self.db.add(PatientProfile(
                user_id=user.id, date_of_birth=date(1990, 1, 1), sex="female"
            ))
        else:
            self.db.add(ProfessionalProfile(
                user_id=user.id, role="lab_staff", verification_status="approved"
            ))
            self.db.add(OrganizationMembership(
                organization_id=laboratory.id,
                user_id=user.id,
                role="lab_staff",
                status="approved",
            ))

        await self.db.flush()
        return user

    async def create_request(self):
        response = await self.client.post(
            CREATE_URL, json={"laboratory_id": str(self.lab.id)}
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        return UUID(body["request"]["id"]), body["request_token"]

    async def test_create_and_list_are_patient_scoped(self):
        request_id, token = await self.create_request()
        row = await self.db.get(LabUploadRequest, request_id)
        self.assertEqual(row.patient_user_id, self.patient.id)
        self.assertEqual(row.laboratory_id, self.lab.id)
        self.assertEqual(row.request_token_hash, hash_token(token))
        self.assertNotEqual(row.request_token_hash, token)
        self.assertEqual(row.expires_at - row.created_at, timedelta(days=7))

        response = await self.client.get(LIST_URL, params={"page": 1, "page_size": 20})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["total"], 1)
        self.assertEqual(response.json()["items"][0]["id"], str(request_id))
        self.assertNotIn("request_token", response.json()["items"][0])
        self.assertNotIn("request_token_hash", response.json()["items"][0])

        response = await self.client.get(LIST_URL, params={"unexpected": 1})
        self.assertEqual(response.status_code, 422, response.text)

        self.actor = self.other_patient
        response = await self.client.get(LIST_URL)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["items"], [])

        self.actor = self.staff
        response = await self.client.post(
            CREATE_URL, json={"laboratory_id": str(self.lab.id)}
        )
        self.assertEqual(response.status_code, 403, response.text)

    async def test_resolve_revoke_and_audit(self):
        request_id, token = await self.create_request()
        resolve_body = {"laboratory_id": str(self.lab.id), "request_token": token}
        revoke_url = f"{CREATE_URL}/{request_id}/revoke"
        revoke_body = {"reason": "ML-104 test revocation"}

        self.actor = self.other_staff
        response = await self.client.post(RESOLVE_URL, json=resolve_body)
        self.assertEqual(response.status_code, 403, response.text)

        self.actor = self.staff
        response = await self.client.post(RESOLVE_URL, json=resolve_body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["request_id"], str(request_id))
        self.assertEqual(
            set(response.json()["patient"]), {"patient_user_id", "display_name", "date_of_birth"}
        )
        self.assertEqual(response.json()["patient"]["patient_user_id"], str(self.patient.id))

        self.actor = self.other_patient
        response = await self.client.post(revoke_url, json=revoke_body)
        self.assertEqual(response.status_code, 404, response.text)

        self.actor = self.patient
        response = await self.client.post(revoke_url, json=revoke_body)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["status"], "revoked")
        row = await self.db.get(LabUploadRequest, request_id, populate_existing=True)
        self.assertEqual(row.revocation_reason, revoke_body["reason"])
        self.assertIsNone(row.used_at)

        self.actor = self.staff
        response = await self.client.post(RESOLVE_URL, json=resolve_body)
        self.assertEqual(response.status_code, 404, response.text)

        events = await self.db.scalars(
            select(AuditEvent).where(
                AuditEvent.target_type == "lab_upload_request",
                AuditEvent.target_id == request_id,
            )
        )
        self.assertCountEqual(
            [(event.action, event.actor_user_id, event.outcome) for event in events],
            [
                ("lab_upload_request.created", self.patient.id, "success"),
                ("lab_upload_request.resolved", self.staff.id, "success"),
                ("lab_upload_request.revoked", self.patient.id, "success"),
            ],
        )

    async def test_expired_request_is_listed_but_cannot_be_resolved_or_revoked(self):
        request_id, token = await self.create_request()
        row = await self.db.get(LabUploadRequest, request_id)
        row.created_at = datetime.now(UTC) - timedelta(days=8)
        row.expires_at = row.created_at + timedelta(days=7)
        await self.db.commit()

        response = await self.client.get(LIST_URL)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["items"][0]["status"], "expired")

        response = await self.client.post(
            f"{CREATE_URL}/{request_id}/revoke", json={"reason": "Expired test request"}
        )
        self.assertEqual(response.status_code, 409, response.text)

        self.actor = self.staff
        response = await self.client.post(
            RESOLVE_URL, json={"laboratory_id": str(self.lab.id), "request_token": token}
        )
        self.assertEqual(response.status_code, 404, response.text)