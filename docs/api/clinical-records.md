# ML-68: Patient record and clinical entries

Implemented operations (Firebase bearer identity and the shared authenticated quota):

- `GET /api/v1/patients/{patient_id}/clinical-records` (`medilink_027`)
- `POST /api/v1/patients/{patient_id}/clinical-records` (`medilink_028`)
- `GET /api/v1/clinical-records/{record_id}` (`medilink_037`)

The existing API router already includes this module. There is no new physical
`clinical_records` table. Migration `ml68_clinical_codes` adds nullable clinical_code
columns to allergies/clinical_notes, matching the existing public ClinicalInput
contract; existing rows remain null and the ORM models are updated together. Correction/version/request operations
belong to ML-72; ML-68 exposes no update/delete route and never modifies a prior
clinical record, including another doctor's entry.

## Permissions

An active Patient with a current Patient role can read their own records and add
conditions/allergies. Patient entries cannot claim a consultation, source, author,
organization, status or version link. An active, currently verified Doctor needs
an unexpired, unrevoked `history_allergies` grant from the target patient. For an
organization context, the organization must be active and the Doctor's membership
approved. A read does not require an open consultation.

Doctor creation additionally binds the supplied consultation to that exact grant,
patient, Doctor and organization; it must have started, remain open, and have the
patient's explicit consultation permission. Source/author/organization come from
that context. Admin/Super Admin receive no clinical access by administrative role.
Revoked roles and inactive accounts cannot authorize access. Other people's
concealed records and invalid contexts return `404 RESOURCE_NOT_FOUND`.

Authorization rows are locked until the clinical mutation and audit commit;
concurrent revocation waits for a previously authorized transaction. Requests
beginning after that revocation commits are denied. Grant time windows are
checked with the database clock after acquiring locks.

## Mapping and provenance

| API type | Typed table | title | description | recorded_at |
| --- | --- | --- | --- | --- |
| condition | conditions | condition_name | description | recorded_at |
| diagnosis | diagnoses | diagnosis_name | description | diagnosed_at |
| allergy | allergies | allergen | reaction | recorded_at |
| note | clinical_notes | note_type | note_text | recorded_at |

Every read includes source, recorded_by, organization_id (nullable), consultation,
recorded_at, created_at, status and available version links. Organization ID is an
additive read field, now recorded in the reference contract/OpenAPI. Authors remain
UUIDs as specified; consumers can display their permitted identity information.
Existing status and correction links are preserved, including historical records.

Clinical codes round-trip for all four types. The additive migration closes the
existing contract-to-schema gap without encoding values inside clinical text.
No severity field is fabricated from description. Note text defaults to an empty
string when the optional description is omitted, as its table requires text.

An empty allergy result means **unknown/not recorded**, never "no known allergies".
An explicit self-reported "No known allergies" allergy entry is returned as an
ordinary attributed entry; the API does not synthesize it from absence, infer a
medical conclusion, or claim clinical verification. There is no new summary-status
field or hidden sentinel. Filtered/paginated results must not be used to infer an
overall allergy assessment; retain provenance and conflicting entries for review.

## Input, paging and errors

JSON and query fields outside the declared contract are rejected. Titles are
trimmed and nonblank (200 characters); description is bounded to 8000 and code to
100. Timestamps require a timezone. Optional `from` (inclusive) and `to` (exclusive)
must be supplied together in ascending order, filtering recorded_at. `type` is one
of condition/diagnosis/allergy/note. Page defaults to 1, page_size to 20 (max 100).
The page contains items/page/page_size/total and uses created_at descending, ID
descending, then type as a final deterministic tie-break across tables.

Safe error envelopes use code/message/field_errors/request_id, without echoing
submitted clinical data. Malformed JSON returns 400; invalid fields return 422.
Reads and writes use Cache-Control: no-store and the shared X-Request-ID. Shared
rate-limit errors keep 429/Retry-After and unavailable-store 503 behavior.

Creation and its minimal `clinical_record.created` audit event commit together;
audit failure rolls back the clinical row. Reads append minimal viewed/listed
events. Audit metadata includes only record type/source or count, never clinical
text, codes, names or search/time filter values. API middleware retains safe
request-outcome auditing for denials/failures.

## Validation

Use Python 3.13.13 and requirements-dev.lock. For real database tests use only
migrated disposable synthetic databases; no Firebase credentials are required.

```powershell
python -m unittest tests.test_clinical_records -v
# Set ML68_TEST_DATABASE_URL to an isolated migrated test DB.
# Set ML68_CONCURRENCY_DATABASE_URL to a separate migrated disposable test DB.
python -m unittest tests.test_clinical_records_postgres tests.test_clinical_records_concurrency -v
python -m ruff check app/modules/clinical_records tests/test_clinical_records.py tests/test_clinical_records_postgres.py tests/test_clinical_records_concurrency.py
```

The integration fixtures roll back. The concurrency test commits synthetic
fixtures and leaves them in its disposable database for inspection. It does not
perform a real Firebase login or frontend end-to-end test.
