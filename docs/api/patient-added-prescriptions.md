# ML-140 — patient-added prescription confirmation

## Workflow and labels

Create an owned patient-source draft using the existing
`POST /api/v1/patients/{patient_id}/prescriptions`, review/correct every saved
medicine item using the existing draft editor, then explicitly call
`POST /api/v1/prescriptions/{prescription_id}/confirm` with **no body**.
The confirmation action acknowledges review of the saved items. It accepts no
clinical input, query fields, inferred dose, source, reviewer or issuer override.
Firebase bearer authentication and the shared authenticated request quota apply.

Returns HTTP 200 `PrescriptionDetail`. First confirmation sets `status=active`,
`issued_at`/`updated_at` to the server time and increments `version` once. Here
`issued_at` is the existing contract's personal confirmation timestamp; it does
**not** mean a doctor clinically issued an order. No additional database fields
or migration are necessary.

Frontend labels derive from the existing projection:

| Response fields | Display meaning |
| --- | --- |
| `source=patient` | Patient-added |
| `review_status=not_reviewed`, no reviewer/time | Not clinically verified |
| Later explicit authorized doctor review `approved` | Patient-added · Doctor Approved; still not a doctor order |

Confirmation never changes source, prescriber, review outcome, claimed prescriber,
medicine content, dates, original document or predecessor lineage. Personal entries
may omit quantity/unit, route and course dates. Confirmation does not create
schedules, dose occurrences, pharmacy shares, dispensing events or doctor orders.
A later doctor reissue must be a separate linked doctor-source prescription.

## Authorization and state

Only an active patient with an unrevoked patient role and patient profile can
confirm their own patient-source draft. Existing professional consent does not
authorize a doctor to perform the patient's confirmation. Admin/Super Admin roles
provide no clinical access. Missing/foreign records are concealed with 404.
A known own doctor/external-source record is forbidden with 403.

The prescription row lock is shared with existing draft update/delete writers.
First confirmation requires an unissued draft with 1–100 valid saved items,
`not_reviewed` status and no review events. Revalidate stored item data, paired
quantity/unit, dates, catalogue references and current original-document ownership,
patient attribution, active status and prescription type. Never infer missing data.

An already active, confirmed patient-source record returns the current detail
without another transition, timestamp/version update or confirmation audit event.
A later doctor review remains intact on a retry. Terminal records cannot be
reopened, and inconsistent issued/reviewed drafts return a state/validation error.
The existing draft editor/delete routes reject confirmed records. A concurrent
edit either completes before confirmation (the current saved draft is confirmed)
or is rejected after confirmation. Concurrent confirmations produce one state
transition and one domain audit event. A concurrent deletion either deletes the
unconfirmed draft or loses to confirmation; no published record is deleted.

`prescription.patient.confirmed` is appended transactionally with actor, target,
request ID, source, previous/current state/version and item count. No raw clinical
text, file keys or claimed prescriber enters audit metadata. Audit failure rolls
back confirmation. Existing API request audit records remain independent.

## HTTP contract

| Status | Meaning |
| --- | --- |
| 200 | Confirmed personal record, or state-idempotent retry |
| 400 | Any nonempty request body, including `{}`, `null` or malformed JSON |
| 401 | Missing/invalid Firebase identity |
| 403 | Inactive account, missing current patient role/profile, forbidden source |
| 404 | Missing or concealed foreign prescription |
| 409 | Nonconfirmable lifecycle or inconsistent source/issuer/review state |
| 422 | Invalid UUID, any query field, invalid saved items or stale evidence |
| 429 | Shared authenticated account quota exceeded; `Retry-After` |
| 500 | Safe internal error; failed transaction rolled back |
| 503 | Identity/quota service unavailable; fail closed |

Errors use `{code,message,field_errors,request_id}`. Private responses use
`Cache-Control: no-store`; all responses have `X-Request-ID`. No Idempotency-Key
or If-Match is required by the approved bodyless confirmation contract. Callers
must reload and review saved content before the explicit action.

## Verification

Python 3.13; install `requirements-dev.lock`. Use two **disposable migrated**
PostgreSQL databases, with environment variables `ML140_TEST_DATABASE_URL` and
`ML140_CONCURRENCY_DATABASE_URL` (SQLAlchemy `postgresql+asyncpg://` URLs).
The first uses rollback-isolated fixtures; the concurrency suite commits synthetic
fixtures to its separate database. Never point these tests at production data.

```powershell
python -m unittest tests.test_ml140_confirmation_contract -v
python -m unittest tests.test_ml140_confirmation_postgres tests.test_ml140_confirmation_concurrency -v
```

The database suites explicitly skip without their environment variables; skipped
checks are not evidence of PostgreSQL correctness. The bundle's validation record
lists the actual run, full regression results and remaining environment limits.

## Integration boundaries

ML-76 record/clinical-entry QA is still an open Jira prerequisite at preparation
time. This implementation can be reviewed independently; do not claim that QA
has been completed. Doctor issue/review/reissue and pharmacy sharing/dispensing
APIs are separate tasks and are not implemented by ML-140. Their runtime routers
on the reviewed base remain placeholders. They must require doctor source plus
current doctor issue/authorization before any pharmacy action; `active` or
`issued_at` alone never authorizes dispensing. This package verifies that patient
confirmation cannot promote source/issuer/review or create pharmacy records; it
does not claim end-to-end testing of those future pharmacy endpoints.
