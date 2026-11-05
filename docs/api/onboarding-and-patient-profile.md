# ML-22 onboarding and patient profile contract

This records the agreed ML-22 adjustments to the MediLink API contract. The
backend's generated OpenAPI schema exposes the complete request and response
models. All operations use a Firebase ID bearer token and the `/api/v1` prefix.

## Onboarding

`POST /me/onboard` accepts `display_name` and `requested_role`, with optional
`phone`. The requested role is `patient`, `doctor`, `pharmacist`, or
`lab_staff`. Patient onboarding also requires `date_of_birth` (an ISO date no
later than today) and `sex` (`female` or `male`). These patient fields are not
accepted for professional onboarding. A successful request returns the `Me`
response with HTTP 200.

The verified Firebase UID identifies the account. Retrying the request
reconciles missing local records without creating duplicate accounts; matching
email addresses do not merge accounts. Professional onboarding creates an
unverified profile and does not grant verified professional privileges.

## Profile operations

- `GET /me` returns the current account, roles, memberships, and available profiles.
- `PATCH /me` updates the current account's display name or phone.
- `PATCH /me/patient-profile` updates the current patient's own profile. Sex
  values are `female` and `male`; a birth date cannot be in the future.

Empty PATCH requests and unknown fields are rejected. Omitted fields remain
unchanged; explicit null clears only nullable fields. Changes and their audit
events are committed in the same transaction. Error responses use the contract's
`code`, `message`, `field_errors`, and `request_id` fields. Private responses
have `Cache-Control: no-store`.
