# MediLink — API Contract

Version 1.3.0 · 2 November 2026 · Consolidated design for team review

Sources: [Project specification](01_PROJECT.md), [PostgreSQL schema](05_POSTGRES_SCHEMA.sql), [data dictionary](04_DATA_DICTIONARY.md). The SQL is the database baseline; public field names follow actual SQL (`type`, `role`, `status`), where dictionary labels differ. MySQL is the corresponding alternate script.

This document and [03_OPENAPI.json](03_OPENAPI.json) define the interface baseline for team review. The routes are specified for implementation; they are not yet running endpoints. The contract text is English to share directly with frontend/backend developers. Read models describe the response fields; input models accept only specified fields. The approved release scope includes printed English prescription OCR with human review, measurement trend charts, authorized on-demand prescription PDF export, and doctor-owned favourites/templates. The approved SQL schema includes the OCR-job and doctor-owned template tables. The schema files are approved design baselines; PostgreSQL Alembic migrations still need to be created and applied to a development database. Browser push, handwriting OCR, email/SMS reminders, and native mobile are outside this release.

## 1. How the team uses this

Frontend builds forms and mock responses from the named request/response schemas. Backend implements the same schemas and permission checks. Database work applies the listed schema deltas through Alembic. Review changes to this contract before changing a route or field. OpenAPI JSON can be imported into an OpenAPI viewer or API client; do not treat it as a running server.

Firebase handles sign-in, sign-out and password recovery directly through its SDK. Angular sends its Firebase ID token to MediLink. There is no MediLink password/login endpoint. Onboarding derives Firebase identity/email from the verified token and reconciles a local profile safely on retries.

## 2. Shared conventions

- App prefix `/api/v1`; limited export prefix `/fhir/r4`. JSON fields use snake_case.
- Human authentication: `Authorization: Bearer <Firebase ID token>`. Mock clients use `X-API-Key`, not a human token. Never include credentials in URLs.
- UUID resource IDs; ISO 8601 dates; timezone-aware RFC3339 timestamps. UTC persistence, user timezone rendering. Numeric quantities are JSON numbers, persisted as PostgreSQL numeric.
- `GET` lists return `{items, page, page_size, total}`; page starts at 1, default page_size 20, maximum 100. Deterministic descending created_at/id order, or scheduled_for/id for dose lists, measured_at/id for measurements, report_date/id for reports. Filters must not widen authorization. `from` inclusive, `to` exclusive; both timestamps when used. Unknown query fields rejected.
- Required fields appear in each schema. Optional nullable fields may be null in reads. For PATCH, omitted means unchanged, explicit null clears only nullable fields. Empty PATCH rejected. PUT replaces the editable draft/profile representation. Unknown JSON fields rejected. Apply sensible bounded lengths to all text and arrays; maximum 100 prescription/dispensing items and 24 daily times. The policy limits in this contract are part of the approved baseline.
- Success is the documented 200/201/202; only draft deletion returns 204. Singleton responses are direct objects; no generic data wrapper. List responses use the page object.
- Server sets IDs, source, actor, issuer, organization attribution, timestamps, verification and clinical status. Clients cannot set these through generic input fields. Related IDs must all belong to the same patient/context.
- Protected reads/writes return safe response models, never raw ORM objects. No secret/hash/storage_key/Firebase UID in responses. One-time share/request/API secrets are returned only at creation. Use Cache-Control: no-store for patient/private/secret responses; X-Request-ID on responses. Poll current grant/share status; UI state never authorizes actions.
- Every request checks identity, local account, action/role, current verification and organization membership, category grant, expiry/revocation, ownership/source/status. Admin/Super Admin gain no automatic clinical access. Return 404 for another person's concealed resource, 403 for a known but forbidden operation.
- Pending professionals may access their profile, organization/membership applications and verification evidence/application routes only. Account suspension blocks protected access. MFA enforcement for privileged actions follows the finalized identity policy; exact Firebase setup remains an implementation decision.
- Mutation retries: required Idempotency-Key on marked operations, scope by actor+operation+target; same key+same body/file digest returns original status/body, mismatched payload returns 409 IDEMPOTENCY_CONFLICT. Persist successful response and body digest transactionally; do not consume a key for failed validation. Keep keys for the demo lifetime (team-review retention proposal). Other action routes are state-idempotent or return 409 for invalid repeated transitions.
- File uploads are multipart with `file`; max 10 MiB=10,485,760 bytes, detected PDF/JPEG/PNG, private random storage keys. No arbitrary remote URLs. Download content requires current parent-resource authorization; attachment names sanitized. Compensate for file/database failures and clean orphan uploads.

## 3. Permissions and workflow decisions

**Doctor consent.** Printed QR identifies a doctor, then the patient confirms identity, categories and duration (24h default, 7d or 30d). `allow_consultation` is a separate explicit patient choice. Consultation permission never outlasts the grant, and ending a consultation blocks its clinical writes. A doctor must have the target category for both reading and writing that category. `history_allergies` covers clinical records; `prescriptions` covers orders/dispensing; `reports` covers reports; `measurements_vaccinations` covers those entries; `medication_logs` covers schedules/doses/adherence. No category selection silently creates write permission.

**Prescriptions.** Draft → explicit doctor issue or patient confirmation → active. Only a doctor-issued active order can be dispensed. Patient-added data retains patient source after review. Doctor reissue creates a separate linked order. Saved corrections/renewals create new drafts, linked to the predecessor; issuing a replacement supersedes the old order atomically. Reason and action lineage are retained. Cancel/discontinue/complete update schedules consistently; elapsed logs remain. Doctor order issue requires quantity/unit, route, dose, frequency, duration and dates; no inferred dosing. Patient personal records may omit prescribed quantity and never authorize dispensing.

**Medication.** Daily fixed-time non-PRN only. Patient confirms times, course and IANA timezone. No auto schedule shift after dispensing. Due transition is based on scheduled_for; the approved worker rule is that the first evaluation after scheduled_for changes an unanswered event to unreported, while reminder delivery is tracked independently. Taken/skipped override that current status through a valid response. Snooze postpones reminder delivery only, never changes scheduled_for or denominator. Future/cancelled/PRN excluded; taken / (taken+skipped+unreported) ×100, latest correction only, null for zero eligible doses. Notifications are in-app; closed-browser alerts are not guaranteed.

**Pharmacy.** One 15-minute claim token; one pharmacist in one pharmacy; patient confirms claimant. Confirmed access lasts 24h from confirmation, revocable sooner. Before confirmation only minimum identity/share state is visible; no medicine details. Save checks active doctor order, grant and remaining quantity under locks. No overfill, substitutions or unit conversion. Reversal is append-only, reasoned, once per original event. Partial/complete dispensing is derived from net item quantities and does not mark the clinical course completed.

**Lab.** Patient creates a 7-day request for one approved lab and one initial report. Lab/manual and mock integration share the same duplicate checks. Original-lab corrections require new file/version and reason even after the initial request expires, but do not authorize unrelated initial uploads. Lab ownership is organizational; a currently verified staff member of that lab can correct its report. Patient-source reports are corrected only by their owner. Patient notified after successful publication/correction.

**Files/evidence.** Patient document authorization inherits prescription/report scope, not a blanket file permission. Professional verification evidence is a separate owner/application context and never appears in patient records. A patient/source author may correct their own published history by a version; patients cannot overwrite professional entries. Vaccination creation is doctor-only in this baseline. Patient-entered measurement corrections create a linked replacement via POST /api/v1/measurements/{measurement_id}/corrections. The patient may correct only their own active patient-sourced record.

**Integration.** One mock approved lab, reports:write only. Submission validates organization, request token, patient binding, content and duplicates. Initial upload returns 201 accepted + report_id after synchronous publication. Client+external_report_id identifies a submitted report; manual/integration collisions also check lab_reference and request. Corrected submissions use a new external_report_id and explicit original report path. No external patient search. API key creation/rotation reveal plaintext once; old credential expires immediately on rotation.

## 4. Approved API-to-database baseline

The API Contract and OpenAPI are aligned to the PostgreSQL schema v1.0 . The SQL files define the approved schema; they are not runtime databases and do not replace Alembic migration files. Measurement correction uses the operation and rules specified in this contract. Implement and verify the schema through the project migration workflow before wiring application routes to it.

| API area | Approved database mapping | Implementation handoff |
|---|---|---|
| Consent, doctor QR and consultation | `access_grants`, `grant_categories`, `professional_profiles.doctor_reference`, `consultations` | Enforce ownership, category, consultation permission, expiry and revocation on every protected operation. |
| Verification evidence/history | `documents`, `verification_application_documents`, `verification_decisions` | Keep evidence in private storage, enforce document owner/application context, and append decisions. |
| Pharmacy claim/dispensing | `prescription_shares`, `dispensing_events`, `dispensing_items` | Bind the claim to its pharmacy; apply expiry and quantity checks atomically with transaction locks. |
| Clinical records and corrections | Typed tables (`conditions`, `diagnoses`, `allergies`, `clinical_notes`) plus `record_corrections` | Map the combined API read model to the typed tables; do not create a physical `clinical_records` table. Patient-entered measurement correction uses linked replacement records. |
| OCR and prescribing shortcuts | `prescription_ocr_jobs`, `prescription_templates`, `prescription_template_items` | Implement patient review before OCR draft creation; keep templates doctor-owned and archive them without deletion. |
| Report lineage and integration retry safety | `report_identities`, `reports`, `integration_submissions`, `api_idempotency_records` | Preserve corrected versions, request identity and replay semantics in the same transaction as publication. |
| Jobs, backups and audit | `job_runs`, `backup_runs`, append-only `audit_events` | Implement bounded retries, encrypted daily backups, restore checks, and the agreed audit coverage. Do not physically delete project data or logs. |

The migration and runtime checks are implementation work: create reviewed Alembic revisions from this approved schema, apply them to a clean development database, and verify constraints and application transactions against the created tables. The SQL scripts alone do not demonstrate that these steps have run.

## 5. Error contract

```json
{"code":"ACCESS_DENIED","message":"You do not have access to this record.","field_errors":{},"request_id":"req-demo-001"}
```

401 AUTHENTICATION_REQUIRED/INVALID_TOKEN; 403 ACCESS_DENIED/ACCOUNT_INACTIVE/VERIFICATION_REQUIRED/CONSENT_EXPIRED; 404 RESOURCE_NOT_FOUND; 409 INVALID_STATE/QUANTITY_EXCEEDED/ALREADY_CLAIMED/DUPLICATE_REPORT/IDEMPOTENCY_CONFLICT/STALE_VERSION; 413 FILE_TOO_LARGE; 415 UNSUPPORTED_FILE_TYPE; 422 VALIDATION_ERROR; 429 RATE_LIMITED (Retry-After seconds); 500 INTERNAL_ERROR. Business-specific code identifies the cause; messages contain no stack traces, clinical payloads or credentials. Field errors map a dotted field path to a list of messages. Implement exception translation so FastAPI validation errors use this shape.

## 6. Endpoint catalogue

Path identifiers are UUIDs. `Idempotency-Key` marked **required** is a request header. All list routes share pagination and authorized deterministic ordering above. Exact schema definitions and required fields follow the catalogue and are also machine-readable in OpenAPI.


### Identity

| Method / path | Purpose | Allowed actor | Request → response |
|---|---|---|---|
| `POST /api/v1/me/onboard` | Reconcile Firebase identity and local profile | Verified Firebase identity | OnboardInput → Me (200) |
| `GET /api/v1/me` | Get account, roles, profiles and memberships | Own account | No body → Me (200) |
| `PATCH /api/v1/me` | Update contact/display details | Own account | ProfileInput → Me (200) |
| `PATCH /api/v1/me/patient-profile` | Update patient profile | Patient: own record | PatientProfileInput → PatientProfilesRead (200) |
| `PUT /api/v1/me/professional-profile` | Submit professional profile | Own professional account | ProfessionalProfileInput → ProfessionalProfilesRead (200) |

- `POST /api/v1/me/onboard`: Retry-safe upsert by verified firebase_uid; email comes from verified token. Public requested roles never confer verified professional or privileged access.
- `PUT /api/v1/me/professional-profile`: Changing role/registration after verification resets approval. One professional role per user in current schema; multi-professional-role support requires DB change.

### Organizations

| Method / path | Purpose | Allowed actor | Request → response |
|---|---|---|---|
| `GET /api/v1/organizations` | Find approved organizations | Authenticated human | No body → OrganizationPublicPage (200) |
| `POST /api/v1/organizations` | Submit organization registration | Professional applicant | OrganizationInput → OrganizationsRead (201) |
| `POST /api/v1/me/memberships` | Request organization membership | Own professional account | MembershipInput → OrganizationMembershipsRead (201) |
| `POST /api/v1/admin/organizations/{organization_id}/decision` | Approve/suspend organization | Super Admin | OrganizationDecision → OrganizationsRead (201) |
| `GET /api/v1/admin/organizations` | List organization applications | Super Admin | No body → OrganizationsReadPage (200) |
| `GET /api/v1/admin/memberships` | Review membership requests | Super Admin | No body → OrganizationMembershipsReadPage (200) |
| `POST /api/v1/admin/memberships/{membership_id}/decision` | Resolve membership request/dispute | Super Admin | MembershipDecision → OrganizationMembershipsRead (201) |

- `GET /api/v1/organizations`: Only approved public organization identity fields; no registration evidence. Filters: `type`, `q`.
- `POST /api/v1/organizations`: Status is always pending; applicant cannot approve it.
- `GET /api/v1/admin/organizations`:  Filters: `status`, `type`.
- `GET /api/v1/admin/memberships`:  Filters: `organization_id`, `status`.

### Verification

