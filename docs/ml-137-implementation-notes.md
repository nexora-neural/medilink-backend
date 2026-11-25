# ML-137 implementation notes

This note describes local backup setup and recovery. A deployed backup is only
operational after its worker and protected storage have been configured and
its own recovery procedure has been verified.

## Local operation

The backup account needs read access to PostgreSQL and all document files, a
compatible `pg_dump`, and a POSIX filesystem supporting advisory locks and hard
links. All application processes and reconciliation jobs must share the same
private storage directory and use the application's locking protocol. Other
programs must not modify or delete files in that directory. The backup fails
closed if any referenced document is missing or its size or SHA-256 does not
match its database record (including records without a SHA-256).
The application, backup and reconciliation processes must run as the same OS
user so they can share the owned, private lock file.
The API can start on Windows without `fcntl`; the backup worker requires POSIX
locking and is not supported on Windows. If `pg_dump` is still running after
one hour, the worker stops it, records the attempt as failed and releases the
backup and storage locks; the daily scheduler can then retry.
The encrypted format limits the complete plaintext archive to 2^36 - 32 bytes
and its manifest to 1 MiB; attempts beyond either limit fail without publishing
a successful backup.

Create a private backup directory and a separate private key directory, both
owned by the backup account and inaccessible to others. Generate a 32-byte
random key once into an exclusively created `0600` file. Keep a protected,
independent copy of the key; losing it makes the backups unrecoverable. Do not
place the key in the repository, backup directory, command arguments, or logs.
Set the normal `DB_*` and `PRIVATE_UPLOAD_DIR` settings for the service, then
run from the project root with its virtual environment:

```sh
python -c 'import os; fd = os.open("/secure/keys/backup.key", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600); os.write(fd, os.urandom(32)); os.close(fd)'
```

Run the backup with:

```sh
python -m app.modules.operations.backup_runner --directory /secure/backups --key-file /secure/keys/backup.key
```

For automatic daily operation, run this standalone worker under a service
supervisor with the same working directory, environment, and OS user as the
application. This example runs at 02:00 UTC:

```sh
python -m app.modules.operations.backup_scheduler --directory /secure/backups --key-file /secure/keys/backup.key --hour-utc 2
```

The worker attempts the current day's backup on startup if that day's scheduled
time has passed. It checks successful daily jobs under the same database lock,
so restarting the worker does not create another successful daily backup on that
UTC day. Failures, database outages and lock contention are retried after 15
minutes; days missed while the worker is stopped are not backfilled. The worker
must be started and supervised by the operator; adding this code alone does not
start a process or install a service. Manual one-shot backups are independent
of the daily worker. Check scheduler failures as well as `job_runs`, `backup_runs`, and
audit events. An unavailable database can prevent any status record from being
written. Overlapping runs are rejected. After a process interruption, the next
run marks its previously running records failed; an already published archive
may need manual investigation. Backup history is never automatically deleted.
The runner rejects a key file within the backup or private-document directory.

Alternatively, use a service-account crontab instead of the daily worker to run
the one-shot command at 02:00 each day in the host's local timezone (replace
the example paths and configure the service's environment securely). A cron
invocation is a manual one-shot run and does not have the daily worker's retry
or once-per-UTC-day behavior:

```cron
0 2 * * * cd /path/to/medilink-backend && /path/to/venv/bin/python -m app.modules.operations.backup_runner --directory /secure/backups --key-file /secure/keys/backup.key
```

For recovery, first select an archive and use a private, owned `0700` staging
parent with enough free space for plaintext output:

```sh
python -m app.modules.operations.backup_restore --backup /secure/backups/example.mlbackup --key-file /secure/keys/backup.key --staging-parent /secure/restore-staging
```

The command authenticates and validates the archive and prints the path of a
new staging directory containing `database.dump` and `files/`. Restore also
temporarily writes a plaintext tar stream to an unlinked temporary file in that
directory. Use encrypted storage for staging when the host's threat model
requires it; unlinking temporary files does not guarantee secure erasure. Protect
the staging directory and remove only this newly created directory after an
authorized recovery. Restore the dump with `pg_restore --exit-on-error
--no-owner --no-privileges` only to a separately provisioned, empty PostgreSQL
database, then place the staged files in a separately provisioned, empty private
storage directory with appropriate ownership and permissions. Verify the restored
service before switching to it. Do not use `--clean`, overwrite an existing
database or file directory, or infer that a restore was checked from a successful
backup. Recovery is an operator-driven process, not an automatic in-place restore.

## Repeatable isolated live-write test

`tests/test_backup_live_postgres.py` verifies a synthetic upload committed after
the exported snapshot: the restored database and files include the earlier
document and exclude the later one. It is skipped unless explicitly enabled.
Provision three disposable databases with names beginning `ml137_test_` on a local
`/private/tmp` PostgreSQL socket; migrate the empty source with
`alembic upgrade heads`, leave the target empty, and set `DB_HOST`, `DB_PORT`,
`DB_USER`, `DB_NAME`, `ML137_RESTORE_TEST_DATABASE`,
`ML137_SCHEDULED_RESTORE_TEST_DATABASE`, and
`ML137_LIVE_BACKUP_TEST=1` before running that test. The test verifies its source
contains no users and its target contains no tables, and leaves its synthetic
database records in place. Never point it at a project database.

## Required outcomes

- Take daily encrypted local backups containing a mutually consistent PostgreSQL database and private document files while the application is running.
- Keep encryption keys separate and protected, require the documented key for restore, and expose success/failure with backup and job records and audit events.
- Never remove project records or historical data as part of backup or cleanup.
- Prove restore against a disposable isolated database and synthetic private files; leave `restore_check_status` as `not_run` until independently verified.

## Important implementation constraints

The document uploader writes a file before committing its database row; failed or ambiguous commits can leave files behind. A normal database dump followed by copying the entire mutable directory does not establish a consistent snapshot. A backup must identify the rows and storage keys from the same PostgreSQL snapshot used by `pg_dump`, protect referenced files against concurrent deletion/reconciliation until copied, and fail closed if a referenced object is missing or does not match its recorded size/hash. Backup, upload, and offline reconciliation need an explicit coordination protocol. A stop-all-writers instruction alone does not satisfy the stated running-services criterion.

Use a consistent PostgreSQL snapshot, deterministic manifest and an archive authenticated with a suitable modern encryption scheme. Backup creation does not write a plaintext archive; restore staging uses plaintext as described above. Avoid secrets in process arguments, environment dumps, command output, Git, or backup metadata. Require restrictive file/key permissions and never record keys, paths, or patient data in audit events. The exact encryption/key provisioning and scheduling mechanisms must be validated against the project environment and documented.

Status records must distinguish started, succeeded and failed; only report succeeded and both inclusion flags after the archive is safely written and verified. Handle crash, overlapping schedules, partial output, database unavailability, corrupted/missing document files and unavailable encryption keys without falsely reporting success. Do not claim a restore was tested merely because an archive was written.

## Scope boundaries

ML-192 covers broader backup/restore integration. This ticket still requires a consistent encrypted local snapshot and a documented key-based restore path; integration-specific follow-up cannot stand in for the Jira Done condition.
