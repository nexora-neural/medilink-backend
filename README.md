# MediLink Backend

FastAPI backend starter for the MediLink academic project. The starter follows the team's familiar Python layers: **router → service → repository → database**. It is a modular monolith; the feature folders are modules inside one application, not separate microservices.

> This repository is a development scaffold. It does not yet implement MediLink's clinical features, authorization rules, database models, or production deployment. Use synthetic data only.

## Technology baseline

| Area | Pinned baseline | Lock or source |
|---|---|---|
| Frontend | Angular 21.2.25, Angular CLI 21.2.28, Angular Material 21.2.14 | `medilink-frontend/package-lock.json` |
| Frontend runtime | Node.js 24.16.0, npm 11.13.0 | Frontend `.nvmrc` and `package.json` |
| Backend runtime | CPython 3.13.13 | `.python-version` |
| API and validation | FastAPI 0.162.0, Pydantic 2.14.1 | `requirements.lock` |
| ORM and PostgreSQL driver | SQLAlchemy 2.1.6, asyncpg 0.32.0 | `requirements.lock` |
| Database server | PostgreSQL 18 | Team baseline; server installation is separate from Python dependencies |
| Firebase Admin SDK | 7.7.0 | `requirements.lock` |

Backend runtime and development locks pin all Python packages. The frontend lock file pins its complete npm dependency tree. Install from these lock files for reproducible setup; the unpinned requirement files are dependency inputs, not the installation source.

Alembic manages database schema revisions. Configure the local PostgreSQL settings in `.env`, create an empty database, and run `python -m alembic upgrade head`. At startup, the application checks database connectivity. Use Alembic migrations to create and change tables; application startup does not create schema objects.

For a disposable development database, apply the organization migration with `python -m alembic upgrade head` and inspect the revision with `python -m alembic current`. To roll back that migration, run `python -m alembic downgrade c1c2a172fbc4`; this removes the organization and membership tables and their data. Review dependent revisions and preserve any needed data before rollback. Run `python -m alembic upgrade head` to apply it again.

## Project layout

```text
app/
├── main.py                    # FastAPI app and API router registration
├── api/router.py              # Versioned API routes
├── core/                      # Settings, authentication and permissions
├── database/                  # SQLAlchemy base, async session and table setup
├── modules/                   # MediLink feature modules
│   ├── identity/
│   ├── profiles/
│   ├── organizations/
│   ├── verification/
│   ├── consent/
│   ├── consultations/
│   ├── clinical_records/
│   ├── prescriptions/
│   ├── medication/
│   ├── dispensing/
│   ├── documents/
│   ├── measurements/
│   ├── vaccinations/
│   ├── notifications/
│   ├── audit/
│   ├── integrations/
│   └── fhir/
├── services/                  # Shared services such as private file storage
└── workers/                   # Background task placeholders
```

Each feature should contain only the layers it needs. A typical feature can use:

```text
prescriptions/
├── router.py       # HTTP endpoints
├── schemas.py      # Pydantic request/response schemas
├── models.py       # SQLAlchemy ORM models
├── repository.py   # Database queries
└── service.py      # Feature rules and orchestration
```

## Repeatable quality checks

Install the pinned development dependencies from the backend repository root:

    python -m pip install -r requirements-dev.lock

Then run lint and API checks:

    ruff check .
    python -m unittest discover -s tests -v

By default the API tests use synthetic fixtures without connecting to PostgreSQL. Set `ML30_TEST_DATABASE_URL` and `ML90_TEST_DATABASE_URL` to a dedicated, migrated PostgreSQL test database to run the optional integration tests.

## Local setup

Prerequisites: Python 3.13.13, PostgreSQL 18, and Git.

1. Create a virtual environment and install the pinned development dependencies:

   ```bash
   python3.13 -m venv .venv
   source .venv/bin/activate
   python -m pip install --upgrade pip
   python -m pip install -r requirements-dev.lock
   ```

   For runtime-only installation, use `python -m pip install -r requirements.lock`.

2. Create local configuration after PostgreSQL is running:

   ```bash
   cp .env.example .env
   ```

3. Apply migrations and start the API:

   ```bash
   python -m alembic upgrade head
   uvicorn app.main:app --reload --no-access-log --no-proxy-headers
   ```

4. Open the local API documentation at `http://127.0.0.1:8000/docs`. The `/health` route checks the API process; `/health/database` checks the database. The database check is also available under `/api/v1/health/database` because the API router is versioned.

`requirements-dev.lock` installs the exact runtime dependency set plus Ruff 0.17.3. `requirements.lock` contains the runtime dependency set only.

## Database table setup

Apply migrations from the backend repository root after creating an empty local PostgreSQL database and setting its connection details in `.env`:

```bash
python -m alembic upgrade head
python -m alembic current
python -m alembic heads
```

The initial revision is an empty baseline because the current model modules contain no mapped domain tables. Future model changes require reviewed migration revisions. For a database previously populated by `create_all()`, compare its schema and decide on a migration strategy before applying or stamping revisions; do not assume it matches. Do not use real patient data in local development or demos.

## Backend request limits

All API workers use the same PostgreSQL counters. Fixed windows reset at the next UTC minute or hour boundary using database time. Each successful quota check is committed before business processing. A request rejected by either resolution quota spends neither quota. Limit responses return `429 RATE_LIMITED` with `Retry-After` in seconds. If the shared counter is unavailable, protected requests receive `503 SERVICE_UNAVAILABLE`.