| Method / path | Purpose | Allowed actor | Request → response |
|---|---|---|---|
| `POST /api/v1/verification-applications` | Submit verification application | Own professional account | VerificationInput → VerificationApplicationsRead (201) |
| `GET /api/v1/me/verification-applications` | View own applications | Own account | No body → VerificationApplicationsReadPage (200) |
| `GET /api/v1/admin/verification-applications` | List review queue | Admin or Super Admin: delegated administrative permissions | No body → VerificationApplicationsReadPage (200) |
| `GET /api/v1/admin/verification-applications/{application_id}` | Get application/evidence references | Admin or Super Admin: delegated administrative permissions | No body → VerificationApplicationsRead (200) |
| `POST /api/v1/admin/verification-applications/{application_id}/decision` | Record verification decision | Admin or Super Admin: delegated administrative permissions | VerificationDecision → VerificationApplicationsRead (201) |
| `GET /api/v1/admin/verification-applications/{application_id}/decisions` | Read append-only decision history | Admin or Super Admin: delegated administrative permissions | No body → VerificationDecisionsReadPage (200) |

- `POST /api/v1/verification-applications`: Evidence IDs must be caller-owned private verification documents. Evidence can be uploaded before application creation.
- `GET /api/v1/admin/verification-applications`:  Filters: `status`, `role`.
- `GET /api/v1/admin/verification-applications/{application_id}`: Response includes evidence_document_ids after schema migration; files use authorized content route.
- `POST /api/v1/admin/verification-applications/{application_id}/decision`: Only pending applications can be decided. Reviewer cannot decide their own application. Approval updates professional role and optional organization membership transactionally; retain reviewer, decision, reason, and time in append-only history. SLMC and other registration details are manually reviewed; the API does not claim automatic verification.
- `GET /api/v1/admin/verification-applications/{application_id}/decisions`: Returns the append-only history in reverse chronological order, with `page` and `page_size` pagination (1–100).

### Consent

| Method / path | Purpose | Allowed actor | Request → response |
|---|---|---|---|
| `GET /api/v1/me/doctor-qr` | Get printed doctor identity QR | Verified Doctor | No body → DoctorQr (200) |
| `POST /api/v1/doctor-qr/resolve` | Resolve scanned doctor identity | Patient: own record | DoctorResolve → ProfessionalIdentity (200) |
| `POST /api/v1/access-grants` | Grant selected access to doctor | Patient: own record | GrantInput → GrantDetail (201) |
| `GET /api/v1/me/access-grants` | List own doctor grants | Patient: own record | No body → GrantDetailPage (200) |
| `POST /api/v1/access-grants/{grant_id}/revoke` | Revoke own grant | Patient: own record | ReasonInput → GrantDetail (201) |
| `GET /api/v1/me/patient-queue` | List patients currently ready for doctor | Verified Doctor | No body → QueueEntryPage (200) |

- `GET /api/v1/me/doctor-qr`: Opaque identity reference only; no grant or patient data.
- `POST /api/v1/access-grants`: Server resolves doctor reference. Category selection includes newly created records. 24h/7d/30d only; confirmation required in UI.
- `GET /api/v1/me/access-grants`:  Filters: `status`.
- `POST /api/v1/access-grants/{grant_id}/revoke`: Immediately blocks subsequent reads/writes; ends consultation permission.
- `GET /api/v1/me/patient-queue`: Only grants to this doctor; no patient directory.

### Consultations

| Method / path | Purpose | Allowed actor | Request → response |
|---|---|---|---|
| `POST /api/v1/consultations` | Start authorized consultation | Verified Doctor: current category grant; clinical writes also require patient-approved consultation | ConsultationInput → ConsultationsRead (201) |
| `GET /api/v1/patients/{patient_id}/consultations` | List consultations | Patient: own record or Verified Doctor: current category grant; clinical writes also require patient-approved consultation | No body → ConsultationsReadPage (200) |
| `POST /api/v1/consultations/{consultation_id}/end` | End own consultation | Verified Doctor: current category grant; clinical writes also require patient-approved consultation | ConsultationEnd → ConsultationsRead (201) |

- `POST /api/v1/consultations`: allow_consultation must be true. Patient/doctor/organization derived from grant; grant must remain active.
- `GET /api/v1/patients/{patient_id}/consultations`: Doctor sees their own consultation contexts; clinical categories remain independently checked.
- `POST /api/v1/consultations/{consultation_id}/end`: Owner doctor only. Ending is idempotent; no future clinical writes using ended context.

### Clinical records

| Method / path | Purpose | Allowed actor | Request → response |
|---|---|---|---|
| `GET /api/v1/patients/{patient_id}/clinical-records` | List clinical-records | Patient: own record or Verified Doctor: current category grant; clinical writes also require patient-approved consultation | No body → ClinicalRecordReadPage (200) |
| `POST /api/v1/patients/{patient_id}/clinical-records` | Create clinical-records | Patient: own record or Verified Doctor: current category grant; clinical writes also require patient-approved consultation | ClinicalInput → ClinicalRecordRead (201) |
| `POST /api/v1/clinical-records/{record_id}/versions` | Create corrected version | Patient: own record for own self-reported record; Verified Doctor: current category grant; clinical writes also require patient-approved consultation for own authored record | ClinicalVersionInput → ClinicalRecordRead (201) |
| `GET /api/v1/clinical-records/{record_id}/history` | Read version history | Patient: own record or Verified Doctor: current category grant; clinical writes also require patient-approved consultation | No body → ClinicalRecordReadPage (200) |
| `GET /api/v1/clinical-records/{record_id}` | Get permitted clinical record | Patient: own record or Verified Doctor: current category grant; clinical writes also require patient-approved consultation | No body → ClinicalRecordRead (200) |
| `POST /api/v1/clinical-records/{record_id}/correction-requests` | Flag incorrect professional entry | Patient: own record | ReasonInput → RecordCorrectionsRead (201) |
| `GET /api/v1/clinical-records/{record_id}/correction-requests` | List correction requests | Patient: own record or original author Doctor | No body → RecordCorrectionsReadPage (200) |
| `POST /api/v1/correction-requests/{correction_id}/decision` | Resolve correction request | Original author Doctor with active consent | CorrectionDecision → RecordCorrectionsRead (201) |

- `GET /api/v1/patients/{patient_id}/clinical-records`:  Filters: `type`, `from`, `to`.
- `POST /api/v1/patients/{patient_id}/clinical-records`: Server assigns source, recorder and organization. Patient clinical types limited to condition/allergy; professional entry requires active consultation and matching category. Vaccination entry is doctor-only baseline.
- `POST /api/v1/clinical-records/{record_id}/versions`: Reason required; preserve original and link supersedes_id. Doctor cannot correct another author. Record/source cannot be promoted by correction.
- `POST /api/v1/correction-requests/{correction_id}/decision`: Decision reason and replacement linkage require DB extension. Acceptance does not overwrite original.

### Measurements

| Method / path | Purpose | Allowed actor | Request → response |
|---|---|---|---|
| `GET /api/v1/patients/{patient_id}/measurements` | List measurements | Patient: own record or Verified Doctor: current category grant; clinical writes also require patient-approved consultation | No body → MeasurementsReadPage (200) |
| `POST /api/v1/patients/{patient_id}/measurements` | Create measurements | Patient: own record or Verified Doctor: current category grant; clinical writes also require patient-approved consultation | MeasurementInput → MeasurementsRead (201) |

- `GET /api/v1/patients/{patient_id}/measurements`:  Filters: `type`, `from`, `to`.
- `POST /api/v1/patients/{patient_id}/measurements`: Server assigns source, recorder and organization. Patient clinical types limited to condition/allergy; professional entry requires active consultation and matching category. Vaccination entry is doctor-only baseline.

### Vaccinations

| Method / path | Purpose | Allowed actor | Request → response |
|---|---|---|---|
| `GET /api/v1/patients/{patient_id}/vaccinations` | List vaccinations | Patient: own record or Verified Doctor: current category grant; clinical writes also require patient-approved consultation | No body → VaccinationsReadPage (200) |
| `POST /api/v1/patients/{patient_id}/vaccinations` | Create vaccinations | Verified Doctor: current category grant; clinical writes also require patient-approved consultation | VaccinationInput → VaccinationsRead (201) |
| `POST /api/v1/vaccinations/{record_id}/versions` | Create corrected version | Patient: own record for own self-reported record; Verified Doctor: current category grant; clinical writes also require patient-approved consultation for own authored record | VaccinationVersionInput → VaccinationsRead (201) |
| `GET /api/v1/vaccinations/{record_id}/history` | Read version history | Patient: own record or Verified Doctor: current category grant; clinical writes also require patient-approved consultation | No body → VaccinationsReadPage (200) |

- `GET /api/v1/patients/{patient_id}/vaccinations`:  Filters: `from`, `to`.
- `POST /api/v1/patients/{patient_id}/vaccinations`: Server assigns source, recorder and organization. Patient clinical types limited to condition/allergy; professional entry requires active consultation and matching category. Vaccination entry is doctor-only baseline.
- `POST /api/v1/vaccinations/{record_id}/versions`: Reason required; preserve original and link supersedes_id. Doctor cannot correct another author. Record/source cannot be promoted by correction.

### Medicines

| Method / path | Purpose | Allowed actor | Request → response |
|---|---|---|---|
| `GET /api/v1/medicines` | Search sample medicine catalogue | Patient or Doctor | No body → MedicinesReadPage (200) |
| `POST /api/v1/admin/medicines` | Add curated sample catalogue entry | Admin or Super Admin: delegated administrative permissions | MedicineInput → MedicinesRead (201) |

- `GET /api/v1/medicines`:  Filters: `q`.
- `POST /api/v1/admin/medicines`: Always labelled sample; no interaction-check claim.

### Prescriptions

| Method / path | Purpose | Allowed actor | Request → response |
|---|---|---|---|
| `GET /api/v1/patients/{patient_id}/prescriptions` | List prescriptions | Patient: own record or Verified Doctor: current category grant; clinical writes also require patient-approved consultation | No body → PrescriptionDetailPage (200) |
| `POST /api/v1/patients/{patient_id}/prescriptions` | Create prescription draft | Patient: own record or Verified Doctor: current category grant; clinical writes also require patient-approved consultation | PrescriptionInput → PrescriptionDetail (201) |
| `GET /api/v1/prescriptions/{prescription_id}` | Read prescription and provenance | Patient: own record or Verified Doctor: current category grant; clinical writes also require patient-approved consultation | No body → PrescriptionDetail (200) |
| `PUT /api/v1/prescriptions/{prescription_id}/draft` | Replace own draft | Patient: own record or owner Doctor | PrescriptionInput → PrescriptionDetail (200) |
| `DELETE /api/v1/prescriptions/{prescription_id}/draft` | Delete own unreferenced draft | Patient: own record or owner Doctor | No body → No body (204) |
| `POST /api/v1/prescriptions/{prescription_id}/confirm` | Confirm patient-added draft | Patient: own record | No body → PrescriptionDetail (200) |
| `POST /api/v1/prescriptions/{prescription_id}/issue` | Issue doctor prescription | Verified Doctor: current category grant; clinical writes also require patient-approved consultation | No body → PrescriptionDetail (200) |
| `POST /api/v1/prescriptions/{prescription_id}/reviews` | Review patient-added evidence | Verified Doctor: current category grant; clinical writes also require patient-approved consultation | PrescriptionReviewInput → PrescriptionReviewsRead (201) |
| `POST /api/v1/prescriptions/{prescription_id}/versions` | Create linked versions | Patient: own record for patient-source versions only; Verified Doctor: current category grant; clinical writes also require patient-approved consultation | PrescriptionVersionInput → PrescriptionDetail (201) |
| `POST /api/v1/prescriptions/{prescription_id}/renewals` | Create linked renewals | Patient: own record for patient-source versions only; Verified Doctor: current category grant; clinical writes also require patient-approved consultation | PrescriptionVersionInput → PrescriptionDetail (201) |
| `POST /api/v1/prescriptions/{prescription_id}/reissues` | Create linked reissues | Patient: own record for patient-source versions only; Verified Doctor: current category grant; clinical writes also require patient-approved consultation | PrescriptionVersionInput → PrescriptionDetail (201) |
| `POST /api/v1/prescriptions/{prescription_id}/cancel` | Cancel prescription | Owner Doctor with current consultation permission; Patient only for own patient-source personal record | ReasonInput → PrescriptionDetail (201) |
| `POST /api/v1/prescriptions/{prescription_id}/discontinue` | Discontinue prescription | Owner Doctor with current consultation permission; Patient only for own patient-source personal record | ReasonInput → PrescriptionDetail (201) |
| `POST /api/v1/prescriptions/{prescription_id}/complete` | Complete prescription | Owner Doctor with current consultation permission; Patient only for own patient-source personal record | ReasonInput → PrescriptionDetail (201) |
| `GET /api/v1/prescriptions/{prescription_id}/history` | Read linked prescription versions | Patient: own record or Verified Doctor: current category grant; clinical writes also require patient-approved consultation | No body → PrescriptionDetailPage (200) |
| `GET /api/v1/prescriptions/{prescription_id}/pdf` | Export prescription as PDF | Patient: own record or Verified Doctor: current category grant; clinical writes also require patient-approved consultation | No body → pdf (200) |

