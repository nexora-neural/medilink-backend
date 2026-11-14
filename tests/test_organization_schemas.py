"""Contract checks for the organization and membership API schemas."""

import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.database.session import get_session
from app.main import app
from app.modules.audit.schemas import SAFE_METADATA_KEYS
from app.modules.identity.auth import get_current_user
from app.modules.identity.models import AccountSupportDecisionReason
from app.modules.organizations import service as organization_service
from app.modules.organizations.router import router
from app.modules.organizations.schemas import (
    MembershipDecision,
    MembershipInput,
    OrganizationDecision,
    OrganizationInput,
)


class OrganizationSchemaTests(unittest.TestCase):
    def test_decision_reasons_use_separate_protected_storage_model(self):
        self.assertEqual(
            AccountSupportDecisionReason.__table__.c.audit_event_id.foreign_keys.pop().target_fullname,
            "audit_events.id",
        )
        self.assertNotIn("reason", SAFE_METADATA_KEYS)

    def test_organization_input_trims_in_service_and_rejects_invalid_type(self):
        payload = OrganizationInput(name="  Clinic  ", type="clinic")
        self.assertEqual(payload.name, "Clinic")
        with self.assertRaises(ValidationError):
            OrganizationInput(name="   ", type="clinic")
        with self.assertRaises(ValidationError):
            OrganizationInput(name="Clinic", type="unapproved")

    def test_organization_registration_cannot_set_status(self):
        payload = OrganizationInput(name="Clinic", type="clinic", status="active")
        self.assertNotIn("status", payload.model_dump())

    def test_membership_and_decision_values_follow_contract(self):
        payload = MembershipInput(organization_id=uuid4(), role="lab_staff")
        self.assertEqual(payload.role, "lab_staff")
        self.assertEqual(OrganizationDecision(status="active", reason="Verified").status, "active")
        self.assertEqual(
            OrganizationDecision(status="active", reason="  Verified ").reason, "Verified"
        )
        self.assertEqual(
            MembershipDecision(status="approved", reason="  Checked ").reason, "Checked"
        )
        self.assertEqual(
            MembershipDecision(status="more_info_requested", reason="Need proof").status,
            "more_info_requested",
        )
        for decision, status in (
            (OrganizationDecision, "active"),
            (MembershipDecision, "approved"),
        ):
            with self.subTest(decision=decision.__name__), self.assertRaises(ValidationError):
                decision(status=status, reason="   ")
        with self.assertRaises(ValidationError):
            MembershipInput(organization_id=uuid4(), role="admin")

    def test_all_eight_contract_routes_are_registered(self):
        paths = {(route.path, tuple(sorted(route.methods or []))) for route in router.routes}
        expected = {
            ("/organizations", ("GET",)),
            ("/organizations", ("POST",)),
            ("/me/memberships", ("POST",)),
            ("/admin/organizations", ("GET",)),
            ("/admin/organizations/{organization_id}", ("GET",)),
            ("/admin/memberships", ("GET",)),
            ("/admin/organizations/{organization_id}/decision", ("POST",)),
            ("/admin/memberships/{membership_id}/decision", ("POST",)),
        }
        self.assertTrue(expected.issubset(paths))


class OrganizationPopulatedListRouteTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.actor = SimpleNamespace(id=uuid4(), status="active")

        async def session_override():
            yield object()

        async def actor_override():
            return self.actor

        app.dependency_overrides[get_session] = session_override
        app.dependency_overrides[get_current_user] = actor_override
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        app.dependency_overrides.clear()

    async def test_public_organization_list_serializes_populated_orm_records(self):
        organization = SimpleNamespace(id=uuid4(), name="Central Clinic", type="clinic")
        with patch(
            "app.modules.organizations.service.repository.list_organizations",
            new=AsyncMock(return_value=([organization], 1)),
        ):
            response = await self.client.get("/api/v1/organizations")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["total"], 1)
        self.assertEqual(
            response.json()["items"],
            [{"id": str(organization.id), "name": "Central Clinic", "type": "clinic"}],
        )

    async def test_admin_organization_detail_returns_the_requested_record(self):
        organization = SimpleNamespace(
            id=uuid4(),
            name="Central Clinic",
            type="clinic",
            registration_ref="REG-123",
            status="active",
            created_at="2026-11-01T10:00:00Z",
            updated_at="2026-11-02T10:00:00Z",
        )
        with patch(
            "app.modules.organizations.service.admin_get_organization",
            new=AsyncMock(return_value=organization),
        ) as get_organization:
            response = await self.client.get(
                f"/api/v1/admin/organizations/{organization.id}"
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["id"], str(organization.id))
        self.assertEqual(response.json()["status"], "active")
        get_organization.assert_awaited_once()

    async def test_admin_organization_detail_denies_non_super_admin(self):
        organization_id = uuid4()
        with (
            patch(
                "app.modules.organizations.service._has_role",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "app.modules.organizations.service.repository.get_organization",
                new=AsyncMock(),
            ) as get_organization,
        ):
            response = await self.client.get(f"/api/v1/admin/organizations/{organization_id}")

        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["code"], "ACCESS_DENIED")
        get_organization.assert_not_awaited()

    async def test_admin_organization_detail_returns_not_found_for_unknown_id(self):
        organization_id = uuid4()
        with (
            patch(
                "app.modules.organizations.service._has_role",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "app.modules.organizations.service.repository.get_organization",
                new=AsyncMock(return_value=None),
            ) as get_organization,
        ):
            response = await self.client.get(f"/api/v1/admin/organizations/{organization_id}")

        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json()["code"], "RESOURCE_NOT_FOUND")
        get_organization.assert_awaited_once()


