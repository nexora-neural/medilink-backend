# Prescription OCR API

This API accepts an active prescription document already stored for the authenticated patient. It supports detected PDF, JPEG and PNG files up to the existing 10 MiB document limit. It processes printed English text only; handwriting recognition is out of scope. The original document remains private evidence and is never replaced by OCR output.

OCR output is untrusted draft material. It does not clinically verify a prescription, issue an order, or authorize dispensing. The patient must review and correct the extracted fields, create a patient-source draft, and use the existing explicit confirmation action separately.

## Endpoints

| Method and path | Purpose | Success |
| --- | --- | --- |
| `POST /api/v1/patients/{patient_id}/prescription-ocr-jobs` | Queue OCR for an eligible, owned prescription document | `202` with `queued` job; requires `Idempotency-Key` |
| `GET /api/v1/patients/{patient_id}/prescription-ocr-jobs` | List the patient's jobs | `200`; supports `status`, `page` and `page_size` |
| `GET /api/v1/prescription-ocr-jobs/{job_id}` | Read state and extracted suggestions | `200` |
| `POST /api/v1/prescription-ocr-jobs/{job_id}/retry` | Requeue an eligible failed job | `202`; JSON body requires a `reason` |
| `POST /api/v1/patients/{patient_id}/prescription-ocr-jobs/{job_id}/draft` | Save patient-reviewed fields as a draft | `201`; requires `Idempotency-Key` |

All read and write operations are patient-owner only. Responses containing job or extracted data use `Cache-Control: no-store`. Missing or non-owned jobs are concealed as not found. Unknown query or body fields are rejected.

The job state flow is:

```text
queued -> processing -> ready_for_review -> draft_created
                      \-> failed -> queued (eligible retry)
```

The retry endpoint only accepts failed jobs. A repeated retry while the job is queued or processing receives `409`; row locks prevent concurrent attempts from being enqueued twice. The original document is reused. Previous attempt starts and outcomes are retained in append-only audit events, with no document text or retry reason in audit metadata.

## OCR engine and operations

The implementation uses local Tesseract OCR with the English (`eng`) language data. PDF pages are rendered locally with Poppler `pdftoppm`. Neither document bytes nor extracted text are sent to an external provider. Raw OCR text is held in worker memory only and is not returned or persisted; the API stores only structured suggestion fields. The parser conservatively recognizes labeled prescriber/date lines and printed medicine lines containing a strength and recognizable frequency. A dose is populated only when an explicit dose and unit appear in the directions (for example, “Take 2 capsules”); product strength is never substituted for dose. An unclear or unstated dose is returned as `null` so the patient must enter it during review. Other unrecognized fields are omitted for the patient to fill or correct.

Worker completion is conditional on both the job still being in `processing` and the attempt count still matching the worker's claimed attempt. If stale-job recovery has requeued the job and another worker has claimed a newer attempt, the older worker discards its result without changing the job or recording a completion audit event.

Install the system tools on macOS with:

```sh
brew install tesseract poppler
```

On Linux, install the distribution packages that provide `tesseract` with English data and `pdftoppm` (commonly `tesseract-ocr` and `poppler-utils`). The Python package locks do not bundle these native tools.

Run a worker process alongside the API, with the same PostgreSQL database and private document-storage volume mounted:

```sh
python -m app.workers.prescription_ocr
```

Configuration keys are `PRESCRIPTION_OCR_TESSERACT_CMD`, `PRESCRIPTION_OCR_PDF_RENDERER_CMD`, `PRESCRIPTION_OCR_TIMEOUT_SECONDS` (default 90 seconds per job), and `PRESCRIPTION_OCR_MAX_ATTEMPTS` (default 3 total executions including the first). The maximum follows the existing operations worker convention; the ML-152 Jira contract says retries are bounded but does not state an OCR-specific number. Treat this default as an implementation assumption pending product confirmation. A timed-out or interrupted processing claim can be recovered after the configured execution timeout plus a 30-second grace period.

Every attempt runs in a private temporary directory under the configured private upload root; the directory is removed after processing. The OCR worker opens the retained source read-only. It records only stable status/error codes and attempt counts in the database and audit stream. The original document must remain available to the worker through the same private storage path.

## Contract reference

The request/response schemas, paths, filters, response codes and job state names follow [the approved API contract](../reference-contract/02_API_CONTRACT.md#prescription-ocr) and [OpenAPI baseline](../reference-contract/03_OPENAPI.json). No migration is added because the OCR job table and fields are supplied by ML-106. Database schema documentation remains within ML-200; private-file role walkthroughs remain within ML-201.