- `GET /api/v1/patients/{patient_id}/prescriptions`:  Filters: `status`, `source`.
- `POST /api/v1/patients/{patient_id}/prescriptions`: Patient source is patient and prescriber_user_id null. Doctor source/issuer from authenticated context; consultation required. No client source/issuer/status fields accepted.
- `GET /api/v1/prescriptions/{prescription_id}`: Pharmacist uses share-specific projection route instead.
- `PUT /api/v1/prescriptions/{prescription_id}/draft`: Draft only; atomically replace items, no published histories deleted.
- `DELETE /api/v1/prescriptions/{prescription_id}/draft`: Only unconfirmed, never issued drafts with no dependent published records. Private unlinked files follow cleanup policy.
- `POST /api/v1/prescriptions/{prescription_id}/confirm`: Sets active/issued_at for personal use; source remains patient, not clinically verified. Requires item review.
- `POST /api/v1/prescriptions/{prescription_id}/issue`: Owner doctor; current consultation permission; complete route, dose, frequency, duration, quantity/unit, dates, explicit review. Idempotent if already issued unchanged.
- `POST /api/v1/prescriptions/{prescription_id}/reviews`: Original evidence visible under prescription grant. Never changes source or grants dispensing.
- `POST /api/v1/prescriptions/{prescription_id}/versions`: Returns new draft linked by previous_prescription_id; reason/action persisted. Doctor renewals/reissues require separate review and issue. Reissue of patient entry makes a separate doctor-source order. Issue version atomically supersedes predecessor and cancels obsolete future doses.
- `POST /api/v1/prescriptions/{prescription_id}/renewals`: Returns new draft linked by previous_prescription_id; reason/action persisted. Doctor renewals/reissues require separate review and issue. Reissue of patient entry makes a separate doctor-source order. Issue version atomically supersedes predecessor and cancels obsolete future doses.
- `POST /api/v1/prescriptions/{prescription_id}/reissues`: Returns new draft linked by previous_prescription_id; reason/action persisted. Doctor renewals/reissues require separate review and issue. Reissue of patient entry makes a separate doctor-source order. Issue version atomically supersedes predecessor and cancels obsolete future doses.
- `POST /api/v1/prescriptions/{prescription_id}/cancel`: Explicit status transition; source-specific ownership checked. Cancel future occurrences and invalidate pharmacy access for cancelled/discontinued prescriptions. No rewriting past logs.
- `POST /api/v1/prescriptions/{prescription_id}/discontinue`: Explicit status transition; source-specific ownership checked. Cancel future occurrences and invalidate pharmacy access for cancelled/discontinued prescriptions. No rewriting past logs.
- `POST /api/v1/prescriptions/{prescription_id}/complete`: Explicit status transition; source-specific ownership checked. Cancel future occurrences and invalidate pharmacy access for cancelled/discontinued prescriptions. No rewriting past logs.
- `GET /api/v1/prescriptions/{prescription_id}/pdf`: Generate on demand from the authorized prescription projection; recheck current ownership/grant/consent. Include source, issuer/review status and lifecycle. Do not persist a duplicate PDF or bypass parent-record authorization.

### Prescribing shortcuts

| Method / path | Purpose | Allowed actor | Request → response |
|---|---|---|---|
| `GET /api/v1/me/prescription-templates` | List own favourites and templates | Verified Doctor | No body → PrescriptionTemplateReadPage (200) |
| `POST /api/v1/me/prescription-templates` | Save doctor-owned favourite or template | Verified Doctor | PrescriptionTemplateInput → PrescriptionTemplateDetail (201) |
| `PATCH /api/v1/prescription-templates/{template_id}` | Edit own favourite or template | Owner Verified Doctor | PrescriptionTemplatePatch → PrescriptionTemplateDetail (200) |
| `POST /api/v1/prescription-templates/{template_id}/archive` | Archive own favourite or template | Owner Verified Doctor | ReasonInput → PrescriptionTemplateDetail (201) |

- `GET /api/v1/me/prescription-templates`:  Filters: `template_type`, `status`.
- `POST /api/v1/me/prescription-templates`: Template stores medication fields only, not a patient or issued prescription. Enforce owner doctor and unique active name per doctor; archive rather than physically delete.
- `PATCH /api/v1/prescription-templates/{template_id}`: Active template owned by caller only. Template use fills an editable doctor draft; current patient consent, consultation, review and explicit issue remain required.
- `POST /api/v1/prescription-templates/{template_id}/archive`: Archive is reversible/status-based; retain row and audit history. No physical deletion.

### Prescription OCR

| Method / path | Purpose | Allowed actor | Request → response |
|---|---|---|---|
| `POST /api/v1/patients/{patient_id}/prescription-ocr-jobs` | Start printed English prescription OCR | Patient: own record | PrescriptionOcrJobInput → PrescriptionOcrJobRead (202) · Idempotency-Key **required** |
| `GET /api/v1/patients/{patient_id}/prescription-ocr-jobs` | List own OCR jobs | Patient: own record | No body → PrescriptionOcrJobReadPage (200) |
| `GET /api/v1/prescription-ocr-jobs/{job_id}` | Read OCR status and extracted draft fields | Patient: own record | No body → PrescriptionOcrJobRead (200) |
| `POST /api/v1/prescription-ocr-jobs/{job_id}/retry` | Retry a failed OCR job | Patient: own record | ReasonInput → PrescriptionOcrJobRead (202) |
| `POST /api/v1/patients/{patient_id}/prescription-ocr-jobs/{job_id}/draft` | Save patient-corrected OCR fields as a prescription draft | Patient: own record | PrescriptionOcrDraftInput → PrescriptionOcrDraftCreated (201) · Idempotency-Key **required** |

- `POST /api/v1/patients/{patient_id}/prescription-ocr-jobs`: Own prescription document only; detected PDF/JPEG/PNG. Create a retained OCR job and return queued state. Handwriting is out of scope; OCR output is untrusted draft data and never authorizes prescribing or dispensing.
- `GET /api/v1/patients/{patient_id}/prescription-ocr-jobs`: Patient owner only; do not expose another patient’s extraction or job metadata. Filters: `status`.
- `GET /api/v1/prescription-ocr-jobs/{job_id}`: Owner patient only. Return ready_for_review, failed-safe error code, attempt count and extracted fields; never claim clinical verification.
- `POST /api/v1/prescription-ocr-jobs/{job_id}/retry`: Owner patient only; failed job only; bounded retries. Reuse the original file; append attempt/audit history without deleting prior outcomes.
- `POST /api/v1/patients/{patient_id}/prescription-ocr-jobs/{job_id}/draft`: Owner patient only; job must be ready_for_review. Client sends corrected fields after human review; server creates patient-source draft linked to the retained original document and job. Patient must still use the existing confirm action; source remains patient and never becomes doctor-issued.

### Medication

| Method / path | Purpose | Allowed actor | Request → response |
|---|---|---|---|
| `POST /api/v1/patients/{patient_id}/medication-schedules` | Confirm fixed-time schedule | Patient: own record | ScheduleInput → ScheduleDetail (201) |
| `GET /api/v1/patients/{patient_id}/medication-schedules` | List medication schedules | Patient: own record or Verified Doctor: current category grant; clinical writes also require patient-approved consultation | No body → ScheduleDetailPage (200) |
| `POST /api/v1/medication-schedules/{schedule_id}/status` | Pause/resume/end schedule | Patient: own record | ScheduleAction → ScheduleDetail (201) |
| `POST /api/v1/medication-schedules/{schedule_id}/replacement` | Replace reminder times | Patient: own record | ScheduleInput → ScheduleDetail (201) |
| `GET /api/v1/patients/{patient_id}/dose-occurrences` | List dose events | Patient: own record or Verified Doctor: current category grant; clinical writes also require patient-approved consultation | No body → DoseOccurrencesReadPage (200) |
| `POST /api/v1/dose-occurrences/{occurrence_id}/snooze` | Snooze same occurrence | Patient: own record | SnoozeInput → DoseOccurrencesRead (201) |
| `POST /api/v1/dose-occurrences/{occurrence_id}/responses` | Record Taken/Skipped | Patient: own record | DoseResponseInput → MedicationLogsRead (201) · Idempotency-Key **required** |
| `POST /api/v1/medication-logs/{log_id}/corrections` | Correct latest response | Patient: own record | DoseCorrectionInput → MedicationLogsRead (201) · Idempotency-Key **required** |
| `GET /api/v1/dose-occurrences/{occurrence_id}/responses` | Read dose response history | Patient: own record or Verified Doctor: current category grant; clinical writes also require patient-approved consultation | No body → MedicationLogsReadPage (200) |
| `GET /api/v1/patients/{patient_id}/adherence` | Get patient-reported adherence | Patient: own record or Verified Doctor: current category grant; clinical writes also require patient-approved consultation | No body → Adherence (200) |

- `POST /api/v1/patients/{patient_id}/medication-schedules`: Patient/source labels remain visible. Daily fixed-time non-PRN only; IANA timezone; course bounds enforced; reject overlapping active schedules for same item.
- `GET /api/v1/patients/{patient_id}/medication-schedules`:  Filters: `status`.
- `POST /api/v1/medication-schedules/{schedule_id}/status`: Reminder state only; no change to clinical dose. Resume cannot generate duplicate occurrences.
- `POST /api/v1/medication-schedules/{schedule_id}/replacement`: Same prescription item only. Transactionally cancel old future occurrences and activate replacement; past occurrences/logs retained.
- `GET /api/v1/patients/{patient_id}/dose-occurrences`:  Filters: `from`, `to`, `status`.
- `POST /api/v1/dose-occurrences/{occurrence_id}/snooze`: Due/unreported, unanswered, uncancelled occurrence only; future snoozed_until within 24h and schedule bounds. Approved 24h maximum; eligibility uses original scheduled_for.
- `POST /api/v1/dose-occurrences/{occurrence_id}/responses`: taken requires reported_taken_at; skipped forbids it. Future taken time rejected, late entry allowed. Second initial response returns 409; use correction route.
- `POST /api/v1/medication-logs/{log_id}/corrections`: Append correction_of_id; one current linear chain per occurrence enforced transactionally. Old/stale head returns 409.
- `GET /api/v1/patients/{patient_id}/adherence`: Both period bounds required; latest response per past non-PRN/noncancelled occurrence. eligible=taken+skipped+unreported; null percentage when zero. Doctor needs medication_logs grant. Filters: `from`, `to`.

### Pharmacy

| Method / path | Purpose | Allowed actor | Request → response |
|---|---|---|---|
| `POST /api/v1/prescription-shares` | Create one-prescription share | Patient: own record | ShareInput → ShareCreated (201) |
| `GET /api/v1/me/prescription-shares` | List own share requests | Patient: own record | No body → ShareDetailPage (200) |
| `POST /api/v1/prescription-shares/claim` | Atomically claim code | Verified Pharmacist: active pharmacy membership | ClaimInput → ShareDetail (200) |
| `GET /api/v1/prescription-shares/{share_id}` | Read share state and claimed professional | Patient: own record or claimant Pharmacist | No body → ShareDetail (200) |
| `POST /api/v1/prescription-shares/{share_id}/confirm` | Confirm pharmacist identity | Patient: own record | No body → ShareDetail (200) |
| `POST /api/v1/prescription-shares/{share_id}/revoke` | Revoke prescription access | Patient: own record | ReasonInput → ShareDetail (201) |
| `GET /api/v1/prescription-shares/{share_id}/prescription` | Read minimal dispensable prescription | Verified Pharmacist: active pharmacy membership plus confirmed unexpired share | No body → PharmacyPrescription (200) |
| `POST /api/v1/dispensing-events` | Save partial/complete dispensing | Verified Pharmacist: active pharmacy membership plus confirmed unexpired share | DispenseInput → DispenseDetail (201) · Idempotency-Key **required** |
| `GET /api/v1/me/dispensing-events` | List own minimal dispensing history | Verified Pharmacist: active pharmacy membership | No body → DispenseHistoryPage (200) |
| `GET /api/v1/patients/{patient_id}/dispensing-events` | View patient dispensing history | Patient: own record or Verified Doctor: current category grant; clinical writes also require patient-approved consultation | No body → DispenseDetailPage (200) |
| `POST /api/v1/dispensing-events/{event_id}/reversal` | Reverse own erroneous event | Verified Pharmacist: active pharmacy membership | ReasonInput → DispenseDetail (201) · Idempotency-Key **required** |

- `POST /api/v1/prescription-shares`: Active doctor-issued prescriptions only; claim_token returned once, expires in 15 minutes.
- `POST /api/v1/prescription-shares/claim`: Single claimant; minimal patient/prescription metadata until patient confirmation, no medicines yet.
- `POST /api/v1/prescription-shares/{share_id}/confirm`: Claim must be current and unexpired; starts 24h access from confirmed_at; binds claimant and pharmacy.
- `POST /api/v1/dispensing-events`: Lock prescription/items; match pharmacy/share/claimant, active doctor source; reject overfill or mismatched units. Duplicate item IDs rejected. No substitutions. Derived partial/complete status never changes clinical completion automatically.
- `GET /api/v1/me/dispensing-events`: After grant expiry expose own event fields/quantities only; no new patient or prescription clinical details.
- `GET /api/v1/patients/{patient_id}/dispensing-events`: Doctor needs prescriptions category.
- `POST /api/v1/dispensing-events/{event_id}/reversal`: Whole-event reversal only, once; same pharmacist/pharmacy. No new patient details disclosed after expiry. Lock remaining quantity and append reasoned inverse event. Replacement dispensing needs live share.

### Laboratory

| Method / path | Purpose | Allowed actor | Request → response |
|---|---|---|---|
| `POST /api/v1/lab-upload-requests` | Create lab-specific one-report request | Patient: own record | LabRequestInput → LabRequestCreated (201) |
| `GET /api/v1/me/lab-upload-requests` | List own lab requests | Patient: own record | No body → LabUploadRequestsReadPage (200) |
| `POST /api/v1/lab-upload-requests/{request_id}/revoke` | Revoke unused request | Patient: own record | ReasonInput → LabUploadRequestsRead (201) |
| `POST /api/v1/lab-upload-requests/resolve` | Resolve lab request to minimal patient identity | Verified Lab Staff: active matching laboratory membership | LabResolveInput → LabRequestResolved (200) |
| `POST /api/v1/patients/{patient_id}/reports` | Publish patient/lab report | Patient: own record or Verified Lab Staff: active matching laboratory membership | ReportInput → ReportsRead (201) · Idempotency-Key **required** |
| `GET /api/v1/patients/{patient_id}/reports` | List patient reports | Patient: own record or Verified Doctor: current category grant; clinical writes also require patient-approved consultation | No body → ReportsReadPage (200) |
| `GET /api/v1/reports/{report_id}` | Read report metadata | Patient: own record or Verified Doctor: current category grant; clinical writes also require patient-approved consultation or originating Lab | No body → ReportsRead (200) |
| `GET /api/v1/me/laboratory-reports` | List own lab reports | Verified Lab Staff: active matching laboratory membership | No body → ReportsReadPage (200) |
| `POST /api/v1/reports/{report_id}/versions` | Publish corrected report | Patient: own record for own patient report or originating Verified Lab Staff: active matching laboratory membership | ReportVersionInput → ReportsRead (201) · Idempotency-Key **required** |
| `POST /api/v1/reports/{report_id}/withdraw` | Withdraw erroneous report | Owner Patient for patient-source or originating Verified Lab Staff: active matching laboratory membership | ReasonInput → ReportsRead (201) |
| `GET /api/v1/reports/{report_id}/history` | Read report version history | Patient: own record or Verified Doctor: current category grant; clinical writes also require patient-approved consultation or originating Lab | No body → ReportsReadPage (200) |