class OrganizationDecisionAuditTests(IsolatedAsyncioTestCase):
    class DecisionSession:
        def __init__(self):
            self.added = []
            self.committed = False

        def add(self, value):
            self.added.append(value)

        async def flush(self):
            return None

        async def refresh(self, value):
            return None

        async def commit(self):
            self.committed = True

        async def rollback(self):
            return None

    async def test_organization_decision_status_and_reason_link_to_same_audit_event(self):
        audit_event_id = uuid4()
        organization = SimpleNamespace(
            id=uuid4(),
            name="Central Clinic",
            type="clinic",
            registration_ref=None,
            status="pending",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session = self.DecisionSession()
        request = SimpleNamespace(state=SimpleNamespace(request_id="req-organization-decision"))
        actor = SimpleNamespace(id=uuid4(), status="active")
        payload = OrganizationDecision(status="active", reason="Registration documents verified.")

        with (
            patch.object(organization_service, "_require_super_admin", new=AsyncMock()),
            patch.object(
                organization_service.repository,
                "get_organization",
                new=AsyncMock(return_value=organization),
            ),
            patch.object(
                organization_service,
                "append_event",
                new=AsyncMock(return_value=SimpleNamespace(id=audit_event_id)),
            ) as append_event,
        ):
            result = await organization_service.decide_organization(
                session, actor, request, organization.id, payload
            )

        self.assertEqual(result.status, "active")
        self.assertTrue(session.committed)
        self.assertEqual(append_event.await_args.kwargs["metadata"], {"status": "active"})
        self.assertNotIn("reason", append_event.await_args.kwargs["metadata"])
        stored_reasons = [
            item for item in session.added if isinstance(item, AccountSupportDecisionReason)
        ]
        self.assertEqual(len(stored_reasons), 1)
        self.assertEqual(stored_reasons[0].audit_event_id, audit_event_id)
        self.assertEqual(stored_reasons[0].reason, payload.reason)

    async def test_membership_decision_status_and_reason_link_to_same_audit_event(self):
        audit_event_id = uuid4()
        membership = SimpleNamespace(
            id=uuid4(),
            organization_id=uuid4(),
            user_id=uuid4(),
            role="doctor",
            status="pending",
            verified_at=None,
            created_at=datetime.now(UTC),
        )
        session = self.DecisionSession()
        request = SimpleNamespace(state=SimpleNamespace(request_id="req-membership-decision"))
        actor = SimpleNamespace(id=uuid4(), status="active")
        payload = MembershipDecision(status="approved", reason="Professional details verified.")

        with (
            patch.object(organization_service, "_require_super_admin", new=AsyncMock()),
            patch.object(
                organization_service.repository,
                "get_membership",
                new=AsyncMock(return_value=membership),
            ),
            patch.object(
                organization_service,
                "append_event",
                new=AsyncMock(return_value=SimpleNamespace(id=audit_event_id)),
            ) as append_event,
        ):
            result = await organization_service.decide_membership(
                session, actor, request, membership.id, payload
            )

        self.assertEqual(result.status, "approved")
        self.assertTrue(session.committed)
        self.assertEqual(append_event.await_args.kwargs["metadata"], {"status": "approved"})
        self.assertNotIn("reason", append_event.await_args.kwargs["metadata"])
        stored_reasons = [
            item for item in session.added if isinstance(item, AccountSupportDecisionReason)
        ]
        self.assertEqual(len(stored_reasons), 1)
        self.assertEqual(stored_reasons[0].audit_event_id, audit_event_id)
        self.assertEqual(stored_reasons[0].reason, payload.reason)


if __name__ == "__main__":
    unittest.main()
