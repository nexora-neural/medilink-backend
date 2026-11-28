# ML-151 scope audit

This records a read-only Jira scope review performed on 2026-11-28 and maps the
ML-151 acceptance criteria to the local backend implementation and verification.
No Jira or GitHub changes were made.

## Jira scope decision

ML-151 describes the work as a limited FHIR R4 export and names two contract
operations: `GET /fhir/r4/Patient/{patient_id}` and
`GET /fhir/r4/MedicationRequest/{item_id}`. Its done criteria require valid FHIR
references, one export per prescription item, exclusion of patient-source
prescriptions, documented resources/version, and contract-compliant input,
permission, status, error, history, and audit behavior.

ML-102, ML-103, and ML-104 are listed in ML-151's description as “Scope
references,” but were not among ML-151's nine linked work items in Jira at the
time of review. Their current descriptions and acceptance criteria do not define
FHIR resources or FHIR endpoints:

| Jira item | Current described scope | FHIR decision for ML-151 |
|---|---|---|
| ML-102 — Connect measurements and corrections workflow (Done) | Connect measurement screens to storage and background behavior; preserve blood-pressure components, entry time, provenance, and linked immutable corrections. | Does not specify a FHIR `Observation` export or a FHIR endpoint. No measurement export was inferred. |
| ML-103 — Super Admin integration-client management API (Done) | Manage integration clients, narrow `reports:write` scope and expiry, show credentials once and store only hashes, and revoke/suspend access. | Specifies client administration and submission operations, not FHIR API-client authentication or additional FHIR resources. No client-auth expansion was inferred. |
| ML-104 — Patient lab upload requests API (Done) | Create, list, revoke, and resolve opaque patient/lab requests with a seven-day lifetime and no directory-search or history access. | Does not specify FHIR lab resources or a FHIR export. No `DiagnosticReport`, `Observation`, or `DocumentReference` mapping was inferred. |

The three references therefore remain a Jira scope ambiguity, not confirmed
additional ML-151 requirements. If FHIR mappings or client authentication were
intended, they need explicit acceptance criteria before being added.

ML-174 is a separate integration task, “Connect limited FHIR R4 exports
workflow.” It repeats the ML-102/103/104 references, asks for a sample-client
workflow against the local stack, and is marked as blocked by ML-151; its status
was Idea during this review. That workflow is distinct from ML-151's two backend
resource endpoints. ML-151 also blocks the cross-service authorization/audit
integration task ML-215. These issue relationships do not add endpoints or
resources to ML-151's stated contract.

## Acceptance-criteria evidence

| ML-151 criterion | Local implementation | Verification |
|---|---|---|
| Register the two contract operations at the exact paths. | FHIR router is mounted at `/fhir/r4`, outside `/api/v1`. | Contract tests assert both routes, operation IDs, and absence of `/api/v1/fhir/...`. |
| Return the supported FHIR R4 resources and media type. | Patient identity export and one MedicationRequest per doctor-issued prescription item; success responses use `application/fhir+json`. | Unit and PostgreSQL HTTP tests check response media types, fields, distinct items, and item-to-patient references. |
| Keep FHIR references valid and exclude patient-source prescriptions. | MedicationRequest `subject.reference` points to the exported Patient; source is checked independently from review status. | PostgreSQL workflow checks references; tests verify reviewed patient-source and external-system items are hidden. Synthetic fixtures were validated with the HL7 FHIR Validator CLI against FHIR R4 4.0.1, with remote terminology validation disabled. |
| Document supported resources and version. | API guide and reference API/OpenAPI contract specify R4 4.0.1, mappings, supported fields, and exclusions. | Runtime OpenAPI paths, response codes, and FHIR schemas were compared with the checked-in OpenAPI contract. |
| Enforce ownership, verified-doctor authorization, and current category grants. | Patient self-access and current doctor role, verification, category grant, and organization checks use existing authorization mechanisms. | PostgreSQL tests cover self/cross-patient access, verified/unverified doctors, current/missing/expired/revoked/wrong-category grants, and concealed IDs. |
| Match status, validation, and safe-error behavior. | Current prescription statuses are mapped explicitly; drafts/unissued items return 409; unsupported states are rejected; errors omit clinical payloads. | Unit/HTTP tests cover status mapping, invalid identifiers, missing/inaccessible resources, authentication, safe response shape, and rate limiting. |
| Preserve required history and audit. | FHIR operations are read-only; they do not change prescriptions or patient history. Existing request audit records the route template, actor, status, and request ID without resource IDs or response bodies. | PostgreSQL test verifies the API request audit record and absence of patient/resource identifiers in its metadata. |

## Verification record

- Targeted FHIR unit, PostgreSQL, and validator tests: 16 passed.
- Ruff on changed Python files and `git diff --check`: passed.
- Runtime FHIR OpenAPI and checked-in contract comparison: passed.
- PostgreSQL verification used a dedicated disposable local database with synthetic data only; the database was dropped after testing.
- The full backend suite had one unrelated report-test fixture failure and 302 environment-dependent skips. The FHIR-specific tests passed independently and within full discovery.
- No migration was needed. No GitHub or Jira state was changed.