- `POST /api/v1/lab-upload-requests`: 7-day expiry; approved lab; token returned once and stored hashed.
- `POST /api/v1/lab-upload-requests/resolve`: No patient directory; token and lab must match active unused request.
- `POST /api/v1/patients/{patient_id}/reports`: Patient uploads own document without lab attribution. Lab requires active upload_request_id matching patient/lab/document; consumes request atomically, deduplicates lab reference and notifies patient.
- `GET /api/v1/patients/{patient_id}/reports`:  Filters: `from`, `to`, `status`.
- `GET /api/v1/me/laboratory-reports`: Restricted to active organization membership; no unrelated patient browsing. Filters: `from`, `to`.
- `POST /api/v1/reports/{report_id}/versions`: Same patient/lab; author/organization ownership checked even if original upload request expired. New document required; supersede original; notify patient. Clinical review permission alone cannot correct a report.
- `POST /api/v1/reports/{report_id}/withdraw`: Keep original/history and reason; block ordinary content access after withdrawal.

### Documents

| Method / path | Purpose | Allowed actor | Request → response |
|---|---|---|---|
| `POST /api/v1/patients/{patient_id}/documents` | Upload private patient file | Patient: own record or Verified Lab Staff: active matching laboratory membership | DocumentUpload → DocumentsRead (201) |
| `POST /api/v1/me/verification-documents` | Upload own professional evidence | Professional applicant | DocumentUpload → DocumentsRead (201) |
| `GET /api/v1/documents/{document_id}` | Read permitted document metadata | Owner or authorized parent-resource reader | No body → DocumentsRead (200) |
| `GET /api/v1/documents/{document_id}/content` | Stream private file | Authorized parent-resource reader | No body → binary (200) |

- `POST /api/v1/patients/{patient_id}/documents`: Lab requires active matching upload request, or supersedes_id from authorized lab correction. PDFs/JPEG/PNG <=10 MiB; file bytes validated. Lab cannot upload arbitrary patient files.
- `POST /api/v1/me/verification-documents`: document_type=verification_evidence; owner_user_id assigned by server; patient_user_id null. Never exposed by patient category grants.
- `GET /api/v1/documents/{document_id}`: Check linked prescription/report/application policy; uploaded-but-unlinked files uploader only.
- `GET /api/v1/documents/{document_id}/content`: Per-request permission, source/status/grant checks; Content-Disposition attachment; no public storage path. No verification evidence through clinical grants.

### Notifications

| Method / path | Purpose | Allowed actor | Request → response |
|---|---|---|---|
| `GET /api/v1/me/notifications` | Refresh in-app notifications | Own account | No body → NotificationsReadPage (200) |
| `POST /api/v1/notifications/{notification_id}/read` | Mark own notification read | Own account | No body → NotificationsRead (200) |

- `GET /api/v1/me/notifications`:  Filters: `unread`.
- `POST /api/v1/notifications/{notification_id}/read`: Retry-safe; no ownership override.

### Administration

| Method / path | Purpose | Allowed actor | Request → response |
|---|---|---|---|
| `GET /api/v1/admin/users` | Find accounts for support | Admin or Super Admin: delegated administrative permissions | No body → UsersReadPage (200) |
| `POST /api/v1/admin/users/{user_id}/status` | Suspend/reactivate delegated account | Admin or Super Admin: delegated administrative permissions | AccountDecision → UsersRead (201) |
| `POST /api/v1/admin/role-assignments` | Appoint Admin | Super Admin | AdminRoleInput → ActionResult (201) |
| `POST /api/v1/admin/users/{user_id}/admin-role/revoke` | Revoke Admin role | Super Admin | ReasonInput → ActionResult (201) |
| `GET /api/v1/admin/operations` | Read operational counts/backup timestamp | Admin or Super Admin: delegated administrative permissions | No body → OperationalSummary (200) |
| `GET /api/v1/admin/jobs` | View failed/background jobs | Admin or Super Admin: delegated administrative permissions | No body → JobStatusPage (200) |
| `POST /api/v1/admin/jobs/{job_id}/retry` | Request bounded retry | Admin or Super Admin: delegated administrative permissions | ReasonInput → JobStatus (202) |
| `GET /api/v1/admin/audit-events` | Read delegated audit events | Admin or Super Admin: delegated administrative permissions | No body → AuditEventsReadPage (200) |
| `GET /api/v1/patients/{patient_id}/activity` | Read own record/access activity | Patient: own record | No body → PatientActivityPage (200) |

- `GET /api/v1/admin/users`: Operational identity only; no clinical records. Bounded exact/refined account search; audits required. Filters: `q`, `status`.
- `POST /api/v1/admin/users/{user_id}/status`: Admin cannot target Admin/Super Admin or self-escalate. Super Admin cannot remove final active Super Admin via this route.
- `POST /api/v1/admin/role-assignments`: Requires privileged MFA policy. No public Super Admin creation route.
- `GET /api/v1/admin/operations`: Counts/status only, no patient payloads.
- `GET /api/v1/admin/jobs`: Requires job_runs persistence; no stack traces/secrets. Filters: `status`.
- `POST /api/v1/admin/jobs/{job_id}/retry`: Only retryable failed job, idempotent scheduling and current permissions/state rechecked by worker.
- `GET /api/v1/admin/audit-events`: Admin restricted from privileged audit; Super Admin oversight. No update/delete endpoints; scrub payloads/secrets. Filters: `action`, `from`, `to`, `request_id`.
- `GET /api/v1/patients/{patient_id}/activity`: Only own patient target events; patient-safe projection, no internal security metadata or unrelated actors. Apply response field filtering.

### Integration management

| Method / path | Purpose | Allowed actor | Request → response |
|---|---|---|---|
| `POST /api/v1/admin/integration-clients` | Approve mock lab client | Super Admin | IntegrationClientInput → CredentialCreated (201) |
| `GET /api/v1/admin/integration-clients` | List clients without credentials | Super Admin | No body → IntegrationClientsReadPage (200) |
| `POST /api/v1/admin/integration-clients/{client_id}/rotate` | Rotate and expire old credential | Super Admin | RotateCredential → CredentialCreated (201) |
| `POST /api/v1/admin/integration-clients/{client_id}/revoke` | Revoke client | Super Admin | ReasonInput → IntegrationClientsRead (201) |
| `POST /api/v1/admin/integration-clients/{client_id}/suspend` | Suspend client | Super Admin | ReasonInput → IntegrationClientsRead (201) |
| `GET /api/v1/admin/integration-submissions` | Inspect sanitized submission outcomes | Super Admin | No body → IntegrationSubmissionsReadPage (200) |

- `POST /api/v1/admin/integration-clients`: Approved lab only, reports:write only; random credential revealed once.
- `POST /api/v1/admin/integration-clients/{client_id}/rotate`: Immediate old-key invalidation; credential hash never returned.
- `GET /api/v1/admin/integration-submissions`: Filters: `status`; returns operational submission identifiers and outcomes without the client-supplied idempotency key or report payload.

### Mock integration

| Method / path | Purpose | Allowed actor | Request → response |
|---|---|---|---|
| `POST /api/v1/integrations/reports` | Submit one lab report file | Active matching lab client with reports:write | IntegrationReportUpload → IntegrationSubmissionsRead (201) · Idempotency-Key **required** |
| `POST /api/v1/integrations/reports/{report_id}/versions` | Correct own integration report | Same active lab client with reports:write | IntegrationReportVersionUpload → IntegrationSubmissionsRead (201) · Idempotency-Key **required** |

- `POST /api/v1/integrations/reports`: Synchronous validation/publication, returns accepted submission + report_id. No arbitrary remote file URLs. Same client+external_report_id deduplicated across retries; same lab_reference deduplicated with manual channel. No patient directory.
- `POST /api/v1/integrations/reports/{report_id}/versions`: Correction requires reason multipart field (added below); original report ownership, same patient/lab; request token identifies original request and need not remain unused/unexpired. No new initial upload permission from old token.

### FHIR export

| Method / path | Purpose | Allowed actor | Request → response |
|---|---|---|---|
| `GET /fhir/r4/Patient/{patient_id}` | Export limited R4 Patient | Patient: own record or Verified Doctor: current category grant; clinical writes also require patient-approved consultation | No body → FhirPatient (200) |
| `GET /fhir/r4/MedicationRequest/{item_id}` | Export doctor-issued medication item | Patient: own record or Verified Doctor: current category grant; clinical writes also require patient-approved consultation | No body → FhirMedicationRequest (200) |

- `GET /fhir/r4/Patient/{patient_id}`: Doctor needs history_allergies grant; patient identity reference only. Mapping/profile validation remains implementation gate.
- `GET /fhir/r4/MedicationRequest/{item_id}`: prescriptions category. One resource per item; source=doctor only. Patient-source returns 404. requester omitted until Practitioner export supported; map patient reference to exported Patient. Superseded item maps cancelled; discontinued maps stopped.

## 7. Concrete request examples

Patient grants a doctor read scope and separately permits consultation:
```http
POST /api/v1/access-grants
Authorization: Bearer <firebase_id_token>
Content-Type: application/json

{"doctor_reference":"opaque-demo-reference","categories":["history_allergies","prescriptions"],"duration_hours":24,"allow_consultation":true}
```

Patient reports one dose (synthetic example):
```http
POST /api/v1/dose-occurrences/11111111-1111-4111-8111-111111111111/responses
Authorization: Bearer <firebase_id_token>
Idempotency-Key: dose-demo-0001
Content-Type: application/json

{"response":"taken","reported_taken_at":"2026-10-26T08:00:00+05:30"}
```

Mock lab uploads a file with metadata:
```text
POST /api/v1/integrations/reports
X-API-Key: <server-side-client-secret>
Idempotency-Key: lab-demo-0001
Content-Type: multipart/form-data; boundary=<generated-by-client>

file: <synthetic PDF bytes>
request_token: <patient-created-lab-token>
external_report_id: demo-report-001
report_type: Synthetic blood test
report_date: 2026-10-26
lab_reference: DEMO-LAB-001
```

## 8. Schema dictionary

`?` means optional on input or not required on output; `nullable` explicitly allows null. Server-generated read models are not accepted as request bodies. Decimal fields use SQL precision in the database. Read table schemas are public projections of actual SQL fields; private columns are excluded. Additional contextual fields listed in section 4 depend on migrations.


### UsersRead

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `email` | string; nullable | No |
| `display_name` | string | Yes |
| `phone` | string; nullable | No |
| `status` | string: active, pending, suspended, disabled | Yes |
| `is_admin` | boolean; current non-revoked Admin assignment | Yes |
| `created_at` | string (date-time) | Yes |
| `updated_at` | string (date-time) | Yes |

`is_admin` is true only when the user has a current, non-revoked `admin` assignment.
It is false when no Admin assignment exists, the Admin assignment has been revoked,
or the account has only the `super_admin` role. A user with both roles has
`is_admin: true` while the `admin` assignment remains non-revoked.
This field is required in every `UsersRead` response, including account search
items and account status-update responses. It does not grant authorization
or replace the existing permission and MFA checks.

### RolesRead

| Field | Type / validation | Required |
|---|---|---|
| `id` | integer | Yes |
| `code` | string | Yes |
| `label` | string | Yes |

### UserRolesRead

| Field | Type / validation | Required |
|---|---|---|
| `user_id` | string (uuid) | Yes |
| `role_id` | integer | Yes |
| `assigned_by` | string (uuid); nullable | No |
| `assigned_at` | string (date-time) | Yes |

### OrganizationsRead

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `name` | string | Yes |
| `type` | string: clinic, hospital, pharmacy, laboratory, other | Yes |
| `registration_ref` | string; nullable | No |
| `status` | string: pending, active, suspended, rejected, inactive | Yes |
| `created_at` | string (date-time) | Yes |
| `updated_at` | string (date-time) | Yes |

### OrganizationMembershipsRead

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `organization_id` | string (uuid) | Yes |
| `user_id` | string (uuid) | Yes |
| `role` | string: doctor, pharmacist, lab_staff | Yes |
| `status` | string: not_submitted, pending, approved, rejected, more_info_requested, suspended | Yes |
| `verified_at` | string (date-time); nullable | No |
| `created_at` | string (date-time) | Yes |

### PatientProfilesRead

| Field | Type / validation | Required |
|---|---|---|
| `user_id` | string (uuid) | Yes |
| `date_of_birth` | string (date) | Yes |
| `sex` | string | Yes |
| `emergency_contact_name` | string; nullable | No |
| `emergency_contact_phone` | string; nullable | No |
| `notification_preferences` | object | Yes |
| `created_at` | string (date-time) | Yes |
| `updated_at` | string (date-time) | Yes |

### ProfessionalProfilesRead

| Field | Type / validation | Required |
|---|---|---|
| `user_id` | string (uuid) | Yes |
| `role` | string: doctor, pharmacist, lab_staff | Yes |
| `doctor_reference` | string; nullable | No |
| `registration_number` | string; nullable | No |
| `qualifications` | string; nullable | No |
| `specialty` | string; nullable | No |
| `verification_status` | string: not_submitted, pending, approved, rejected, more_info_requested, suspended | Yes |
| `created_at` | string (date-time) | Yes |
| `updated_at` | string (date-time) | Yes |

