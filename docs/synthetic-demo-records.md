# Synthetic clinical demo records

The local development seed provides fictional data only. Run it against a dedicated local or test PostgreSQL database after applying all migrations. Set `APP_ENV=local` (or `test`), the `DB_*` connection settings, and `PRIVATE_UPLOAD_DIR` to that environment's private storage directory. Never point it at a production or shared patient database.

```sh
alembic upgrade head
python -m app.database.seed_demo --apply
python -m app.database.seed_synthetic_journeys --apply
```

The second command can be rerun: deterministic identifiers retain existing seeded rows and avoid creating duplicates. A collision with an unexpected linked owner stops the transaction. The report file is fictional and is created privately with restricted permissions; an incompatible file at its reserved key stops the command. Existing values are retained on rerun, including the grant expiry. To refresh an expired or otherwise modified demo, prepare a fresh dedicated database and private storage directory.

The seed also adds fictional approved pharmacist and lab staff database identities and their synthetic organizations; these identities cannot sign in to Firebase without separate account provisioning. The seed connects the synthetic patient and doctor from `seed_demo` to a consent grant, consultation, clinical history, medicine, prescription and medication schedule with a logged dose, measurements with weight history, vaccination, a doctor-owned template and patient-uploaded report. The report and prescription images are fictional development examples, not clinical documents. Dates and values are fictional. Synthetic database identities are not Firebase credentials: use the documented local test account setup or identity substitution for authenticated API tests. The seed does not create accounts in Firebase.

PostgreSQL integration checks require a dedicated migrated database containing the role/identity seed. Set `DEMO_SEED_TEST_DATABASE_URL` to its asyncpg URL and run `python -m unittest tests.test_synthetic_journeys_postgres -v`. The tests run their clinical writes inside a transaction that is rolled back. They verify reruns, account and document collision safeguards, patient and doctor measurement access, medicine lookup, private report download, the doctor queue and consultation flow, a patient-to-laboratory upload request, and staff membership authorization for the matching organization. The dispensing router is a placeholder in this checkout, so dispensing is not verified by these tests. API tests substitute identity verification and do not test live Firebase sign-in.

For an interactive local demo, dedicated fictional Firebase test accounts can be prepared using the existing `seed_test_account` procedure. The patient must already have a patient profile and an active patient role; the doctor must already have an active doctor role and an approved doctor profile. When explicit UIDs are supplied, the seed does not grant or approve those accounts roles. Use a fresh dedicated database and private storage directory for one account pair, then pass their Firebase UIDs (not passwords):

```sh
python -m app.database.seed_synthetic_journeys --apply --patient-uid "$DEMO_PATIENT_UID" --doctor-uid "$DEMO_DOCTOR_UID"
```

Both IDs must be supplied together. The records use one reserved set of IDs per database; trying to attach them to a different pair later is rejected. The operator is responsible for ensuring the chosen accounts and database are exclusively for synthetic testing. This option has been tested with synthetic local identities; live Firebase authentication has not been verified by the database tests.

## What this seed can currently support

| Journey or data | Fixture and verification |
| --- | --- |
| Patient and doctor access | An active category-scoped grant, patient profile, approved doctor profile and a completed consultation; API tests cover the doctor queue, starting a new consultation and patient/doctor measurement reads. |
| Clinical history and charts | Synthetic condition, diagnosis, allergy and note records plus three weight readings and one blood glucose reading. PostgreSQL tests verify ownership, consultation links and weight ordering. |
| Prescription and medication | An active doctor-authored prescription with two catalogue items and an unreviewed patient-added draft. One schedule includes taken, skipped, unreported and upcoming occurrences; a doctor-owned template reuses a catalogue entry. PostgreSQL tests verify the chain. |
| OCR failure | A fictional prescription image, a failed OCR job and a separate patient-added draft linked to the original. The fixture does not simulate an OCR run or doctor approval. |
| Report | One fictional patient-uploaded PNG and report row. API tests verify owner download and denial to another patient. |
| Pharmacy and laboratory | Fictional approved staff and organization memberships. The lab request create/resolve flow is exercised using the seeded lab. Pharmacy dispensing is not exercised because its router is currently a placeholder. |
| Vaccination | One fictional patient-recorded vaccine. Its patient and recorder links are verified at database level. |

The automated API checks replace identity verification with synthetic test identities. They do not certify live Firebase login or a complete frontend rehearsal. Clinical records and vaccination routers in this checkout have no endpoints, and the final release demo script remains a separate pending deliverable. These limitations should be revisited when the corresponding implementation and agreed demo script are available.
