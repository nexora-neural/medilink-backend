"""Contract and mapping tests for the limited FHIR R4 exports."""

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import Request
from fastapi.testclient import TestClient

from app.core.rate_limits import RateLimitExceeded
from app.database.session import get_session
from app.main import app
from app.modules.fhir import service
from app.modules.fhir.schemas import FhirPatient
from app.modules.fhir.service import FhirExportError
from app.modules.identity.auth import AuthenticationError, get_current_user
from app.modules.identity.request_limits import require_authenticated_rate_limit


class FhirExportContractTests(TestCase):
    def setUp(self):
        self.actor = SimpleNamespace(id=uuid4(), status="active")

        async def current_user(request: Request):
            request.state.audit_actor_user_id = self.actor.id
            return self.actor

        overrides = patch.dict(
            app.dependency_overrides,
            {
                get_current_user: current_user,
                require_authenticated_rate_limit: lambda: None,
                get_session: lambda: None,
            },
        )
        overrides.start()
        self.addCleanup(overrides.stop)
        self.client = TestClient(app, raise_server_exceptions=False)

    def test_paths_media_types_security_and_safe_error_contract(self):
        schema = app.openapi()
        patient = schema["paths"]["/fhir/r4/Patient/{patient_id}"]["get"]
        medication = schema["paths"]["/fhir/r4/MedicationRequest/{item_id}"]["get"]

        self.assertEqual(patient["operationId"], "medilink_123")
        self.assertEqual(medication["operationId"], "medilink_124")
        self.assertNotIn("/api/v1/fhir/r4/Patient/{patient_id}", schema["paths"])
        self.assertEqual(
            patient["responses"]["200"]["content"]["application/fhir+json"]["schema"]["$ref"],
            "#/components/schemas/FhirPatient",
        )
        self.assertEqual(
            medication["responses"]["200"]["content"]["application/fhir+json"]["schema"]["$ref"],
            "#/components/schemas/FhirMedicationRequest",
        )
        error_schema = patient["responses"]["404"]["content"]["application/json"]["schema"]
        self.assertEqual(error_schema["properties"]["code"]["type"], "string")
        self.assertNotIn("application/fhir+json", patient["responses"]["404"]["content"])
        self.assertEqual(patient["security"], [{"FirebaseBearer": []}])
        self.assertEqual(
            schema["components"]["securitySchemes"]["FirebaseBearer"]["scheme"], "bearer"
        )

    def test_patient_response_uses_fhir_media_type_and_disables_caching(self):
        expected = FhirPatient(
            resourceType="Patient",
            id=str(self.actor.id),
            identifier=[{"system": "urn:medilink:patient", "value": str(self.actor.id)}],
            name=[{"text": "Synthetic patient"}],
            birthDate=date(1990, 1, 2),
        )
        with patch.object(service, "export_patient", new_callable=AsyncMock, return_value=expected):
            response = self.client.get(f"/fhir/r4/Patient/{self.actor.id}")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.headers["content-type"].startswith("application/fhir+json"))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.json()["resourceType"], "Patient")
        self.assertEqual(response.json()["identifier"][0]["system"], "urn:medilink:patient")
        self.assertNotIn("email", response.json())

    def test_fhir_error_uses_safe_json_error_shape(self):
        with patch.object(
            service,
            "export_patient",
            new_callable=AsyncMock,
            side_effect=FhirExportError(
                404, "RESOURCE_NOT_FOUND", "The requested resource is not available."
            ),
        ):
            response = self.client.get(f"/fhir/r4/Patient/{uuid4()}")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.headers["content-type"], "application/json")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(set(response.json()), {"code", "message", "field_errors", "request_id"})
        self.assertNotIn("detail", response.json())

    def test_invalid_uuid_and_authentication_errors_are_safe(self):
        invalid = self.client.get("/fhir/r4/Patient/not-a-uuid")
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(invalid.json()["code"], "VALIDATION_ERROR")
        self.assertNotIn("not-a-uuid", invalid.text)

        async def no_identity():
            raise AuthenticationError(
                status_code=401,
                detail={
                    "code": "INVALID_TOKEN",
                    "message": "The authentication token is invalid.",
                    "field_errors": {},
                    "request_id": "test-request",
                },
            )

        app.dependency_overrides[get_current_user] = no_identity
        denied = self.client.get(f"/fhir/r4/Patient/{uuid4()}")
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(denied.json()["code"], "INVALID_TOKEN")
        self.assertNotIn("detail", denied.json())

    def test_authenticated_rate_limit_dependency_returns_contract_response(self):
        async def limited():
            raise RateLimitExceeded(7)

        app.dependency_overrides[require_authenticated_rate_limit] = limited
        response = self.client.get(f"/fhir/r4/Patient/{uuid4()}")

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["Retry-After"], "7")
        self.assertEqual(response.json()["code"], "RATE_LIMITED")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_top_level_fhir_request_is_included_in_api_audit(self):
        expected = FhirPatient(
            resourceType="Patient",
            id=str(self.actor.id),
            identifier=[{"system": "urn:medilink:patient", "value": str(self.actor.id)}],
            name=[{"text": "Synthetic patient"}],
        )
        with (
            patch.object(service, "export_patient", new_callable=AsyncMock, return_value=expected),
            patch("app.main.record_api_request", new_callable=AsyncMock) as audit,
        ):
            self.client.get(f"/fhir/r4/Patient/{self.actor.id}")

        audit.assert_awaited_once()
        request, response = audit.await_args.args
        self.assertEqual(request.url.path, "/fhir/r4/Patient/" + str(self.actor.id))
        self.assertEqual(response.status_code, 200)


class FhirExportMappingTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.patient_id = uuid4()
        self.patient_actor = SimpleNamespace(id=self.patient_id, status="active")
        self.doctor_id = uuid4()
        self.doctor_actor = SimpleNamespace(id=self.doctor_id, status="active")
        self.patient = SimpleNamespace(id=self.patient_id, display_name="  Synthetic Patient  ")
        self.profile = SimpleNamespace(date_of_birth=date(1988, 3, 4))

    async def test_patient_mapping_includes_only_contract_identity_fields(self):
        with (
            patch.object(
                service.repository,
                "get_active_role_codes",
                new_callable=AsyncMock,
                return_value={"patient"},
            ),
            patch.object(
                service.repository,
                "get_patient",
                new_callable=AsyncMock,
                return_value=(self.patient, self.profile),
            ),
        ):
            resource = await service.export_patient(
                session=object(), user=self.patient_actor, patient_id=self.patient_id
            )

        self.assertEqual(resource.resourceType, "Patient")
        self.assertEqual(resource.id, str(self.patient_id))
        self.assertEqual(resource.identifier[0].system, "urn:medilink:patient")
        self.assertEqual(resource.identifier[0].value, str(self.patient_id))
        self.assertEqual(resource.name[0].text, "Synthetic Patient")
        self.assertEqual(resource.birthDate, date(1988, 3, 4))
        self.assertEqual(
            set(resource.model_dump()), {"resourceType", "id", "identifier", "name", "birthDate"}
        )

    async def test_doctor_patient_export_requires_verified_current_correct_category_grant(self):
        with (
            patch.object(
                service.repository,
                "get_active_role_codes",
                new_callable=AsyncMock,
                return_value={"doctor"},
            ),
            patch.object(
                service.repository, "is_verified_doctor", new_callable=AsyncMock, return_value=True
            ),
            patch.object(
                service.repository,
                "get_current_grant_organizations",
                new_callable=AsyncMock,
                return_value=[None],
            ) as grants,
            patch.object(service, "require_doctor_grant", new_callable=AsyncMock) as guard,
            patch.object(
                service.repository,
                "get_patient",
                new_callable=AsyncMock,
                return_value=(self.patient, self.profile),
            ),
        ):
            resource = await service.export_patient(
                session=object(), user=self.doctor_actor, patient_id=self.patient_id
            )

        self.assertEqual(resource.resourceType, "Patient")
        self.assertEqual(grants.await_args.kwargs["category"], "history_allergies")
        self.assertEqual(guard.await_args.args[4], "history_allergies")

    async def test_unverified_doctor_and_missing_or_wrong_category_grant_are_denied(self):
        with (
            patch.object(
                service.repository,
                "get_active_role_codes",
                new_callable=AsyncMock,
                return_value={"doctor"},
            ),
            patch.object(
                service.repository, "is_verified_doctor", new_callable=AsyncMock, return_value=False
            ),
        ):
            with self.assertRaises(FhirExportError) as unverified:
                await service.export_patient(
                    session=object(), user=self.doctor_actor, patient_id=self.patient_id
                )
        self.assertEqual(unverified.exception.status_code, 403)

        with (
            patch.object(
                service.repository,
                "get_active_role_codes",
                new_callable=AsyncMock,
                return_value={"doctor"},
            ),
            patch.object(
                service.repository, "is_verified_doctor", new_callable=AsyncMock, return_value=True
            ),
            patch.object(
                service.repository,
                "get_current_grant_organizations",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            with self.assertRaises(FhirExportError) as missing_grant:
                await service.export_patient(
                    session=object(), user=self.doctor_actor, patient_id=self.patient_id
                )
        self.assertEqual(missing_grant.exception.status_code, 404)

    async def test_cross_patient_patient_role_is_concealed(self):
        with patch.object(
            service.repository,
            "get_active_role_codes",
            new_callable=AsyncMock,
            return_value={"patient"},
        ):
            with self.assertRaises(FhirExportError) as denied:
                await service.export_patient(
                    session=object(), user=self.patient_actor, patient_id=uuid4()
                )
        self.assertEqual(denied.exception.status_code, 404)

    async def test_doctor_issued_item_mapping_and_all_domain_statuses(self):
        item = SimpleNamespace(
            id=uuid4(),
            medicine_name_snapshot="  Synthetic medicine  ",
            dose_text="1 tablet",
            route="oral",
            frequency="twice daily",
            duration_days=5,
            instructions="after meals",
        )
        status_map = {
            "active": "active",
            "completed": "completed",
            "discontinued": "stopped",
            "cancelled": "cancelled",
            "superseded": "cancelled",
        }
        for domain_status, fhir_status in status_map.items():
            prescription = SimpleNamespace(
                source="doctor",
                patient_user_id=self.patient_id,
                status=domain_status,
                issued_at=datetime(2026, 11, 28, 8, 30, tzinfo=UTC),
            )
            with (
                patch.object(
                    service.repository,
                    "get_medication_item",
                    new_callable=AsyncMock,
                    return_value=(item, prescription, self.patient, self.profile),
                ),
                patch.object(
                    service.repository,
                    "get_active_role_codes",
                    new_callable=AsyncMock,
                    return_value={"patient"},
                ),
            ):
                resource = await service.export_medication_request(
                    session=object(), user=self.patient_actor, item_id=item.id
                )
            with self.subTest(domain_status=domain_status):
                self.assertEqual(resource.status, fhir_status)
                self.assertEqual(resource.id, str(item.id))
                self.assertEqual(resource.intent, "order")
                self.assertEqual(resource.subject.reference, f"Patient/{self.patient_id}")
                self.assertEqual(resource.medicationCodeableConcept.text, "Synthetic medicine")
                self.assertEqual(resource.authoredOn, prescription.issued_at)
                self.assertEqual(
                    resource.dosageInstruction[0].text,
                    "Dose: 1 tablet; Route: oral; Frequency: twice daily; "
                    "Duration: 5 days; Instructions: after meals",
                )
                self.assertNotIn("requester", resource.model_dump())

    async def test_reviewed_patient_source_is_hidden_and_draft_is_rejected(self):
        item = SimpleNamespace(
            id=uuid4(), medicine_name_snapshot="Synthetic", dose_text="1", frequency="daily"
        )
        patient_source = SimpleNamespace(
            source="patient",
            review_status="approved",
            patient_user_id=self.patient_id,
            status="active",
            issued_at=datetime(2026, 11, 28, tzinfo=UTC),
        )
        with patch.object(
            service.repository, "get_medication_item", new_callable=AsyncMock,
            return_value=(item, patient_source, self.patient, self.profile),
        ), patch.object(
            service.repository, "get_active_role_codes", new_callable=AsyncMock,
            return_value={"patient"},
        ):
            with self.assertRaises(FhirExportError) as hidden:
                await service.export_medication_request(
                    session=object(), user=self.patient_actor, item_id=item.id
                )
        self.assertEqual(hidden.exception.status_code, 404)

        external_source = SimpleNamespace(
            source="approved_external_system",
            review_status="approved",
            patient_user_id=self.patient_id,
            status="active",
            issued_at=datetime(2026, 11, 28, tzinfo=UTC),
        )
        with patch.object(
            service.repository, "get_medication_item", new_callable=AsyncMock,
            return_value=(item, external_source, self.patient, self.profile),
        ), patch.object(
            service.repository, "get_active_role_codes", new_callable=AsyncMock,
            return_value={"patient"},
        ):
            with self.assertRaises(FhirExportError) as external_hidden:
                await service.export_medication_request(
                    session=object(), user=self.patient_actor, item_id=item.id
                )
        self.assertEqual(external_hidden.exception.status_code, 404)

        draft = SimpleNamespace(
            source="doctor",
            patient_user_id=self.patient_id,
            status="draft",
            issued_at=None,
        )
        with (
            patch.object(
                service.repository,
                "get_medication_item",
                new_callable=AsyncMock,
                return_value=(item, draft, self.patient, self.profile),
            ),
            patch.object(
                service.repository,
                "get_active_role_codes",
                new_callable=AsyncMock,
                return_value={"patient"},
            ),
        ):
            with self.assertRaises(FhirExportError) as unissued:
                await service.export_medication_request(
                    session=object(), user=self.patient_actor, item_id=item.id
                )
        self.assertEqual(unissued.exception.status_code, 409)
