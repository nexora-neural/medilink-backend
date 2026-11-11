"""Contract checks for the organization and membership API schemas."""

import unittest
from uuid import uuid4

from pydantic import ValidationError

from app.modules.organizations.router import router
from app.modules.organizations.schemas import (
    MembershipDecision,
    MembershipInput,
    OrganizationDecision,
    OrganizationInput,
)


class OrganizationSchemaTests(unittest.TestCase):
    def test_organization_input_trims_in_service_and_rejects_invalid_type(self):
        payload = OrganizationInput(name="Clinic", type="clinic")
        self.assertEqual(payload.name, "Clinic")
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
            MembershipDecision(status="more_info_requested", reason="Need proof").status,
            "more_info_requested",
        )
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


if __name__ == "__main__":
    unittest.main()
