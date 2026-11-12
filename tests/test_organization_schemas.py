"""Contract checks for the organization and membership API schemas."""

import unittest
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.database.session import get_session
from app.main import app
from app.modules.identity.auth import get_current_user
from app.modules.organizations.router import router
from app.modules.organizations.schemas import (
    MembershipDecision,
    MembershipInput,
    OrganizationDecision,
    OrganizationInput,
)


class OrganizationSchemaTests(unittest.TestCase):
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

    def test_all_seven_contract_routes_are_registered(self):
        paths = {(route.path, tuple(sorted(route.methods or []))) for route in router.routes}
        expected = {
            ("/organizations", ("GET",)),
            ("/organizations", ("POST",)),
            ("/me/memberships", ("POST",)),
            ("/admin/organizations", ("GET",)),
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


if __name__ == "__main__":
    unittest.main()
