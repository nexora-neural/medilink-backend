# Authorization guards

These helpers are building blocks, not automatic route protection. Each protected
route must authenticate the caller and explicitly apply the relevant checks.

`get_current_user` checks the verified Firebase identity against the local
account. Pending professionals may use their own profile endpoints; suspended
and disabled accounts are denied. `require_role` requires an active account
and the stated local role.

For a doctor accessing patient information, load the target from the database
and call `require_doctor_grant(session, user, patient_id, organization_id,
category)`. Set `clinical_write=True` for a clinical write. The helper checks
current role, professional approval, active organization, approved membership,
grant category, time window and revocation, and active consultation when needed.
No admin role grants implicit clinical access. The agreed policy requires an
active organization and approved membership for doctor clinical access. A grant
without an organization is denied. The future grant-creation endpoint must reject
doctor grants without an organization; the database column is still nullable.

For a patient correction, load and lock the target record in the transaction;
pass its persisted patient, author, source and status to
`require_patient_source_correction`. Never supply these values directly from a
request body. The guard does not implement version creation, per-resource
status checks, concealed-resource 404 responses, or all source-specific rules.

Authorization must be checked for each request, including downloads and exports,
against the parent resource. Add route-level and database tests when each future
clinical endpoint is implemented. Mock integration clients, pharmacy sharing,
lab requests, privileged MFA, and additional source rules require their own
explicit guards; these helpers do not cover them.
