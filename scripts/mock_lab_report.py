"""Send a local lab report file to the MediLink integration API."""

import argparse
import mimetypes
import os
import sys
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--external-report-id", required=True)
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument("--lab-reference", required=True)
    parser.add_argument("--report-type", required=True)
    parser.add_argument("--report-date")
    parser.add_argument("--notes")
    parser.add_argument("--correct-report-id")
    parser.add_argument("--reason")
    args = parser.parse_args()
    if bool(args.correct_report_id) != bool(args.reason):
        parser.error("--correct-report-id and --reason must be supplied together")
    api_key = os.environ.get("MEDILINK_LAB_API_KEY")
    if not api_key:
        parser.error("Set MEDILINK_LAB_API_KEY in the environment")
    request_token = os.environ.get("MEDILINK_LAB_REQUEST_TOKEN")
    if not request_token:
        parser.error("Set MEDILINK_LAB_REQUEST_TOKEN in the environment")
    data = {
        "request_token": request_token,
        "external_report_id": args.external_report_id,
        "lab_reference": args.lab_reference,
        "report_type": args.report_type,
    }
    for name in ("report_date", "notes", "reason"):
        value = getattr(args, name)
        if value is not None:
            data[name] = value
    url = args.base_url.rstrip("/") + "/api/v1/integrations/reports"
    if args.correct_report_id:
        url += f"/{args.correct_report_id}/versions"
    try:
        with args.file.open("rb") as report:
            response = httpx.post(
                url,
                data=data,
                files={
                    "file": (
                        args.file.name,
                        report,
                        mimetypes.guess_type(args.file.name)[0] or "application/octet-stream",
                    )
                },
                headers={"X-API-Key": api_key, "Idempotency-Key": args.idempotency_key},
                timeout=30.0,
            )
        print(response.text)
        return 0 if response.is_success else 1
    except (OSError, httpx.HTTPError) as exc:
        print(f"Submission failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
