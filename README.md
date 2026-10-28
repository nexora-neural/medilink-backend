# MediLink Backend

FastAPI backend starter for the MediLink academic project. The starter follows the team's familiar Python layers: **router → service → repository → database**. It is a modular monolith; the feature folders are modules inside one application, not separate microservices.

> This repository is a development scaffold. It does not yet implement MediLink's clinical features, authorization rules, database models, or production deployment. Use synthetic data only.

## Technology baseline

- Python 3.14
- FastAPI and Pydantic 2
- SQLAlchemy 2 Async ORM with asyncpg
- PostgreSQL 18
- Firebase Authentication token verification (backend integration to be implemented)
- Celery and Redis for background tasks (worker tasks to be implemented)

Alembic is intentionally not included in this starter, following the team's current decision. ORM models do not automatically update an existing database. At API startup, `Base.metadata.create_all()` creates missing tables for registered models; it does not create the PostgreSQL database itself or alter existing tables.

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
└── workers/                   # Celery app and background task modules
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

## Local setup

Prerequisites: Python 3.14, PostgreSQL 18, Redis, and Git. Install and run PostgreSQL and Redis using the method already used by your team.

1. Create a virtual environment and install dependencies:

   ```bash
   python3.14 -m venv .venv
   source .venv/bin/activate
   python -m pip install --upgrade pip
   pip install -r requirements-dev.txt
   ```

2. Create local configuration after PostgreSQL and Redis are running:

   ```bash
   cp .env.example .env
   ```

3. Start the API:

   ```bash
   uvicorn app.main:app --reload
   ```

4. Open the local API documentation at `http://127.0.0.1:8000/docs`. The `/health` route checks the API process; `/health/database` checks the database. The database check is also available under `/api/v1/health/database` because the API router is versioned.

`requirements-dev.txt` installs the runtime dependencies plus Ruff, which is used for linting and formatting according to `pyproject.toml`.

## Database table setup

The API follows the class-project startup pattern: FastAPI calls `init_db()` at startup and closes the engine on shutdown. Once SQLAlchemy models have been implemented and imported, the startup step creates their missing tables. A manual setup command is also available from this directory:

```bash
python -m app.database.create_tables
```

`create_all()` does not update existing tables when a model changes. Developers must coordinate later schema changes and keep the database structure and ORM models in sync. At this scaffold stage, MediLink domain ORM models are not yet implemented or imported, so startup creates no domain tables. Do not use real patient data in local development or demos.

## Configuration and credentials

- Copy `.env.example` to `.env` for local settings. `.env` is ignored by Git.
- Set `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, and `DB_PASSWORD` in `.env`; the app loads these separately and safely assembles the asyncpg connection. Optional pool settings are `DB_POOL_SIZE` and `DB_MAX_OVERFLOW`.
- Provide Firebase Admin credentials through `GOOGLE_APPLICATION_CREDENTIALS` in the local shell. Do not place service-account JSON files in the repository.
- The backend must verify Firebase ID tokens and make its own role, ownership, organization, consent, expiry, and record-status decisions. Firebase login alone does not grant access to medical records.
- Store uploaded files in private storage, outside the public/static directory. File authorization must be enforced by the backend.

## Git and GitHub

1. Create a private GitHub repository for the backend unless the team has already selected a repository.
2. Keep `.env`, credentials, database files, uploads, and real or identifiable health information out of commits.
3. Use feature branches and pull requests. Review API-contract and database changes with the relevant team members before merging.
4. Add a license only after the project owner/team chooses one; this starter does not assume a license.

