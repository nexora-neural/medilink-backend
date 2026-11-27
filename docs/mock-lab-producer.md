# Mock laboratory report producer

Use this local client with an active integration credential and a patient-created upload request. Use synthetic files and data for demos. The API accepts readable PDF, JPEG and PNG files up to 10 MiB. Obtain the API key through the integration client approval process; do not commit it or patient tokens.

Set `MEDILINK_LAB_API_KEY` and `MEDILINK_LAB_REQUEST_TOKEN` in the environment, then run:

```sh
python scripts/mock_lab_report.py --file synthetic.pdf \
  --external-report-id demo-report-001 \
  --idempotency-key demo-report-key-001 --lab-reference DEMO-LAB-001 \
  --report-type 'Synthetic blood test'
```

The default server is `http://localhost:8000`; use `--base-url` to select another server. The response contains the accepted report ID. To correct that report, repeat the command with a new external report ID and idempotency key, add `--correct-report-id REPORT_ID --reason 'Correction reason'`, and use the original request token and lab reference. Optional `--report-date` and `--notes` provide metadata. Reuse the same idempotency key and identical content when retrying a request.

The command exits with status 0 for a successful response and 1 for an HTTP or transport failure. It prints the server response, so avoid sharing output that contains sensitive identifiers.

For database-backed checks, set `ML116_TEST_DATABASE_URL` to a dedicated migrated PostgreSQL database and run `python -m unittest tests.test_ml139_postgres -v`. The tests use synthetic data and roll back their changes.