### VerificationApplicationsRead

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `user_id` | string (uuid) | Yes |
| `organization_id` | string (uuid); nullable | No |
| `role` | string: doctor, pharmacist, lab_staff | Yes |
| `registration_number` | string; nullable | No |
| `status` | string: not_submitted, pending, approved, rejected, more_info_requested, suspended | Yes |
| `submitted_at` | string (date-time) | Yes |
| `reviewed_by` | string (uuid); nullable | No |
| `reviewed_at` | string (date-time); nullable | No |
| `review_reason` | string; nullable | No |
| `evidence_document_ids` | array<string (uuid)> | No |

### VerificationDecisionsRead

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `application_id` | string (uuid) | Yes |
| `reviewer_user_id` | string (uuid) | Yes |
| `decision` | string | Yes |
| `reason` | string | Yes |
| `decided_at` | string (date-time) | Yes |

### AccessGrantsRead

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `patient_user_id` | string (uuid) | Yes |
| `professional_user_id` | string (uuid) | Yes |
| `organization_id` | string (uuid); nullable | No |
| `allow_consultation` | boolean | Yes |
| `status` | string: active, expired, revoked | Yes |
| `starts_at` | string (date-time) | Yes |
| `expires_at` | string (date-time) | Yes |
| `revoked_at` | string (date-time); nullable | No |
| `created_at` | string (date-time) | Yes |

### GrantCategoriesRead

| Field | Type / validation | Required |
|---|---|---|
| `grant_id` | string (uuid) | Yes |
| `category` | string: history_allergies, prescriptions, reports, measurements_vaccinations, medication_logs | Yes |

### ConsultationsRead

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `patient_user_id` | string (uuid) | Yes |
| `doctor_user_id` | string (uuid) | Yes |
| `organization_id` | string (uuid); nullable | No |
| `grant_id` | string (uuid) | Yes |
| `started_at` | string (date-time) | Yes |
| `ended_at` | string (date-time); nullable | No |
| `summary` | string; nullable | No |

### ConditionsRead

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `patient_user_id` | string (uuid) | Yes |
| `consultation_id` | string (uuid); nullable | No |
| `condition_name` | string | Yes |
| `clinical_code` | string; nullable | No |
| `description` | string; nullable | No |
| `source` | string: patient, doctor, pharmacist, lab_staff, approved_external_system, admin | Yes |
| `recorded_by` | string (uuid) | Yes |
| `organization_id` | string (uuid); nullable | No |
| `status` | string: active, entered_in_error, superseded, resolved | Yes |
| `recorded_at` | string (date-time) | Yes |
| `created_at` | string (date-time) | Yes |
| `supersedes_id` | string (uuid); nullable | No |
| `correction_reason` | string; nullable | No |

### DiagnosesRead

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `patient_user_id` | string (uuid) | Yes |
| `consultation_id` | string (uuid); nullable | No |
| `diagnosis_name` | string | Yes |
| `clinical_code` | string; nullable | No |
| `description` | string; nullable | No |
| `source` | string: patient, doctor, pharmacist, lab_staff, approved_external_system, admin | Yes |
| `recorded_by` | string (uuid) | Yes |
| `organization_id` | string (uuid); nullable | No |
| `status` | string: active, entered_in_error, superseded, resolved | Yes |
| `diagnosed_at` | string (date-time) | Yes |
| `created_at` | string (date-time) | Yes |
| `supersedes_id` | string (uuid); nullable | No |
| `correction_reason` | string; nullable | No |

### AllergiesRead

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `patient_user_id` | string (uuid) | Yes |
| `consultation_id` | string (uuid); nullable | No |
| `allergen` | string | Yes |
| `reaction` | string; nullable | No |
| `severity` | string; nullable | No |
| `source` | string: patient, doctor, pharmacist, lab_staff, approved_external_system, admin | Yes |
| `recorded_by` | string (uuid) | Yes |
| `organization_id` | string (uuid); nullable | No |
| `status` | string: active, entered_in_error, superseded, resolved | Yes |
| `recorded_at` | string (date-time) | Yes |
| `created_at` | string (date-time) | Yes |
| `supersedes_id` | string (uuid); nullable | No |
| `correction_reason` | string; nullable | No |

### ClinicalNotesRead

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `patient_user_id` | string (uuid) | Yes |
| `consultation_id` | string (uuid); nullable | No |
| `note_type` | string | Yes |
| `note_text` | string | Yes |
| `source` | string: patient, doctor, pharmacist, lab_staff, approved_external_system, admin | Yes |
| `recorded_by` | string (uuid) | Yes |
| `organization_id` | string (uuid); nullable | No |
| `status` | string: active, entered_in_error, superseded, resolved | Yes |
| `recorded_at` | string (date-time) | Yes |
| `created_at` | string (date-time) | Yes |
| `supersedes_id` | string (uuid); nullable | No |
| `correction_reason` | string; nullable | No |

### RecordCorrectionsRead

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `condition_id` | string (uuid); nullable | No |
| `diagnosis_id` | string (uuid); nullable | No |
| `allergy_id` | string (uuid); nullable | No |
| `clinical_note_id` | string (uuid); nullable | No |
| `requested_by` | string (uuid) | Yes |
| `reason` | string | Yes |
| `request_status` | string | Yes |
| `reviewed_by` | string (uuid); nullable | No |
| `reviewed_at` | string (date-time); nullable | No |
| `resolution_reason` | string; nullable | No |
| `replacement_record_type` | string; nullable | No |
| `replacement_record_id` | string (uuid); nullable | No |
| `created_at` | string (date-time) | Yes |

### MedicinesRead

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `generic_name` | string | Yes |
| `brand_name` | string; nullable | No |
| `strength` | string; nullable | No |
| `form` | string; nullable | No |
| `is_sample` | boolean | Yes |
| `created_at` | string (date-time) | Yes |

### DocumentsRead

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `owner_user_id` | string (uuid) | Yes |
| `patient_user_id` | string (uuid); nullable | No |
| `uploaded_by` | string (uuid); nullable | No |
| `source` | string: patient, doctor, pharmacist, lab_staff, approved_external_system, admin | Yes |
| `document_type` | string | Yes |
| `original_filename` | string | Yes |
| `media_type` | string | Yes |
| `byte_size` | integer | Yes |
| `sha256` | string; nullable | No |
| `status` | string: active, superseded, withdrawn, quarantined | Yes |
| `created_at` | string (date-time) | Yes |
| `supersedes_id` | string (uuid); nullable | No |

### VerificationApplicationDocumentsRead

| Field | Type / validation | Required |
|---|---|---|
| `application_id` | string (uuid) | Yes |
| `document_id` | string (uuid) | Yes |
| `attached_by` | string (uuid) | Yes |
| `attached_at` | string (date-time) | Yes |

### PrescriptionsRead

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `patient_user_id` | string (uuid) | Yes |
| `consultation_id` | string (uuid); nullable | No |
| `prescriber_user_id` | string (uuid); nullable | No |
| `source` | string: patient, doctor, approved_external_system | Yes |
| `claimed_prescriber` | string; nullable | No |
| `review_status` | string: not_applicable, not_reviewed, reviewed, approved, needs_clarification, not_accepted | Yes |
| `reviewed_by` | string (uuid); nullable | No |
| `reviewed_at` | string (date-time); nullable | No |
| `status` | string: draft, active, completed, discontinued, cancelled, superseded | Yes |
| `issued_at` | string (date-time); nullable | No |
| `starts_on` | string (date); nullable | No |
| `ends_on` | string (date); nullable | No |
| `previous_prescription_id` | string (uuid); nullable | No |
| `original_document_id` | string (uuid); nullable | No |
| `created_by` | string (uuid) | Yes |
| `created_at` | string (date-time) | Yes |
| `updated_at` | string (date-time) | Yes |
| `version` | integer | Yes |
| `status_reason` | string; nullable | No |

### PrescriptionItemsRead

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `prescription_id` | string (uuid) | Yes |
| `medicine_id` | string (uuid); nullable | No |
| `medicine_name_snapshot` | string | Yes |
| `dose_text` | string | Yes |
| `route` | string; nullable | No |
| `frequency` | string | Yes |
| `duration_days` | integer; nullable | No |
| `instructions` | string; nullable | No |
| `quantity` | number; nullable | No |
| `quantity_unit` | string; nullable | No |
| `is_prn` | boolean | Yes |
| `created_at` | string (date-time) | Yes |

### PrescriptionOcrJobsRead

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `patient_user_id` | string (uuid) | Yes |
| `original_document_id` | string (uuid) | Yes |
| `requested_by_user_id` | string (uuid) | Yes |
| `status` | string: queued, processing, ready_for_review, failed, draft_created | Yes |
| `extracted_fields` | object; nullable | No |
| `attempt_count` | integer | Yes |
| `error_code` | string; nullable | No |
| `resulting_prescription_id` | string (uuid); nullable | No |
| `created_at` | string (date-time) | Yes |
| `updated_at` | string (date-time) | Yes |

### PrescriptionTemplatesRead

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `doctor_user_id` | string (uuid) | Yes |
| `name` | string | Yes |
| `template_type` | string: favourite, template | Yes |
| `status` | string: active, archived | Yes |
| `created_at` | string (date-time) | Yes |
| `updated_at` | string (date-time) | Yes |
| `archived_at` | string (date-time); nullable | No |

### PrescriptionTemplateItemsRead

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `template_id` | string (uuid) | Yes |
| `position` | integer | Yes |
| `medicine_id` | string (uuid); nullable | No |
| `medicine_name_snapshot` | string | Yes |
| `dose_text` | string | Yes |
| `route` | string; nullable | No |
| `frequency` | string | Yes |
| `duration_days` | integer; nullable | No |
| `instructions` | string; nullable | No |
| `quantity` | number; nullable | No |
| `quantity_unit` | string; nullable | No |
| `is_prn` | boolean | Yes |
| `created_at` | string (date-time) | Yes |

### PrescriptionReviewsRead

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `prescription_id` | string (uuid) | Yes |
| `reviewer_user_id` | string (uuid) | Yes |
| `outcome` | string: reviewed, approved, needs_clarification, not_accepted | Yes |
| `notes` | string; nullable | No |
| `reviewed_at` | string (date-time) | Yes |

### MedicationSchedulesRead

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `prescription_item_id` | string (uuid) | Yes |
| `patient_user_id` | string (uuid) | Yes |
| `timezone` | string | Yes |
| `starts_at` | string (date-time) | Yes |
| `ends_at` | string (date-time); nullable | No |
| `confirmed_at` | string (date-time) | Yes |
| `status` | string: active, paused, completed, cancelled | Yes |
| `created_at` | string (date-time) | Yes |
| `version` | integer | Yes |

### ScheduleTimesRead

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `schedule_id` | string (uuid) | Yes |
| `time_of_day` | string; pattern=^\d{2}:\d{2}:\d{2}$ | Yes |

### DoseOccurrencesRead

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `schedule_id` | string (uuid) | Yes |
| `scheduled_for` | string (date-time) | Yes |
| `status` | string: upcoming, due, taken, skipped, unreported, cancelled | Yes |
| `snoozed_until` | string (date-time); nullable | No |
| `created_at` | string (date-time) | Yes |

### MedicationLogsRead

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `dose_occurrence_id` | string (uuid) | Yes |
| `response` | string: taken, skipped | Yes |
| `reported_taken_at` | string (date-time); nullable | No |
| `recorded_by` | string (uuid) | Yes |
| `recorded_at` | string (date-time) | Yes |
| `correction_of_id` | string (uuid); nullable | No |
| `correction_reason` | string; nullable | No |

### PrescriptionSharesRead

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `prescription_id` | string (uuid) | Yes |
| `patient_user_id` | string (uuid) | Yes |
| `status` | string: created, claimed, confirmed, expired, revoked | Yes |
| `claim_expires_at` | string (date-time) | Yes |
| `claimed_by` | string (uuid); nullable | No |
| `claimed_organization_id` | string (uuid); nullable | No |
| `claimed_at` | string (date-time); nullable | No |
| `confirmed_at` | string (date-time); nullable | No |
| `confirmed_access_expires_at` | string (date-time); nullable | No |
| `revoked_at` | string (date-time); nullable | No |
| `created_at` | string (date-time) | Yes |

### DispensingEventsRead

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `prescription_id` | string (uuid) | Yes |
| `pharmacist_user_id` | string (uuid) | Yes |
| `organization_id` | string (uuid) | Yes |
| `share_id` | string (uuid); nullable | No |
| `dispensed_at` | string (date-time) | Yes |
| `notes` | string; nullable | No |
| `reversal_of_id` | string (uuid); nullable | No |
| `reason` | string; nullable | No |
| `created_at` | string (date-time) | Yes |

### DispensingItemsRead

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `event_id` | string (uuid) | Yes |
| `prescription_item_id` | string (uuid) | Yes |
| `quantity` | number | Yes |
| `quantity_unit` | string | Yes |

### LabUploadRequestsRead

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `patient_user_id` | string (uuid) | Yes |
| `laboratory_id` | string (uuid) | Yes |
| `expires_at` | string (date-time) | Yes |
| `status` | string: active, used, expired, revoked | Yes |
| `created_at` | string (date-time) | Yes |
| `used_at` | string (date-time); nullable | No |

### ReportIdentitiesRead

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `patient_user_id` | string (uuid) | Yes |
| `laboratory_id` | string (uuid) | Yes |
| `lab_reference` | string | Yes |
| `initial_upload_request_id` | string (uuid) | Yes |
| `created_at` | string (date-time) | Yes |

