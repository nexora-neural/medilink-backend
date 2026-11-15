# Patient measurement history (ML-102)

`GET /api/v1/patients/{patient_id}/measurements/history`

Requires a verified Firebase bearer token and an active local account. A patient
with a non-revoked patient role can read their own history. A verified doctor
with a non-revoked doctor role needs a currently valid grant for
`measurements_vaccinations` and any applicable approved organization membership.
This read does not require an active consultation. Other patients and doctors
without access receive a non-disclosing error, not a history response.

## Parameters

| Parameter | Location | Contract |
| --- | --- | --- |
| `patient_id` | path | Required UUID |
| `page` | query | Integer, minimum 1, default 1 |
| `page_size` | query | Integer, 1–100, default 20 |

## Response

HTTP 200 returns `MeasurementsReadPage`: `items`, `page`, `page_size`, `total`.
All stored versions are included, including `active`, `superseded` and
`entered_in_error` records. Rows are ordered by `created_at DESC, id DESC`.
Unlike the current-measurements endpoint, this route does not filter out
superseded records or accept type/date filters. Empty and out-of-range pages
return `items: []` while retaining the matching `total`.

Each item preserves `id`, `patient_user_id`, `type`, `value`, `secondary_value`,
`unit`, `measured_at`, `recorded_by`, `source`, `notes`, `status`, `supersedes_id`,
`correction_reason` and `created_at`. A correction's `supersedes_id` links to its
immediate predecessor; the original has no predecessor or correction reason.
Values and units are returned as recorded, without conversion.

Errors use the measurement error envelope: `code`, `message`, `field_errors`,
`request_id`. Missing authentication returns 401; inactive accounts return 403;
missing/inaccessible patients normally return 404 (unverified doctors return
403); invalid UUIDs/pagination return 422. Unexpected failures return a safe
500 response. Responses carry `X-Request-ID` and `Cache-Control: no-store`.

The companion `measurement-history.openapi.json` is an endpoint-specific OpenAPI
snapshot, including its referenced schemas. Runtime `/openapi.json` remains the
complete API definition.

## Verification

Run HTTP route tests without a database:

```sh
python -m unittest discover -s tests -p test_measurements_review.py -v
```

Run PostgreSQL tests against a **dedicated test database migrated to head**:

```powershell
$env:ML102_TEST_DATABASE_URL = 'postgresql+asyncpg://USER:PASSWORD@localhost/medilink_test'
python -m unittest discover -s tests -p test_measurement_history_postgres.py -v
```

Only verified Firebase identity claims are substituted in the PostgreSQL tests.
The real routes, role/profile/grant queries, create/correction transactions and
history serialization execute. Each test rolls back its synthetic data using
an outer transaction. Coverage includes successive corrections, preserved units
and values, tied-timestamp ordering across pages, empty/out-of-range pages,
pagination validation, missing authentication, cross-patient isolation, doctor
grant checks and revoked patient/doctor roles. Skipped database tests are not
proof of integration success.
