# ML-154 — prescription lifecycle and linked versions

Implements the seven baseline operations medilink_051 through medilink_057.
All require Firebase bearer identity and the shared authenticated request quota.
Responses use the existing PrescriptionDetail/PrescriptionDetailPage with the
additive private `lifecycle_events` collection. Private responses use no-store;
errors remain `{code,message,field_errors,request_id}` with X-Request-ID.

## Operations

| Method / suffix under `/api/v1/prescriptions/{prescription_id}` | Input | Result |
| --- | --- | --- |
| POST `/versions` | PrescriptionVersionInput | 201 linked draft for correction |
| POST `/renewals` | PrescriptionVersionInput | 201 linked review-required renewal draft |
| POST `/reissues` | PrescriptionVersionInput | 201 linked review-required reissue draft |
| POST `/cancel` | ReasonInput | 201 cancelled prescription |
| POST `/discontinue` | ReasonInput | 201 discontinued prescription |
| POST `/complete` | ReasonInput | 201 completed prescription |
| GET `/history` | page, page_size | 200 entire linked family, paginated |

Version input extends PrescriptionInput with a trimmed nonblank `reason` (max
1000 characters), and 1–100 medicine items. Dates and medication field/pair
validation reuse the draft schema. Source, issuer, status, review outcome,
predecessor and actor are server-owned and rejected as unknown input fields.
The version payload is the complete proposed new draft, not an implicit clone;
no dosing, course dates or consultation context is inferred.

POST query fields are forbidden. An optional `If-Match` may contain a positive
integer version, bare or fully quoted; stale versions return 409 STALE_VERSION.
It is optional to preserve the baseline contract, which does not require that
header on these operations. Malformed/weak/wildcard values return 422. Consumers
should send the displayed version to protect against stale forms.

## Permissions and state

Only current active patient roles can change their own patient-source personal
records. Owning patients cannot terminate/version a doctor-issued order. Admin,
Super Admin and pharmacy roles do not inherit clinical access.

A verified current doctor needs a prescriptions category grant, an active
patient-approved consultation, and any organization membership required by that
grant. Versions and renewals require their own doctor-source predecessor.
Only explicit **reissue** may take patient-source evidence and create a separate
doctor-source draft. This never changes the evidence's source or marks it
clinically approved. Existing issue validation still requires a complete doctor
order and an explicit doctor issue action.

For version writes the caller supplies the current consultation_id in the new
content. For terminal doctor actions the stored order consultation must still be
active/authorized; the ReasonInput contract contains no replacement consultation
field. Ended, expired, revoked or mismatched clinical contexts fail closed.
History is a read: a doctor needs the current prescriptions read grant, but does
not need consultation write permission. Another patient's inaccessible resource
is concealed with 404; known forbidden own operations return 403.

| Starting state | Allowed action |
| --- | --- |
| active, previously confirmed/issued | version, renewal, reissue, cancel, discontinue, complete, subject to source/actor rules |
| completed, previously confirmed/issued | version, renewal, reissue into a new draft; never reopen the same record |
| draft | existing draft editor/confirmation/doctor issue; no ML-154 terminal/version action |
| cancelled, discontinued, superseded | retained history only; no reactivation/version action |

One pending linked draft is allowed per predecessor. Repeating creation returns
409 and points the caller to the existing draft workflow without exposing a
foreign ID. Creation does not change the old active order, its shares or
schedules. Patient confirmation / doctor issuance of the separate successor
atomically supersedes the active/completed predecessor. A second competing
successor cannot activate after the first has superseded it.

Terminal actions allow active → requested terminal state only. Repeating the
same action/reason by the same actor is state-idempotent (201, no extra event or
version change); changing the reason or choosing a different terminal action
returns 409. If a stale If-Match is supplied, the stale check takes precedence.

## Retained data and transaction boundaries

The new `prescription_lifecycle_events` table stores actor, action, private
reason, prior/current state/version, related prescription and timestamp. The
predecessor link is the existing previous_prescription_id. Action events are
retained separately from the last status_reason. The authorized detail/history
projection returns them; generic audit metadata contains IDs/state/version/item
counts only, never clinical reason or medication text.

Each write, private action row, audit event and dependent cancellation commits
atomically. Audit failure rolls everything back. Draft replacement still returns
retained review and lifecycle evidence. History-bearing linked drafts cannot be
hard-deleted through the existing unreferenced-draft deletion route; their
retained action references intentionally produce a conflict.

Cancellation, discontinuation, completion and supersession stop active/paused
schedules and revoke created/claimed/confirmed pharmacy shares. Only future
unanswered, unlogged occurrences are cancelled; elapsed statuses, taken/skipped
logs, original scheduled timestamps, original items and prior notifications stay
unchanged. The future boundary is re-evaluated after schedule lock waits. Existing
worker processing selects active schedules and will not emit later reminders
for cancelled schedules. Events published before the termination commits are
historical delivery evidence and are not deleted.

The existing prescription/draft row locks serialize confirmation/issuance with
lifecycle actions. Authorization is rechecked after dependent lock acquisition
where a grant could expire during a wait. Cycle-safe recursive UNION queries
read the linked family, with patient predicates on each edge and deterministic
created_at DESC/id DESC pagination. page_size is at most 100.

Pharmacy share/dispensing service routes are still separate placeholders on the
reviewed base. This implementation revokes stored shares and changes order
state; it does not implement new dispensing endpoints. Downstream dispensing
must require an active doctor-issued order and a live confirmed share. Active
status or issued_at alone on a patient-source record never permits dispensing.

## Database / verification

Apply Alembic `ml154_lifecycle`, parent `ml170_worker_fairness`. No existing
clinical table is rewritten. Downgrade removes the new action-history table;
the packaged reversal/reapply check uses an empty disposable test database.

Use Python 3.13 and requirements-dev.lock. Set **both** dedicated migrated URLs:
`ML154_TEST_DATABASE_URL` and `ML154_CONCURRENCY_DATABASE_URL` (postgresql+asyncpg).
The concurrency database receives committed synthetic fixtures. Other API tests
roll back an outer fixture transaction. Never use a real patient database.

```powershell
python -m unittest tests.test_ml154_lifecycle_contract tests.test_ml154_lifecycle_postgres tests.test_ml154_lifecycle_concurrency -v
```

Database tests explicitly skip when their variables are missing. The packaged
VALIDATION.md and logs contain the actual tested runtime, full-suite result and
any baseline failures or optional verification that remains unrun.
