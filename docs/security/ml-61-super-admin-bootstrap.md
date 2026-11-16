# ML-61 — Controlled Super Admin bootstrap (MFA deferred)

**Target:** `nexora-neural/medilink-backend`, `develop`, reviewed starting at
commit `b2666c94cc07ca6b77802f05c23c20d09104507e` (2026-11-16).

**Status:** implementation candidate; operator policy and dedicated database
integration tests require project-team verification before merging or release.

## 1. Security boundary

- Public `POST /api/v1/me/onboard` accepts only `patient`, `doctor`,
  `pharmacist`, and `lab_staff`. `admin` and `super_admin` return HTTP 422.
  The service also rejects a deliberately bypassed validation object with 403.
- An initial Super Admin is created **only** by a restricted, offline CLI. There
  is no public bootstrap route, admin appointment bypass, or signup secret.
- The CLI accepts a pre-approved Firebase UID, independently looks it up using
  the Firebase Admin SDK, and requires an enabled account with verified email.
  The email is obtained from Firebase, not the request or the operator.
- `ML61_BOOTSTRAP_APPROVED_UID` is a protected operational allowlist. Its value
  must match the requested UID. The explicit confirmation switch and (for
  production) approval flag are **safety interlocks, not authentication**.
  OS/IAM/database/service-account access must already restrict operators to
  trusted maintainers. An independent change-approval record is required for
  production bootstrap.
- Postgres advisory lock `(717, 61)` serializes simultaneous bootstrap scripts.
  Once **any** Super Admin role assignment exists — even revoked — the CLI
  refuses to bootstrap again. Recovery is outside this one-time flow.
- Bootstrap refuses reusing a local account which already has any role, to
  prevent a patient/professional account from receiving platform privilege.
- The user/role assignment and the append-only `account.super_admin.bootstrapped`
  event are written in **one database transaction**. On audit failure, there
  is no persisted privileged account. A denied database bootstrap attempt is
  audited in a separate transaction with sanitized metadata. Audit metadata contains only safe
  operator-type, source and role labels; do not add Firebase UID, passwords,
  tokens, email addresses or free-form secrets.

## 2. Production prerequisites (no automatic production enablement)

1. Obtain documented change approval, designated operator, target project,
   environment, Firebase UID and recovery owner. Store the approval outside
   the application database and link the printed request ID to the record.
2. Run only on a restricted administration host or CI runner with short-lived
   privileged access, scoped PostgreSQL credentials and Firebase Admin SDK
   credentials. Audit the operator at the infrastructure layer. Never expose
   the CLI from a web route or normal user workstation.
3. Confirm Firebase account has a verified email and is not disabled. Use a
   dedicated identity with no existing patient/professional/Admin roles.
4. Back up the database and verify there are **no existing Super Admin role
   assignments**. If one exists, stop and use a reviewed recovery process.
5. Require external approval for the change in your deployment/IAM system.
   `ML61_BOOTSTRAP_PRODUCTION_APPROVAL=APPROVED` is merely a guard preventing
   accidental execution and does not prove that approval occurred.
6. Post-bootstrap, validate actor role and audit event using **restricted**
   read-only database inspection. Do not print sensitive identity data to logs.

The academic project team must explicitly approve this controlled operator
process before claiming ML-61 production-ready.

## 3. Run on a dedicated synthetic LOCAL environment

From the backend repository root, on Windows PowerShell (after migrations):

```powershell
# Use a dedicated local/test database and Firebase project, NOT production.
$env:APP_ENV = 'test'
$env:FIREBASE_PROJECT_ID = 'your-test-firebase-project'
$env:ML61_BOOTSTRAP_APPROVED_UID = (Read-Host 'Approved synthetic Firebase UID')
# GOOGLE_APPLICATION_CREDENTIALS must refer to a secure local credential file.

python -m alembic upgrade head
python -m app.database.bootstrap_super_admin `
  --firebase-uid $env:ML61_BOOTSTRAP_APPROVED_UID `
  --confirm-initial-bootstrap
```

The CLI will reject disabled/unverified Firebase accounts, a mismatched UID,
existing Super Admin assignments and role-bearing local accounts. The command
outputs an audit request ID, not a token or email.

**Important:** `python -m app.database.seed_demo --apply` creates a synthetic
Super Admin assignment. On that database, the one-time bootstrap will
correctly refuse to run. Use a **fresh, dedicated** database for this exercise.

## 4. Quality and validation

```powershell
python -m ruff check app/modules/identity/bootstrap.py `
  app/database/bootstrap_super_admin.py app/modules/profiles/service.py `
  tests/test_ml61_public_signup.py tests/test_ml61_bootstrap.py
python -m unittest tests.test_ml61_public_signup -v
python -m unittest tests.test_ml61_bootstrap -v
python -m unittest discover -s tests -v
```

The database tests in `tests/test_ml61_bootstrap.py` are **skipped** unless
`ML61_TEST_DATABASE_URL` points to a separate, empty, fully migrated PostgreSQL
test database. Example: set the URL to the project-approved test DB, then run
`python -m unittest tests.test_ml61_bootstrap -v`. Do not treat skips as passes.
The test suite uses synthetic identifiers and a savepoint/outer-transaction
pattern so its data is rolled back. Test the CLI with a *different* disposable
DB after automated tests.

## 5. Checks to capture in PR evidence

| Scenario | Expected outcome |
| --- | --- |
| Public `requested_role=super_admin` / `admin` | 422; onboarding not persisted |
| Public request includes `roles`, `is_super_admin` or forged UID | 422 |
| Internal call bypasses Pydantic and requests privileged role | 403 before SQL |
| First trusted CLI bootstrap with verified Firebase account | One active `super_admin` role, one audit event |
| Duplicate/revoked role bootstrap attempt | Denied; no new assignment |
| Existing patient/professional role on target account | Denied |
| Audit-event insert fails | User and role rolled back |
| Concurrent initial bootstrap attempts | At most one succeeds (verify on PostgreSQL) |
| Current Admin appointment/revocation | Existing MFA protection unchanged |

## 6. Deferred: MFA

MFA enrollment, second-factor sign-in UX, token freshness, production MFA
policy, recovery, and live privileged-MFA testing are **not implemented here**.
Existing `require_privileged_mfa` checks in ML-35 must remain untouched. Do
not bypass or remove those checks to make tests pass. Track MFA in a separately
approved Jira subtask and PR. **ML-61 cannot be marked fully Done** until the
remaining original MFA acceptance scope is completed or formally rescheduled.

## 7. Changes intentionally NOT made

- No new public endpoints or API contract changes.
- No new database tables, columns, or Alembic migrations.
- No modifications to Firebase Auth configuration or MFA guards.
- No changes to `app/modules/identity/service.py` privileged-role operations.