| Operation | Demo limit | Wiring |
|---|---|---|
| Authenticated profiles and medicine catalogue | 120/minute per verified account | `require_authenticated_rate_limit` on existing routers |
| QR, claim and request-token resolution | 10/minute per account and 30/minute per IP | Use `require_resolution_rate_limit` on the feature routes |
| Uploads and OCR starts | 20/hour per account | Use `require_upload_rate_limit` and call `validate_document_upload` before processing a file |
| Mock lab integration | 60/minute per client | Call `require_integration_rate_limit` with the client ID resolved by API-key authentication |

Use one operation class per endpoint. The last three classes are ready for the feature routers as their handlers are implemented. Upload validation accepts detected PDF, JPEG or PNG files up to 10 MiB and returns safe 413 or 415 responses. Firebase-hosted sign-in quotas are configured in Firebase. The backend limits above use verified identities. An unauthenticated-attempt quota and a total request-body cap are separate decisions.

Use the ASGI peer address for the IP quota. Behind a reverse proxy, configure Uvicorn to trust only explicitly listed proxies that set the forwarded address. Disable raw access logs where URLs can include private identifiers. Application error logs use request IDs and omit request inputs and exception details.

The store keeps one row per hashed quota key and resets it on use. If operating a long-lived environment, periodically remove inactive counters with `DELETE FROM request_rate_limits WHERE window_end < now() - INTERVAL '1 day'`.

## Configuration and credentials

- Copy `.env.example` to `.env` for local settings. `.env` is ignored by Git.
- Set `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, and `DB_PASSWORD` in `.env`; the app loads these separately and safely assembles the asyncpg connection. Optional pool settings are `DB_POOL_SIZE` and `DB_MAX_OVERFLOW`.
- Set `CORS_ORIGINS` to a JSON array of exact browser origins, for example `["http://localhost:4200"]`. The backend permits the `Authorization`, `Accept`, and `Content-Type` headers and does not allow cookie credentials.
- Provide Firebase Admin credentials through `GOOGLE_APPLICATION_CREDENTIALS` in the local shell. Do not place service-account JSON files in the repository.
- The backend must verify Firebase ID tokens and make its own role, ownership, organization, consent, expiry, and record-status decisions. Firebase login alone does not grant access to medical records.
- Store uploaded files in private storage, outside the public/static directory. File authorization must be enforced by the backend.

## Git and GitHub

1. Create a private GitHub repository for the backend unless the team has already selected a repository.
2. Keep `.env`, credentials, database files, uploads, and real or identifiable health information out of commits.
3. Use feature branches and pull requests. Review API-contract and database changes with the relevant team members before merging.
4. Add a license only after the project owner/team chooses one; this starter does not assume a license.


### PostgreSQL enum migrations

Register models in `app/database/models.py` and generate revisions with `python -m alembic revision --autogenerate -m "describe change"` against an up-to-date development database. The Alembic PostgreSQL enum extension generates enum changes for new revisions; it does not repair existing revisions. For newly declared PostgreSQL enums, omit `create_type=False` in the model: the extension adds it to generated migrations. With extension version 1.11.0, explicitly setting it on the model can generate a duplicate keyword and invalid Python. Unused enum types are not automatically dropped. Review generated revisions, especially shared enums and value changes, and verify upgrade and downgrade on a disposable PostgreSQL database before merging.


### Synthetic development roles and verification examples

After applying all Alembic migrations, run `python -m app.database.seed_demo --apply`
to add six synthetic identities and pending, approved and suspended verification examples.
The seed is intended for local, development and test environments only. It uses synthetic
Firebase identifiers and creates no Firebase accounts or login credentials. Existing records
are not overwritten; rerunning the command does not duplicate the seeded identities.
The verification tables from the verification migration must be present first.

### Local Firebase test-account role seeding

Each developer can link an existing shared Firebase test account to their own local
database without copying passwords into the database or repository. Set
`FIREBASE_PROJECT_ID` and `FIREBASE_WEB_API_KEY` in the local `.env`, apply migrations,
then run one command per account:

```sh
python -m app.database.seed_test_account --role patient --email medilink.test.patient@example.com
python -m app.database.seed_test_account --role doctor --email medilink.test.doctor@example.com
python -m app.database.seed_test_account --role pharmacist --email medilink.test.pharmacist@example.com
python -m app.database.seed_test_account --role lab_staff --email medilink.test.lab_staff@example.com
python -m app.database.seed_test_account --role admin --email medilink.test.admin@example.com --allow-privileged-role
python -m app.database.seed_test_account --role super_admin --email medilink.test.super_admin@example.com --allow-privileged-role
```

The command prompts for the Firebase password without echoing it, signs in to the
configured Firebase project to obtain the UID, and stores only the UID, email, role,
and required local profile row. It is restricted to local, development and test
environments. Patient profile values are synthetic. Professional profiles remain
`not_submitted`; this command does not grant professional verification. Admin roles
require an explicit flag and should only be used against a local/test database.

### Patient onboarding

`POST /api/v1/me/onboard` requires a verified Firebase bearer token, `display_name`,
and `requested_role`. Patient requests also require `date_of_birth` (ISO date, not
in the future) and `sex` (`female` or `male`). This extends the original onboarding
contract to supply the fields required by the patient profile schema. Professional
requests must not include these patient fields. Repeated requests reconcile using
the verified Firebase UID; matching email addresses do not merge accounts. Role
seed data must be present. Account and profile changes are recorded in audit events.