### ReportsRead

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `patient_user_id` | string (uuid) | Yes |
| `laboratory_id` | string (uuid); nullable | No |
| `report_identity_id` | string (uuid); nullable | No |
| `upload_request_id` | string (uuid); nullable | No |
| `document_id` | string (uuid) | Yes |
| `report_type` | string | Yes |
| `report_date` | string (date); nullable | No |
| `notes` | string; nullable | No |
| `source` | string: patient, doctor, pharmacist, lab_staff, approved_external_system, admin | Yes |
| `uploaded_by` | string (uuid); nullable | No |
| `status` | string: active, superseded, withdrawn | Yes |
| `created_at` | string (date-time) | Yes |
| `supersedes_id` | string (uuid); nullable | No |
| `correction_reason` | string; nullable | No |

### MeasurementsRead

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `patient_user_id` | string (uuid) | Yes |
| `type` | string: weight, blood_pressure, blood_glucose | Yes |
| `value` | positive number; required | Yes |
| `secondary_value` | number; nullable | No |
| `unit` | string | Yes |
| `measured_at` | string (date-time) | Yes |
| `recorded_by` | string (uuid) | Yes |
| `source` | string: patient, doctor, pharmacist, lab_staff, approved_external_system, admin | Yes |
| `notes` | string; nullable | No |
| `status` | string: active, superseded, entered_in_error | Yes |
| `supersedes_id` | string (uuid); nullable | No |
| `correction_reason` | string; nullable | No |
| `created_at` | string (date-time) | Yes |

### VaccinationsRead

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `patient_user_id` | string (uuid) | Yes |
| `vaccine_name` | string | Yes |
| `administered_on` | string (date) | Yes |
| `dose_details` | string; nullable | No |
| `next_dose_on` | string (date); nullable | No |
| `recorded_by` | string (uuid) | Yes |
| `source` | string: patient, doctor, pharmacist, lab_staff, approved_external_system, admin | Yes |
| `organization_id` | string (uuid); nullable | No |
| `notes` | string; nullable | No |
| `created_at` | string (date-time) | Yes |
| `supersedes_id` | string (uuid); nullable | No |
| `correction_reason` | string; nullable | No |

### NotificationsRead

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `user_id` | string (uuid) | Yes |
| `notification_type` | string | Yes |
| `title` | string | Yes |
| `body` | string | Yes |
| `related_type` | string; nullable | No |
| `related_id` | string (uuid); nullable | No |
| `created_at` | string (date-time) | Yes |
| `read_at` | string (date-time); nullable | No |

### JobRunsRead

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `job_key` | string | Yes |
| `job_type` | string | Yes |
| `status` | string | Yes |
| `attempt_count` | integer | Yes |
| `started_at` | string (date-time); nullable | No |
| `finished_at` | string (date-time); nullable | No |
| `last_error_code` | string; nullable | No |
| `created_at` | string (date-time) | Yes |

### BackupRunsRead

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `status` | string | Yes |
| `database_included` | boolean | Yes |
| `files_included` | boolean | Yes |
| `started_at` | string (date-time) | Yes |
| `finished_at` | string (date-time); nullable | No |
| `restore_check_status` | string | Yes |
| `restore_checked_at` | string (date-time); nullable | No |
| `error_code` | string; nullable | No |

### ApiIdempotencyRecordsRead

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `actor_type` | string: user, client | Yes |
| `actor_id` | string (uuid) | Yes |
| `operation_key` | string | Yes |
| `target_key` | string | Yes |
| `idempotency_key` | string | Yes |
| `request_digest` | string | Yes |
| `response_status` | integer | Yes |
| `response_body` | object | Yes |
| `created_at` | string (date-time) | Yes |
| `expires_at` | string (date-time) | Yes |

### IntegrationClientsRead

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `organization_id` | string (uuid) | Yes |
| `name` | string | Yes |
| `scopes` | array<string> | Yes |
| `status` | string: pending, active, suspended, revoked, expired | Yes |
| `expires_at` | string (date-time) | Yes |
| `created_by` | string (uuid) | Yes |
| `created_at` | string (date-time) | Yes |
| `revoked_at` | string (date-time); nullable | No |

### IntegrationAdminSubmissionRead

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `client_id` | string (uuid) | Yes |
| `upload_request_id` | string (uuid) | Yes |
| `external_report_id` | string | Yes |
| `status` | string: received, processing, accepted, rejected, failed | Yes |
| `report_id` | string (uuid); nullable | No |
| `received_at` | string (date-time) | Yes |
| `error_code` | string; nullable | No |

This sanitized schema is used only by the Super Admin integration-submission listing. It intentionally omits the client-supplied `idempotency_key` and report payload.

### IntegrationSubmissionsRead

This response schema extends `IntegrationAdminSubmissionRead` with the idempotency key from the request, for use by report submission and correction responses.

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `client_id` | string (uuid) | Yes |
| `upload_request_id` | string (uuid) | Yes |
| `external_report_id` | string | Yes |
| `status` | string: received, processing, accepted, rejected, failed | Yes |
| `report_id` | string (uuid); nullable | No |
| `received_at` | string (date-time) | Yes |
| `error_code` | string; nullable | No |
| `idempotency_key` | string | Yes |

### AuditEventsRead

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `actor_user_id` | string (uuid); nullable | No |
| `actor_client_id` | string (uuid); nullable | No |
| `action` | string | Yes |
| `target_type` | string | Yes |
| `target_id` | string (uuid); nullable | No |
| `outcome` | string | Yes |
| `request_id` | string | Yes |
| `metadata` | object | Yes |
| `created_at` | string (date-time) | Yes |

### UserSummary

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `display_name` | string | Yes |

### PatientIdentity

| Field | Type / validation | Required |
|---|---|---|
| `patient_user_id` | string (uuid) | Yes |
| `display_name` | string | Yes |
| `date_of_birth` | string (date); nullable | No |

### ProfessionalIdentity

| Field | Type / validation | Required |
|---|---|---|
| `user_id` | string (uuid) | Yes |
| `display_name` | string | Yes |
| `role` | string: doctor, pharmacist, lab_staff | Yes |
| `verification_status` | string | Yes |
| `organization_id` | string (uuid) | No |
| `organization_name` | string | No |

### ActionResult

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `status` | string | Yes |
| `updated_at` | string (date-time) | No |

### ReasonInput

| Field | Type / validation | Required |
|---|---|---|
| `reason` | string; minLength=1; maxLength=1000 | Yes |

### Error

| Field | Type / validation | Required |
|---|---|---|
| `code` | string | Yes |
| `message` | string | Yes |
| `field_errors` | object | Yes |
| `request_id` | string | Yes |

### Me

| Field | Type / validation | Required |
|---|---|---|
| `user` | UsersRead | Yes |
| `roles` | array<string: patient, doctor, pharmacist, lab_staff, admin, super_admin> | Yes |
| `patient_profile` | PatientProfilesRead | No |
| `professional_profile` | ProfessionalProfilesRead | No |
| `memberships` | array<OrganizationMembershipsRead> | Yes |

### OnboardInput

| Field | Type / validation | Required |
|---|---|---|
| `display_name` | string; minLength=1; maxLength=150 | Yes |
| `phone` | string; maxLength=32 | No |
| `requested_role` | string: patient, doctor, pharmacist, lab_staff | Yes |

### ProfileInput

| Field | Type / validation | Required |
|---|---|---|
| `display_name` | string; minLength=1; maxLength=150 | No |
| `phone` | string; maxLength=32 | No |

### PatientProfileInput

| Field | Type / validation | Required |
|---|---|---|
| `date_of_birth` | string (date) | No |
| `sex` | string: female, male, intersex, another, prefer_not_to_say | No |
| `emergency_contact_name` | string; maxLength=150 | No |
| `emergency_contact_phone` | string; maxLength=32 | No |
| `notification_preferences` | object | No |

### ProfessionalProfileInput

| Field | Type / validation | Required |
|---|---|---|
| `role` | string: doctor, pharmacist, lab_staff | Yes |
| `registration_number` | string; maxLength=100 | No |
| `qualifications` | string; maxLength=2000 | No |
| `specialty` | string; maxLength=150 | No |

### OrganizationInput

| Field | Type / validation | Required |
|---|---|---|
| `name` | string; minLength=1; maxLength=200 | Yes |
| `type` | string: clinic, hospital, pharmacy, laboratory, other | Yes |
| `registration_ref` | string; maxLength=100 | No |

### MembershipInput

| Field | Type / validation | Required |
|---|---|---|
| `organization_id` | string (uuid) | Yes |
| `role` | string: doctor, pharmacist, lab_staff | Yes |

### VerificationInput

| Field | Type / validation | Required |
|---|---|---|
| `organization_id` | string (uuid) | No |
| `role` | string: doctor, pharmacist, lab_staff | Yes |
| `registration_number` | string; minLength=1; maxLength=100 | Yes |
| `evidence_document_ids` | array<string (uuid)> | Yes |

### VerificationDecision

| Field | Type / validation | Required |
|---|---|---|
| `status` | string: approved, rejected, more_info_requested | Yes |
| `reason` | string; minLength=1; maxLength=1000 | Yes |

### OrganizationDecision

| Field | Type / validation | Required |
|---|---|---|
| `status` | string: active, suspended, rejected, inactive | Yes |
| `reason` | string; minLength=1; maxLength=1000 | Yes |

### MembershipDecision

| Field | Type / validation | Required |
|---|---|---|
| `status` | string: approved, rejected, more_info_requested, suspended | Yes |
| `reason` | string; minLength=1; maxLength=1000 | Yes |

### AccountDecision

| Field | Type / validation | Required |
|---|---|---|
| `status` | string: active, suspended, disabled | Yes |
| `reason` | string; minLength=1; maxLength=1000 | Yes |

### AdminRoleInput

| Field | Type / validation | Required |
|---|---|---|
| `user_id` | string (uuid) | Yes |
| `role` | string: admin | Yes |
| `reason` | string; minLength=1; maxLength=1000 | Yes |

### DoctorQr

| Field | Type / validation | Required |
|---|---|---|
| `doctor_reference` | string | Yes |
| `qr_payload` | string | Yes |

### DoctorResolve

| Field | Type / validation | Required |
|---|---|---|
| `doctor_reference` | string; minLength=1; maxLength=256 | Yes |

### GrantInput

| Field | Type / validation | Required |
|---|---|---|
| `doctor_reference` | string | Yes |
| `organization_id` | string (uuid) | No |
| `categories` | array<string: history_allergies, prescriptions, reports, measurements_vaccinations, medication_logs> | Yes |
| `duration_hours` | integer: 24, 168, 720 | Yes |
| `allow_consultation` | boolean | Yes |

### GrantDetail

| Field | Type / validation | Required |
|---|---|---|
| `grant` | AccessGrantsRead | Yes |
| `categories` | array<string: history_allergies, prescriptions, reports, measurements_vaccinations, medication_logs> | Yes |

### QueueEntry

| Field | Type / validation | Required |
|---|---|---|
| `patient` | PatientIdentity | Yes |
| `grant_id` | string (uuid) | Yes |
| `categories` | array<string> | Yes |
| `expires_at` | string (date-time) | Yes |
| `allow_consultation` | boolean | Yes |

### ConsultationInput

| Field | Type / validation | Required |
|---|---|---|
| `grant_id` | string (uuid) | Yes |

### ConsultationEnd

| Field | Type / validation | Required |
|---|---|---|
| `summary` | string; maxLength=4000 | No |

### ClinicalInput

| Field | Type / validation | Required |
|---|---|---|
| `consultation_id` | string (uuid) | No |
| `type` | string: condition, diagnosis, allergy, note | Yes |
| `title` | string; minLength=1; maxLength=200 | Yes |
| `description` | string; maxLength=8000 | No |
| `clinical_code` | string; maxLength=100 | No |
| `recorded_at` | string (date-time) | Yes |

### ClinicalRecordRead

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `patient_user_id` | string (uuid) | Yes |
| `consultation_id` | string (uuid) | No |
| `type` | string: condition, diagnosis, allergy, note | Yes |
| `title` | string | Yes |
| `description` | string | No |
| `clinical_code` | string | No |
| `source` | string: patient, doctor, pharmacist, lab_staff, approved_external_system, admin | Yes |
| `recorded_by` | string (uuid) | Yes |
| `status` | string: active, entered_in_error, superseded, resolved | Yes |
| `recorded_at` | string (date-time) | Yes |
| `created_at` | string (date-time) | Yes |
| `supersedes_id` | string (uuid) | No |
| `correction_reason` | string | No |

### ClinicalVersionInput

| Field | Type / validation | Required |
|---|---|---|
| `consultation_id` | string (uuid) | No |
| `type` | string: condition, diagnosis, allergy, note | Yes |
| `title` | string; minLength=1; maxLength=200 | Yes |
| `description` | string; maxLength=8000 | No |
| `clinical_code` | string; maxLength=100 | No |
| `recorded_at` | string (date-time) | Yes |
| `reason` | string; minLength=1; maxLength=1000 | Yes |

### CorrectionDecision

| Field | Type / validation | Required |
|---|---|---|
| `request_status` | string: accepted, rejected, resolved | Yes |
| `reason` | string; minLength=1; maxLength=1000 | Yes |
| `replacement_record_id` | string (uuid) | No |

### MedicineInput

| Field | Type / validation | Required |
|---|---|---|
| `generic_name` | string; minLength=1; maxLength=200 | Yes |
| `brand_name` | string; maxLength=200 | No |
| `strength` | string; maxLength=100 | No |
| `form` | string; maxLength=100 | No |

### PrescriptionItemInput

| Field | Type / validation | Required |
|---|---|---|
| `medicine_id` | string (uuid) | No |
| `medicine_name_snapshot` | string; minLength=1; maxLength=200 | Yes |
| `dose_text` | string; minLength=1; maxLength=200 | Yes |
| `route` | string; maxLength=100 | No |
| `frequency` | string; minLength=1; maxLength=200 | Yes |
| `duration_days` | integer; minimum=1 | No |
| `instructions` | string; maxLength=2000 | No |
| `quantity` | number; exclusiveMinimum=0 | No |
| `quantity_unit` | string: tablet, capsule, mL | No |
| `is_prn` | boolean | Yes |

