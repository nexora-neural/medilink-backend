# ML-155: daily medicine schedule confirmation

## Operations

| Operation | Response | Permission |
| --- | --- | --- |
| POST /api/v1/patients/{patient_id}/medication-schedules | ScheduleDetail, 201 | Own active Patient account and current Patient role |
| GET /api/v1/patients/{patient_id}/medication-schedules | ScheduleDetailPage, 200 | Own Patient record, or verified Doctor with current medication_logs consent |
| POST /api/v1/medication-schedules/{schedule_id}/status | ScheduleDetail, 201 | Owner Patient; If-Match current version |
| POST /api/v1/medication-schedules/{schedule_id}/replacement | ScheduleDetail, 201 | Owner Patient; If-Match current version (existing ML-112 guard retained) |

All routes use verified Firebase identity, the shared authenticated request quota,
safe code/message/field_errors/request_id errors and Cache-Control: no-store.
Lists accept only page (1+), page_size (1..100) and optional status. They include
retained terminal schedules, with deterministic created_at/ID descending order.
Doctor reads require medication_logs, not merely prescriptions consent, and
recheck verification, current roles, expiry, revocation and organization/membership.
No administrative role bypasses clinical authorization. Reads require no consultation;
Doctors cannot create, pause, resume or replace a Patient's reminder schedule.

## Explicit confirmation and clinical bounds

ScheduleInput accepts only prescription_item_id, timezone, starts_at, optional
ends_at, times and confirmed. confirmed must be JSON true; 1, strings and false
are rejected. Times must be unique HH:MM:SS strings, up to 24; timezone must be an
IANA zone and dates must be RFC3339 with timezone. End must follow start.

The item must belong to that patient and its prescription must be active with an
issued_at (including a separately confirmed patient-source prescription). Draft,
cancelled, completed, discontinued and superseded prescriptions cannot authorize
new schedules or resume/replacement. Source is never upgraded by scheduling.

The bounded daily-frequency vocabulary is case-insensitive with collapsed spaces:

| Count | Supported frequency values |
| --- | --- |
| 1 | daily; once daily; once a day; 1/day; OD |
| 2 | twice daily; twice a day; 2/day; BD; BID |
| 3 | three times daily; three times a day; 3/day; TDS; TID |
| 4 | four times daily; four times a day; 4/day; QID |

PRN, intervals (e.g. every 8 hours), weekly, tapers and unrecognized free text are
rejected with 422. The number of confirmed times must match the prescribed daily
count. This vocabulary is an explicit demo policy to review against the issue API;
it does not parse or infer medical instructions and never suggests times itself.
User-provided times become active only on explicit confirmation.

Courses are half-open instants [starts_at, ends_at). Prescription date bounds are
interpreted in the selected IANA timezone: starts_on at local midnight through the
midnight after ends_on. If starts_on is absent, the issued local date anchors the
course. duration_days, when present, additionally caps that course from its anchor.
The earliest known end wins. An explicit end is required for a bounded course;
an open end is allowed only when neither clinical end nor duration is present.
Already-ended courses cannot be newly confirmed/resumed. No dates are shifted to
fit dispensing, and prescription dose/frequency/instructions/dates are never written.
The API does not assume stock availability changes clinical timing.

## State and concurrency

Active <-> paused is supported; either can become completed or cancelled. Terminal
schedules cannot reopen or switch terminal state. Repeating the current state with
the current version is a no-op (201; no additional history/audit/version increment).
A stale If-Match always returns 409 STALE_VERSION. Version headers accept a positive
integer, optionally quoted; no weak/wildcard/malformed ETags. Status changes require
a nonblank reason. Replacement requires a new explicitly confirmed ScheduleInput
for the same item and an active predecessor.

Parent prescription share locks and item row locks serialize create/resume/replace
checks before locking the schedule itself. Overlapping active OR paused reservations
for an item are rejected; adjacent windows are allowed. Paused reservations prevent
resume collisions. Concurrent confirmation has one success, one conflict; concurrent
changes using one version have one success, one stale conflict. Database uniqueness
on previous_schedule_id prevents two successors of one predecessor.

Replacement cancels only the old future upcoming/due occurrences, cancels/increments
the old schedule and creates its linked successor. Existing times, past occurrences
and all medication logs are retained. Status completion/cancellation similarly
cancels only future unanswered occurrences and clears their pending snooze.
Pausing preserves occurrences and their timestamps; resuming never creates rows or
backfills missed doses, so it cannot duplicate occurrences.

**ML-170 boundary:** this API does not generate dose occurrences, deliver reminders,
mark due/unreported or implement snooze. The occurrence/reminder worker must gate on
current active schedule status and course bounds, and use the existing unique
(schedule_id, scheduled_for) key. In particular, it must not send reminders for
paused or terminal schedules. End-to-end delivery requires that dependent workflow;
these tests exercise schedule persistence and existing occurrence retention only.

## Retained history, audit and API additions

Migration ml155_schedule_history (after ml135_merge_heads) adds:

- Nullable, unique previous_schedule_id FK on medication_schedules.
- medication_schedule_status_changes with actor, prior/new status, version,
  private reason and timestamp. Application operations append history only.

The ORM and migration match. Existing schedules keep null predecessor links; none
are inferred from old audit events. A downgrade removes this new history/lineage.
Status reasons are private operational data, not generic audit metadata. Each
successful write and minimal audit commit together. Audit failures roll back all
schedule, occurrence, status-history and lineage changes. Reads log only count.

Additive read fields: schedule.previous_schedule_id and ScheduleDetail.prescription_source.
Status now explicitly documents required If-Match; replacement's existing required
header is also documented. Reference contract and OpenAPI are updated for review.
One ML-112 test fixture now creates an active issued prescription, keeping its stale
version and history assertions while satisfying the new course eligibility guard.

## Verify

Use Python 3.13.13 and requirements-dev.lock, two separate migrated disposable DBs:

```powershell
python -m unittest tests.test_ml155_schedule_contract -v
# Set ML155_TEST_DATABASE_URL and ML155_CONCURRENCY_DATABASE_URL to isolated test DBs.
python -m unittest tests.test_ml155_schedule_contract tests.test_ml155_schedules_postgres tests.test_ml155_schedules_concurrency -v
```

Main PostgreSQL fixtures roll back; concurrency fixtures commit synthetic records
for inspection. Live Firebase, frontend end-to-end, the pending ML-138 issuing
workflow and ML-75 QA remain independent integration/review activities.
