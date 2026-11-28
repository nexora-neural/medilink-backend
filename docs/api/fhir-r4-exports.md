# Limited FHIR R4 exports

The backend exposes two read-only individual-resource exports using FHIR R4 (4.0.1). Successful responses use `application/fhir+json`. Authentication uses a Firebase bearer token, followed by current local account, role, verification, ownership, and grant checks. Both routes use the authenticated account request limit (120 requests per minute by default) and return `Cache-Control: no-store`.

This implementation does not provide FHIR search, create/update, Bundles, SMART authentication, Practitioner export, or full FHIR server conformance. `MedicationRequest.requester` is omitted because this API does not export a Practitioner reference.

## Patient

```http
GET /fhir/r4/Patient/{patient_id}
Authorization: Bearer <Firebase ID token>
Accept: application/fhir+json
```

A patient can export their own record. A doctor must have an approved doctor role and a current `history_allergies` grant for that patient. Another patient's record, a missing grant, or an unusable organization membership is concealed with `404 RESOURCE_NOT_FOUND`; an inactive account or unverified doctor receives `403 ACCESS_DENIED`.

The response contains only `resourceType`, `id`, `identifier`, `name`, and the optional `birthDate`. `id` is the internal patient UUID. The identifier uses system `urn:medilink:patient` and the UUID as its value. `name[0].text` is the authorized account display name. Email, phone, sex, emergency contacts, and Firebase identifiers are not exported.

## MedicationRequest

```http
GET /fhir/r4/MedicationRequest/{item_id}
Authorization: Bearer <Firebase ID token>
Accept: application/fhir+json
```

A patient can export only an item belonging to them. A verified doctor needs a current `prescriptions` grant for the item's patient. Patient-source and approved-external-system items return `404 RESOURCE_NOT_FOUND`, including patient-source prescriptions with an approved review outcome. The source field is authoritative; review state never converts a patient-source prescription into a doctor-issued prescription.

Each doctor-issued prescription item is a separate `MedicationRequest`. The item UUID is its `id`; `subject.reference` is `Patient/{patient_uuid}`; `medicationCodeableConcept.text` is the stored medicine-name snapshot; `intent` is `order`; `authoredOn` is the prescription `issued_at`; and one `dosageInstruction.text` joins only the available dose, route, frequency, duration, and instruction values. No coded dose or other structured clinical value is inferred.

Prescription statuses map as follows:

| MediLink prescription status | FHIR R4 status |
|---|---|
| `active` | `active` |
| `completed` | `completed` |
| `discontinued` | `stopped` |
| `cancelled` | `cancelled` |
| `superseded` | `cancelled` |
| `draft` or missing `issued_at` | Rejected with `409 INVALID_STATE` |

The current prescription model has no `entered-in-error` status. A status outside the mappings above is rejected with `409 INVALID_STATE`; no additional mapping is inferred.

## FHIR R4 structure verification

Representative synthetic resources are stored in `tests/fixtures/fhir-r4/`. To validate them with the official HL7 FHIR Validator CLI, provide the local jar path and run:

```sh
FHIR_VALIDATOR_JAR=/path/to/validator_cli.jar FHIR_VALIDATOR_HOME=/tmp/fhir-validator-home \
  ./.venv/bin/python -m unittest tests.test_fhir_r4_validator -v
```

The test pins the validation version to FHIR R4 `4.0.1` and disables the remote terminology service (`-tx n/a`), so the official HL7 CLI validates the core resource structures without checking external terminology bindings. This does not claim full server or implementation-guide conformance.

## Errors and audit

Errors use the normal safe MediLink JSON shape:

```json
{
  "code": "RESOURCE_NOT_FOUND",
  "message": "The requested resource is not available.",
  "field_errors": {},
  "request_id": "request-id"
}
```

Missing/invalid identity returns `401`; an inactive account or unverified doctor returns `403`; absent, concealed, patient-source, or unauthorized resources and unusable organization membership return `404`; an unissued or otherwise unmappable prescription returns `409`; malformed UUIDs return `422`; rate limiting returns `429` with `Retry-After`; and rate-limit store unavailability returns `503`. Responses, including errors, do not include clinical payloads, tokens, or stack traces.

The existing append-only API request audit records each FHIR route template, HTTP method, status, request ID, and authenticated actor ID. It does not store the resource ID or FHIR response body.