### PrescriptionInput

| Field | Type / validation | Required |
|---|---|---|
| `consultation_id` | string (uuid) | No |
| `claimed_prescriber` | string; maxLength=200 | No |
| `starts_on` | string (date) | No |
| `ends_on` | string (date) | No |
| `original_document_id` | string (uuid) | No |
| `items` | array<PrescriptionItemInput> | Yes |

### PrescriptionVersionInput

| Field | Type / validation | Required |
|---|---|---|
| `consultation_id` | string (uuid) | No |
| `claimed_prescriber` | string; maxLength=200 | No |
| `starts_on` | string (date) | No |
| `ends_on` | string (date) | No |
| `original_document_id` | string (uuid) | No |
| `items` | array<PrescriptionItemInput> | Yes |
| `reason` | string; minLength=1; maxLength=1000 | Yes |

### PrescriptionReviewInput

| Field | Type / validation | Required |
|---|---|---|
| `outcome` | string: reviewed, approved, needs_clarification, not_accepted | Yes |
| `notes` | string; maxLength=2000 | No |

### PrescriptionDetail

| Field | Type / validation | Required |
|---|---|---|
| `prescription` | PrescriptionsRead | Yes |
| `items` | array<PrescriptionItemsRead> | Yes |
| `reviews` | array<PrescriptionReviewsRead> | Yes |

### ScheduleInput

| Field | Type / validation | Required |
|---|---|---|
| `prescription_item_id` | string (uuid) | Yes |
| `timezone` | string | Yes |
| `starts_at` | string (date-time) | Yes |
| `ends_at` | string (date-time) | No |
| `times` | array<string; pattern=^([01]\d\|2[0-3]):[0-5]\d:[0-5]\d$> | Yes |
| `confirmed` | boolean: True | Yes |

### ScheduleDetail

| Field | Type / validation | Required |
|---|---|---|
| `schedule` | MedicationSchedulesRead | Yes |
| `times` | array<ScheduleTimesRead> | Yes |

### ScheduleAction

| Field | Type / validation | Required |
|---|---|---|
| `status` | string: active, paused, completed, cancelled | Yes |
| `reason` | string; minLength=1; maxLength=1000 | Yes |

### SnoozeInput

| Field | Type / validation | Required |
|---|---|---|
| `snoozed_until` | string (date-time) | Yes |

### DoseResponseInput

| Field | Type / validation | Required |
|---|---|---|
| `response` | string: taken, skipped | Yes |
| `reported_taken_at` | string (date-time) | No |

### DoseCorrectionInput

| Field | Type / validation | Required |
|---|---|---|
| `response` | string: taken, skipped | Yes |
| `reported_taken_at` | string (date-time) | No |
| `correction_reason` | string; minLength=1; maxLength=1000 | Yes |

### Adherence

| Field | Type / validation | Required |
|---|---|---|
| `period_from` | string (date-time) | Yes |
| `period_to` | string (date-time) | Yes |
| `taken` | integer | Yes |
| `skipped` | integer | Yes |
| `unreported` | integer | Yes |
| `eligible` | integer | Yes |
| `percentage` | number; minimum=0; maximum=100; nullable | Yes |
| `label` | string: Patient-reported adherence | Yes |

### ShareInput

| Field | Type / validation | Required |
|---|---|---|
| `prescription_id` | string (uuid) | Yes |

### ClaimInput

| Field | Type / validation | Required |
|---|---|---|
| `claim_token` | string; minLength=1; maxLength=256 | Yes |
| `organization_id` | string (uuid) | Yes |

### ShareCreated

| Field | Type / validation | Required |
|---|---|---|
| `share` | PrescriptionSharesRead | Yes |
| `claim_token` | string | Yes |
| `claim_expires_at` | string (date-time) | Yes |

### ShareDetail

| Field | Type / validation | Required |
|---|---|---|
| `share` | PrescriptionSharesRead | Yes |
| `pharmacist` | ProfessionalIdentity | No |
| `access_expires_at` | string (date-time); nullable | Yes |

### DispensableItem

| Field | Type / validation | Required |
|---|---|---|
| `item` | PrescriptionItemsRead | Yes |
| `dispensed_quantity` | number | Yes |
| `remaining_quantity` | number | Yes |

### PharmacyPrescription

| Field | Type / validation | Required |
|---|---|---|
| `prescription_id` | string (uuid) | Yes |
| `patient` | PatientIdentity | Yes |
| `issuer` | ProfessionalIdentity | Yes |
| `source` | string: doctor | Yes |
| `status` | string: active | Yes |
| `items` | array<DispensableItem> | Yes |

### DispenseItemInput

| Field | Type / validation | Required |
|---|---|---|
| `prescription_item_id` | string (uuid) | Yes |
| `quantity` | number; exclusiveMinimum=0 | Yes |
| `quantity_unit` | string: tablet, capsule, mL | Yes |

### DispenseInput

| Field | Type / validation | Required |
|---|---|---|
| `share_id` | string (uuid) | Yes |
| `organization_id` | string (uuid) | Yes |
| `items` | array<DispenseItemInput> | Yes |
| `notes` | string; maxLength=1000 | No |

### DispenseDetail

| Field | Type / validation | Required |
|---|---|---|
| `event` | DispensingEventsRead | Yes |
| `items` | array<DispensingItemsRead> | Yes |

### DispenseHistory

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `prescription_reference` | string (uuid) | Yes |
| `organization_id` | string (uuid) | Yes |
| `dispensed_at` | string (date-time) | Yes |
| `reversal_of_id` | string (uuid); nullable | No |
| `items` | array<DispensingItemsRead> | Yes |

### LabRequestInput

| Field | Type / validation | Required |
|---|---|---|
| `laboratory_id` | string (uuid) | Yes |

### LabRequestCreated

| Field | Type / validation | Required |
|---|---|---|
| `request` | LabUploadRequestsRead | Yes |
| `request_token` | string | Yes |

### LabResolveInput

| Field | Type / validation | Required |
|---|---|---|
| `request_token` | string; minLength=1; maxLength=256 | Yes |
| `laboratory_id` | string (uuid) | Yes |

### LabRequestResolved

| Field | Type / validation | Required |
|---|---|---|
| `request_id` | string (uuid) | Yes |
| `patient` | PatientIdentity | Yes |
| `laboratory_id` | string (uuid) | Yes |
| `expires_at` | string (date-time) | Yes |

### ReportInput

| Field | Type / validation | Required |
|---|---|---|
| `upload_request_id` | string (uuid) | No |
| `document_id` | string (uuid) | Yes |
| `report_type` | string; minLength=1; maxLength=200 | Yes |
| `report_date` | string (date) | No |
| `lab_reference` | string; maxLength=150 | No |
| `notes` | string; maxLength=2000 | No |

### ReportVersionInput

| Field | Type / validation | Required |
|---|---|---|
| `upload_request_id` | string (uuid) | No |
| `document_id` | string (uuid) | Yes |
| `report_type` | string; minLength=1; maxLength=200 | Yes |
| `report_date` | string (date) | No |
| `lab_reference` | string; maxLength=150 | No |
| `notes` | string; maxLength=2000 | No |
| `reason` | string; minLength=1; maxLength=1000 | Yes |

### DocumentUpload

| Field | Type / validation | Required |
|---|---|---|
| `file` | string (binary) | Yes |
| `document_type` | string: prescription, report, verification_evidence | Yes |
| `upload_request_id` | string (uuid) | No |
| `verification_application_id` | string (uuid) | No |
| `supersedes_id` | string (uuid) | No |

### PrescriptionOcrJobInput

| Field | Type / validation | Required |
|---|---|---|
| `original_document_id` | string (uuid) | Yes |

### PrescriptionOcrJobRead

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `patient_user_id` | string (uuid) | Yes |
| `original_document_id` | string (uuid) | Yes |
| `status` | string: queued, processing, ready_for_review, failed, draft_created | Yes |
| `extracted_fields` | PrescriptionOcrFields | No |
| `attempt_count` | integer | Yes |
| `error_code` | string; nullable | No |
| `resulting_prescription_id` | string (uuid); nullable | No |
| `created_at` | string (date-time) | Yes |
| `updated_at` | string (date-time) | Yes |

### PrescriptionOcrFields

| Field | Type / validation | Required |
|---|---|---|
| `claimed_prescriber` | string; nullable | No |
| `starts_on` | string (date); nullable | No |
| `ends_on` | string (date); nullable | No |
| `items` | array<PrescriptionItemInput> | No |

### PrescriptionOcrDraftInput

| Field | Type / validation | Required |
|---|---|---|
| `claimed_prescriber` | string; maxLength=200 | No |
| `starts_on` | string (date) | No |
| `ends_on` | string (date) | No |
| `items` | array<PrescriptionItemInput> | Yes |

### PrescriptionOcrDraftCreated

| Field | Type / validation | Required |
|---|---|---|
| `job` | PrescriptionOcrJobRead | Yes |
| `prescription` | PrescriptionDetail | Yes |

### PrescriptionTemplateInput

| Field | Type / validation | Required |
|---|---|---|
| `name` | string; minLength=1; maxLength=120 | Yes |
| `template_type` | string: favourite, template | Yes |
| `items` | array<PrescriptionItemInput> | Yes |

### PrescriptionTemplateRead

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `doctor_user_id` | string (uuid) | Yes |
| `name` | string | Yes |
| `template_type` | string: favourite, template | Yes |
| `status` | string: active, archived | Yes |
| `items` | array<PrescriptionItemInput> | Yes |
| `created_at` | string (date-time) | Yes |
| `updated_at` | string (date-time) | Yes |

### PrescriptionTemplateDetail

| Field | Type / validation | Required |
|---|---|---|
| `template` | PrescriptionTemplateRead | Yes |

### PrescriptionTemplatePatch

| Field | Type / validation | Required |
|---|---|---|
| `name` | string; minLength=1; maxLength=120 | No |
| `items` | array<PrescriptionItemInput> | No |

### MeasurementInput

| Field | Type / validation | Required |
|---|---|---|
| `consultation_id` | string (uuid) | No |
| `type` | string: weight, blood_pressure, blood_glucose | Yes |
| `value` | number; exclusiveMinimum=0 | Yes |
| `secondary_value` | number; exclusiveMinimum=0 | No |
| `unit` | string: kg, lb, mmol/L, mg/dL, mmHg | Yes |
| `measured_at` | string (date-time) | Yes |
| `notes` | string; maxLength=2000 | No |

### VaccinationInput

| Field | Type / validation | Required |
|---|---|---|
| `consultation_id` | string (uuid) | Yes |
| `vaccine_name` | string; minLength=1; maxLength=200 | Yes |
| `administered_on` | string (date) | Yes |
| `dose_details` | string; maxLength=200 | No |
| `next_dose_on` | string (date) | No |
| `notes` | string; maxLength=2000 | No |

### VaccinationVersionInput

| Field | Type / validation | Required |
|---|---|---|
| `consultation_id` | string (uuid) | Yes |
| `vaccine_name` | string; minLength=1; maxLength=200 | Yes |
| `administered_on` | string (date) | Yes |
| `dose_details` | string; maxLength=200 | No |
| `next_dose_on` | string (date) | No |
| `notes` | string; maxLength=2000 | No |
| `reason` | string; minLength=1; maxLength=1000 | Yes |

### IntegrationClientInput

| Field | Type / validation | Required |
|---|---|---|
| `organization_id` | string (uuid) | Yes |
| `name` | string; minLength=1; maxLength=200 | Yes |
| `scopes` | array<string: reports:write> | Yes |
| `expires_at` | string (date-time) | Yes |

### CredentialCreated

| Field | Type / validation | Required |
|---|---|---|
| `client` | IntegrationClientsRead | Yes |
| `credential` | string | Yes |

### RotateCredential

| Field | Type / validation | Required |
|---|---|---|
| `expires_at` | string (date-time) | Yes |
| `reason` | string; minLength=1; maxLength=1000 | Yes |

### IntegrationReportUpload

| Field | Type / validation | Required |
|---|---|---|
| `file` | string (binary) | Yes |
| `request_token` | string; minLength=1; maxLength=256 | Yes |
| `external_report_id` | string; minLength=1; maxLength=150 | Yes |
| `report_type` | string; minLength=1; maxLength=200 | Yes |
| `report_date` | string (date) | No |
| `lab_reference` | string; minLength=1; maxLength=150 | Yes |
| `notes` | string; maxLength=2000 | No |

### OperationalSummary

| Field | Type / validation | Required |
|---|---|---|
| `pending_verifications` | integer | Yes |
| `active_organizations` | integer | Yes |
| `failed_jobs` | integer | Yes |
| `last_backup_at` | string (date-time); nullable | Yes |

### JobStatus

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `job_type` | string | Yes |
| `status` | string: pending, running, succeeded, failed | Yes |
| `attempt_count` | integer | Yes |
| `error_code` | string; nullable | No |
| `updated_at` | string (date-time) | Yes |

### FhirPatient

| Field | Type / validation | Required |
|---|---|---|
| `resourceType` | string: Patient | Yes |
| `id` | string | Yes |
| `identifier` | array<object> | Yes |
| `name` | array<object> | Yes |
| `birthDate` | string (date) | No |

### FhirMedicationRequest

| Field | Type / validation | Required |
|---|---|---|
| `resourceType` | string: MedicationRequest | Yes |
| `id` | string | Yes |
| `status` | string: active, completed, stopped, cancelled, draft, entered-in-error | Yes |
| `intent` | string: order | Yes |
| `subject` | object | Yes |
| `medicationCodeableConcept` | object | Yes |
| `authoredOn` | string (date-time) | Yes |
| `dosageInstruction` | array<object> | Yes |

### IntegrationReportVersionUpload

