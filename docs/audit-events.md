# Audit events

`audit_events` is an append-only evidence table. The database migration installs a PostgreSQL trigger that rejects `UPDATE`, `DELETE`, and `TRUNCATE`; application code may only add events.

## Request events

The request-ID middleware appends one `api.request` event for each `/api/v1` request except health checks. It stores the route template, HTTP method, response status, request ID, and a resolved local user or integration-client ID when available. It never reads or stores request bodies, response bodies, query values, headers, bearer tokens, file names, or raw path parameters. Outcomes are `success`, `denied` for 401/403, and `failed` for other error responses.

The event is written after the route returns, in a separate short database transaction. If the audit store is unavailable, the request ID is logged for operations and the application does not log request payloads or credentials.

## Business events

For a state-changing operation, call `app.modules.audit.service.append_event` with the same `AsyncSession` and before committing the business transaction. This keeps the state change and its audit event atomic. Use stable action names, resource type and ID, actor identity, outcome, and request ID. Only the keys listed in `SAFE_METADATA_KEYS` are accepted; free-form reasons, clinical details, credentials, and request payloads must stay out of audit metadata.

Worker and backup code should call `record_job_attempt` and `record_backup_run` in the transaction that persists their run status. These helpers record attempt/status metadata only, never job payloads, backup contents, or error text.

## History and corrections

There is no audit-event read, update, or delete API. Existing clinical history routers do not expose edit or physical-delete operations. Corrections must be represented as a new linked/superseding record with its reason, preserving the original clinical values. The correction workflow must not put the reason or clinical content in audit metadata.
