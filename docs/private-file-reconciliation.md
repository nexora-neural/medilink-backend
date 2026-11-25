# Private file reconciliation

Stop all application processes, workers, upload clients, and other reconciliation
jobs before running either command. Ensure no database transactions remain in
flight. This is an offline single-server procedure, not an online sweeper; the
tool cannot verify that writers are stopped. Use the same database and private
storage configuration as the application. Restrict access to command output.

Run `python -m app.modules.documents.reconcile --writers-stopped` for an
inventory. It reports document IDs whose files are missing or not regular files,
and unreferenced regular storage files older than one hour. It does not modify
files or database records. Investigate missing documents and restore verified
files from a matching private backup; do not mark missing content as recovered
without checking its identity, size and digest. Report metadata already published
before loss requires separate operational investigation.

After reviewing the inventory, `python -m app.modules.documents.reconcile
--writers-stopped --quarantine-orphans` moves eligible orphan files to the
private `.quarantine` directory without overwriting existing files. Nothing is
deleted. If a quarantine move fails, keep writers stopped, investigate, and
repeat the inventory before retrying. Retain or dispose of quarantined files
according to the project's retention policy, after verifying database state and
backups. The tool exits nonzero for missing referenced files or errors.

An upload commit can succeed on the database server while its response is lost.
In this case the file is retained and this offline inventory resolves whether it
has a database reference. Do not manually delete it based only on an error log.
These checks do not prevent external file loss or storage failure after publish;
they do not replace backup and restore verification (ML-137 and ML-192).