| Field | Type / validation | Required |
|---|---|---|
| `file` | string (binary) | Yes |
| `request_token` | string; minLength=1; maxLength=256 | Yes |
| `external_report_id` | string; minLength=1; maxLength=150 | Yes |
| `report_type` | string; minLength=1; maxLength=200 | Yes |
| `report_date` | string (date) | No |
| `lab_reference` | string; minLength=1; maxLength=150 | Yes |
| `notes` | string; maxLength=2000 | No |
| `reason` | string; minLength=1; maxLength=1000 | Yes |

### OrganizationPublic

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `name` | string | Yes |
| `type` | string: clinic, hospital, pharmacy, laboratory, other | Yes |

### PatientActivity

| Field | Type / validation | Required |
|---|---|---|
| `id` | string (uuid) | Yes |
| `action` | string | Yes |
| `target_type` | string | Yes |
| `target_id` | string (uuid); nullable | No |
| `outcome` | string | Yes |
| `created_at` | string (date-time) | Yes |

### OrganizationPublicPage

| Field | Type / validation | Required |
|---|---|---|
| `items` | array<OrganizationPublic> | Yes |
| `page` | integer | Yes |
| `page_size` | integer | Yes |
| `total` | integer | Yes |

### OrganizationsReadPage

| Field | Type / validation | Required |
|---|---|---|
| `items` | array<OrganizationsRead> | Yes |
| `page` | integer | Yes |
| `page_size` | integer | Yes |
| `total` | integer | Yes |

### OrganizationMembershipsReadPage

| Field | Type / validation | Required |
|---|---|---|
| `items` | array<OrganizationMembershipsRead> | Yes |
| `page` | integer | Yes |
| `page_size` | integer | Yes |
| `total` | integer | Yes |

### VerificationApplicationsReadPage

| Field | Type / validation | Required |
|---|---|---|
| `items` | array<VerificationApplicationsRead> | Yes |
| `page` | integer | Yes |
| `page_size` | integer | Yes |
| `total` | integer | Yes |

### GrantDetailPage

| Field | Type / validation | Required |
|---|---|---|
| `items` | array<GrantDetail> | Yes |
| `page` | integer | Yes |
| `page_size` | integer | Yes |
| `total` | integer | Yes |

### QueueEntryPage

| Field | Type / validation | Required |
|---|---|---|
| `items` | array<QueueEntry> | Yes |
| `page` | integer | Yes |
| `page_size` | integer | Yes |
| `total` | integer | Yes |

### ConsultationsReadPage

| Field | Type / validation | Required |
|---|---|---|
| `items` | array<ConsultationsRead> | Yes |
| `page` | integer | Yes |
| `page_size` | integer | Yes |
| `total` | integer | Yes |

### ClinicalRecordReadPage

| Field | Type / validation | Required |
|---|---|---|
| `items` | array<ClinicalRecordRead> | Yes |
| `page` | integer | Yes |
| `page_size` | integer | Yes |
| `total` | integer | Yes |

### MeasurementsReadPage

| Field | Type / validation | Required |
|---|---|---|
| `items` | array<MeasurementsRead> | Yes |
| `page` | integer | Yes |
| `page_size` | integer | Yes |
| `total` | integer | Yes |

### VaccinationsReadPage

| Field | Type / validation | Required |
|---|---|---|
| `items` | array<VaccinationsRead> | Yes |
| `page` | integer | Yes |
| `page_size` | integer | Yes |
| `total` | integer | Yes |

### RecordCorrectionsReadPage

| Field | Type / validation | Required |
|---|---|---|
| `items` | array<RecordCorrectionsRead> | Yes |
| `page` | integer | Yes |
| `page_size` | integer | Yes |
| `total` | integer | Yes |

### MedicinesReadPage

| Field | Type / validation | Required |
|---|---|---|
| `items` | array<MedicinesRead> | Yes |
| `page` | integer | Yes |
| `page_size` | integer | Yes |
| `total` | integer | Yes |

### PrescriptionDetailPage

| Field | Type / validation | Required |
|---|---|---|
| `items` | array<PrescriptionDetail> | Yes |
| `page` | integer | Yes |
| `page_size` | integer | Yes |
| `total` | integer | Yes |

### PrescriptionTemplateReadPage

| Field | Type / validation | Required |
|---|---|---|
| `items` | array<PrescriptionTemplateRead> | Yes |
| `page` | integer | Yes |
| `page_size` | integer | Yes |
| `total` | integer | Yes |

### PrescriptionOcrJobReadPage

| Field | Type / validation | Required |
|---|---|---|
| `items` | array<PrescriptionOcrJobRead> | Yes |
| `page` | integer | Yes |
| `page_size` | integer | Yes |
| `total` | integer | Yes |

### ScheduleDetailPage

| Field | Type / validation | Required |
|---|---|---|
| `items` | array<ScheduleDetail> | Yes |
| `page` | integer | Yes |
| `page_size` | integer | Yes |
| `total` | integer | Yes |

### DoseOccurrencesReadPage

| Field | Type / validation | Required |
|---|---|---|
| `items` | array<DoseOccurrencesRead> | Yes |
| `page` | integer | Yes |
| `page_size` | integer | Yes |
| `total` | integer | Yes |

### MedicationLogsReadPage

| Field | Type / validation | Required |
|---|---|---|
| `items` | array<MedicationLogsRead> | Yes |
| `page` | integer | Yes |
| `page_size` | integer | Yes |
| `total` | integer | Yes |

### ShareDetailPage

| Field | Type / validation | Required |
|---|---|---|
| `items` | array<ShareDetail> | Yes |
| `page` | integer | Yes |
| `page_size` | integer | Yes |
| `total` | integer | Yes |

### DispenseHistoryPage

| Field | Type / validation | Required |
|---|---|---|
| `items` | array<DispenseHistory> | Yes |
| `page` | integer | Yes |
| `page_size` | integer | Yes |
| `total` | integer | Yes |

### DispenseDetailPage

| Field | Type / validation | Required |
|---|---|---|
| `items` | array<DispenseDetail> | Yes |
| `page` | integer | Yes |
| `page_size` | integer | Yes |
| `total` | integer | Yes |

### LabUploadRequestsReadPage

| Field | Type / validation | Required |
|---|---|---|
| `items` | array<LabUploadRequestsRead> | Yes |
| `page` | integer | Yes |
| `page_size` | integer | Yes |
| `total` | integer | Yes |

### ReportsReadPage

| Field | Type / validation | Required |
|---|---|---|
| `items` | array<ReportsRead> | Yes |
| `page` | integer | Yes |
| `page_size` | integer | Yes |
| `total` | integer | Yes |

### NotificationsReadPage

| Field | Type / validation | Required |
|---|---|---|
| `items` | array<NotificationsRead> | Yes |
| `page` | integer | Yes |
| `page_size` | integer | Yes |
| `total` | integer | Yes |

### UsersReadPage

| Field | Type / validation | Required |
|---|---|---|
| `items` | array<UsersRead> | Yes |
| `page` | integer | Yes |
| `page_size` | integer | Yes |
| `total` | integer | Yes |

### JobStatusPage

| Field | Type / validation | Required |
|---|---|---|
| `items` | array<JobStatus> | Yes |
| `page` | integer | Yes |
| `page_size` | integer | Yes |
| `total` | integer | Yes |

### AuditEventsReadPage

| Field | Type / validation | Required |
|---|---|---|
| `items` | array<AuditEventsRead> | Yes |
| `page` | integer | Yes |
| `page_size` | integer | Yes |
| `total` | integer | Yes |

### PatientActivityPage

| Field | Type / validation | Required |
|---|---|---|
| `items` | array<PatientActivity> | Yes |
| `page` | integer | Yes |
| `page_size` | integer | Yes |
| `total` | integer | Yes |

### IntegrationClientsReadPage

| Field | Type / validation | Required |
|---|---|---|
| `items` | array<IntegrationClientsRead> | Yes |
| `page` | integer | Yes |
| `page_size` | integer | Yes |
| `total` | integer | Yes |

### IntegrationSubmissionsReadPage

| Field | Type / validation | Required |
|---|---|---|
| `items` | array<IntegrationAdminSubmissionRead> | Yes |
| `page` | integer | Yes |
| `page_size` | integer | Yes |
| `total` | integer | Yes |

## 9. Limited FHIR export mapping

Only individual read exports: Patient and doctor-issued prescription-item MedicationRequest. No FHIR search/create/update, Bundle service, SMART auth or full conformance claim. Firebase human authorization and category grants still apply. Patient.id = internal patient UUID; identifier system = `urn:medilink:patient`; value = that UUID; name.text from authorized display name; birthDate if present. MedicationRequest.id = item UUID; subject.reference = `Patient/{patient_uuid}`; medicationCodeableConcept.text = medicine_name_snapshot; intent = order; authoredOn = issued_at; dosageInstruction.text combines dose_text/route/frequency/duration/instructions without fabricating structure. Active→active, completed→completed, discontinued→stopped, cancelled/superseded→cancelled. Draft export is rejected until issued_at exists. Review status never upgrades patient-source orders. Validate fixtures against selected R4 structures during implementation before claiming export compatibility.

## 10. Coverage and release boundary

| Mandatory scope | Contract area |
|---|---|
| Firebase/profile/6 roles | Identity, Administration; Firebase SDK flows |
| Organizations/professional verification | Organizations, Verification, private evidence |
| Patient history/consent/QR/doctor queue | Consent, Consultations, Clinical records |
| Doctor and patient prescriptions/provenance | Prescriptions, Medicines, Documents |
| Fixed schedules/reminders/Taken/Skipped/adherence | Medication, Notifications, background jobs |
| Partial/complete dispensing/history | Pharmacy |
| Patient/lab report uploads and corrections | Laboratory, Documents |
| Weight/BP/glucose history, charts and vaccination dates | Measurements list (authorized patient/doctor access); charts are built from paginated readings |
| Printed English OCR with human review and retry | Prescription OCR job routes; new `prescription_ocr_jobs` table linked to original document and resulting patient draft |
| Prescription PDF export | Authorized on-demand `GET /prescriptions/{prescription_id}/pdf`; generated from the currently authorized prescription projection |
| Doctor favourites and templates | Prescription shortcut routes; new doctor-owned template and item tables; renewal still creates a review-required draft |
| Correction/version/audit trails | Explicit version/actions, correction requests, activity/audit |
| One mock lab partner | Integration management, Mock integration |
| Limited Patient/MedicationRequest exports | FHIR export |
| Local setup/recovery/operations | Operational statuses; setup, Compose and restore procedures belong to engineering documentation |

OCR accepts printed English files only; handwriting is out of scope. OCR output is untrusted draft material. A patient must review and correct it, then create a patient-source draft and explicitly confirm it; it never authorizes prescribing or dispensing. Charts use the existing authorized measurement list and do not interpret values or diagnose. PDF export is generated on demand after current record access is checked. Templates contain medication fields only, never patient-specific data; using one fills a draft, and consent, consultation, doctor review and explicit issue remain mandatory. Archived templates and OCR jobs are retained; no hard-delete routes are added.

## 11. Implementation handoff

This contract describes the project baseline for routes, data boundaries, roles, dose/snooze limits, idempotency, delegated permissions and privileged MFA. Implement the approved schema through Alembic migrations, confirm the chart dependency sequence against the measurement package, and configure bounded rate limits before integration/demo. Treat interface changes as contract changes requiring team review. Verification must cover patient ownership of OCR documents/jobs, failed-job retry bounds, correction before draft creation, no OCR promotion to doctor-issued status, chart grant enforcement, PDF authorization, template ownership/archive behavior, renewal/review requirements, and the existing high-risk access and lifecycle checks.


## Measurement correction — finalized design

`POST /api/v1/measurements/{measurement_id}/corrections` requires FirebaseBearer and `Idempotency-Key`. The patient may correct only their own active patient-sourced measurement. Professional-entered measurements cannot be overwritten by the patient. The request is a full replacement with `value`, `unit`, `measured_at`, and nonblank `reason`; `secondary_value` is required for blood pressure and forbidden otherwise; `notes` is optional. The type is immutable and derived from the original. `value` and `secondary_value` must be positive, blood pressure uses mmHg, weight kg/lb, blood glucose mmol/L or mg/dL. Date/time must include a timezone. Unknown fields are rejected. The server sets patient, type, source, recorder and status.

Lock the original, check ownership, source and active status, then atomically insert a replacement with `supersedes_id` pointing to the original and `correction_reason`, mark the original superseded and audit the action. Return 201 and `MeasurementsRead`. Different requests racing against the same original: one wins, others return 409 `STALE_VERSION`; the same idempotency key and identical request replays the original response, while different content gives `IDEMPOTENCY_CONFLICT`. 401 invalid token; 403 forbidden known action; 404 absent/concealed record; 422 field/type/unit validation; 429 rate limited; 500 safe error. Current lists/charts show active records only; preserve superseded history. This is a project design decision; team review and implementation remain pending.


**Doctor approval rule:** A verified authorized doctor can explicitly choose review outcome `approved`. Store the review event and set the prescription review status to `approved` transactionally, including reviewer and review time. Only this outcome displays “Patient-added · Doctor Approved”. `reviewed` alone is not approval. The source remains `patient`; approval is not a doctor-issued order and does not authorize dispensing. A later review supersedes the displayed approval according to the latest recorded review outcome; history is retained.


## Measurement correction history

`GET /api/v1/measurements/{measurement_id}/history` returns a `MeasurementsReadPage` for the complete correction lineage containing the supplied version (active or superseded). Human Firebase bearer authentication is required. A patient may read their own history. A verified doctor may read the patient history only with the current `measurements_vaccinations` read grant; consultation write permission is not required for this read. Recheck account, verification and grant expiry/revocation on each request. Conceal inaccessible records with 404 and use 403 for a known forbidden action. Use page (default 1) and page_size (default 20, maximum 100); deterministic order is `created_at DESC, id DESC`. Return source, status, `supersedes_id`, correction reason and timestamps through the existing `MeasurementsRead` projection. The normal measurement list and chart include active rows by default; this endpoint includes active and superseded versions of one lineage only.
