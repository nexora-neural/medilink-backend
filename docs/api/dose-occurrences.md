# ML-170: dose occurrences and snoozed reminders

## API operations

| Operation | Response | Permission |
| --- | --- | --- |
| `GET /api/v1/patients/{patient_id}/dose-occurrences` | `DoseOccurrencesReadPage`, 200 | Own Patient record, or verified Doctor with a current `medication_logs` grant |
| `POST /api/v1/dose-occurrences/{occurrence_id}/snooze` | `DoseOccurrencesRead`, 201 | Owner Patient only |

Both routes use the existing authenticated request quota, private response
headers, request IDs and safe medication error envelope. Doctor reads recheck the
approved Doctor verification, active role/account, current unexpired and unrevoked
category grant, and approved organization membership where relevant. A Doctor
cannot snooze a patient's occurrence.

The list supports `from` (inclusive), `to` (exclusive), `status`, `page` and
`page_size` (1–100, default 20). Results sort by `scheduled_for` and UUID ascending;
`total` is calculated after filters. Timestamps must include a timezone. Unknown
query parameters and invalid status/date ranges are rejected.

Snooze accepts only `snoozed_until`. The server requires an active schedule, an
unanswered `due` or `unreported` occurrence whose original `scheduled_for` is not
in the future, and no response log. `snoozed_until` must be after the database
clock, no more than 24 hours later, and inside the half-open schedule window. A
successful write updates only `snoozed_until` and appends an audit event in the
same transaction. It does not alter the occurrence time, response history, status
or adherence denominator. ML-186 remains responsible for Taken/Skipped responses
and corrections.

## Occurrence worker

Run the worker as a separate backend process:

```sh
python -m app.workers.dose_occurrences
```

Each pass locks a bounded batch of active schedules with `SKIP LOCKED`, generates
fixed local-time occurrences through the next 24 hours, and advances the schedule's
generation cursor in the same transaction. The least recently processed schedules
are selected first so batches rotate fairly. The existing unique
`(schedule_id, scheduled_for)` constraint and conflict-safe inserts make repeated
passes and worker retries idempotent. A catch-up pass is capped at 31 calendar
days per schedule; later passes continue from the cursor. A schedule confirmed
before this migration starts generation from deployment time rather than
inventing pre-ML-170 adherence history.

An occurrence is `upcoming` before its scheduled time, `due` when evaluated exactly
at that instant, and `unreported` on the first evaluation after that instant if no
response log exists. The original `scheduled_for` is never shifted. Notification
rows provide separate in-app reminder delivery evidence and use a dedupe key per
occurrence/reminder slot. A pending snooze suppresses delivery until its target;
after delivery, the notification row retains the evidence and the pending time is
cleared. Repeated passes cannot duplicate an already delivered reminder.
When snoozed before the first reminder, the snoozed delivery replaces the original
reminder; a later pass does not send the original as well.

The worker skips nonexistent wall-clock times on daylight-saving transitions and
uses the earlier offset for ambiguous local times. It does not send reminders for
paused, terminal or course-expired schedules. Resume starts generation at the
resume instant and does not backfill occurrences or initial reminders for doses
missed while paused. Stopping/replacing a schedule first marks elapsed unanswered
occurrences `unreported`, then cancels only future unanswered occurrences and
clears pending snoozes for unanswered occurrences. Any response log and answered
occurrence remain.

## Verification

The `test_ml170_dose_occurrences` suites cover contract validation, filters and
pagination, status transitions, duplicate generation, retry-safe notification
delivery, snooze invariants and bounds, patient/Doctor authorization, audit
rollback, schedule-stop cleanup, time zones and concurrent worker/API requests.
PostgreSQL suites use isolated `ML170_TEST_DATABASE_URL` and
`ML170_CONCURRENCY_DATABASE_URL` databases; test fixtures use synthetic data.
