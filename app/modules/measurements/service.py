import hashlib
import json

from sqlalchemy import text

from .repository import (
    GRANT_ACCESS,
    fetch_one,
    insert_reading,
    lock_owned_reading,
    mark_superseded,
)
from .schemas import MeasurementsRead


class MeasurementError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code


async def measurement_access(db, actor, patient, *, write=False, consultation=None):
    profile = await fetch_one(db, """
        SELECT u.status FROM patient_profiles p JOIN users u ON u.id = p.user_id
        WHERE p.user_id = :patient
    """, patient=patient)
    if profile is None:
        raise MeasurementError(404, "NOT_FOUND", "Patient record not found.")
    if actor["id"] == patient and actor["is_patient"]:
        if consultation is not None:
            raise MeasurementError(
                422,
                "VALIDATION_ERROR",
                "Omit consultation_id for a patient entry.",
            )
        return "patient"
    if not actor["is_verified_doctor"]:
        if actor["is_doctor"]:
            raise MeasurementError(403, "FORBIDDEN", "Doctor verification is required.")
        raise MeasurementError(404, "NOT_FOUND", "Patient record not found.")

    params = dict(patient=patient, actor=actor["id"])
    grant = await fetch_one(db, f"""
        SELECT g.id FROM access_grants g JOIN grant_categories gc ON gc.grant_id = g.id
        WHERE {GRANT_ACCESS} LIMIT 1
    """, **params)
    if grant is None:
        raise MeasurementError(404, "NOT_FOUND", "Patient record not found.")
    if profile["status"] != "active":
        raise MeasurementError(403, "FORBIDDEN", "Patient account is inactive.")
    if write:
        if consultation is None:
            raise MeasurementError(
                422,
                "VALIDATION_ERROR",
                "consultation_id is required for doctors.",
            )
        permitted = await fetch_one(db, f"""
            SELECT c.id FROM consultations c JOIN access_grants g ON g.id = c.grant_id
            JOIN grant_categories gc ON gc.grant_id = g.id
            WHERE {GRANT_ACCESS} AND g.allow_consultation AND c.id = :consultation
              AND c.patient_user_id = :patient AND c.doctor_user_id = :actor
              AND c.organization_id IS NOT DISTINCT FROM g.organization_id
              AND c.started_at <= statement_timestamp() AND c.ended_at IS NULL
            FOR SHARE OF c, g, gc
        """, **params, consultation=consultation)
        if permitted is None:
            raise MeasurementError(
                403,
                "FORBIDDEN",
                "An active authorized consultation is required.",
            )
    return "doctor"


async def audit(db, actor, action, reading, request_id, metadata):
    await db.execute(text("""
        INSERT INTO audit_events
            (actor_user_id, action, target_type, target_id, outcome, request_id, metadata)
        VALUES (:actor, :action, 'measurement', :target, 'success', :request_id,
                CAST(:metadata AS jsonb))
    """), dict(actor=actor, action=action, target=reading.id,
               request_id=request_id, metadata=json.dumps(metadata)))


async def create_measurement(db, actor, patient, body, request_id):
    source = await measurement_access(
        db, actor, patient, write=True, consultation=body.consultation_id,
    )
    row = await insert_reading(db, body, patient, actor["id"], body.type, source)
    reading = MeasurementsRead.model_validate(row)
    metadata = {"source": source}
    if body.consultation_id is not None:
        metadata["consultation_id"] = str(body.consultation_id)
    await audit(db, actor["id"], "measurement.created", reading, request_id, metadata)
    return reading


async def correct_measurement(db, actor, measurement_id, body, key, request_id, request_json):
    original = await lock_owned_reading(db, measurement_id, actor["id"])
    if original is None:
        raise MeasurementError(404, "NOT_FOUND", "Measurement not found.")
    if not actor["is_patient"] or original["source"] != "patient":
        raise MeasurementError(403, "FORBIDDEN", "Only your own patient entries can be corrected.")

    digest = hashlib.sha256(json.dumps(
        request_json, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode()).hexdigest()
    scope = dict(actor=actor["id"], target=str(measurement_id), key=key)
    previous = await fetch_one(db, """
        SELECT request_digest, response_body FROM api_idempotency_records
        WHERE actor_type = 'user' AND actor_id = :actor
          AND operation_key = 'correctPatientMeasurement'
          AND target_key = :target AND idempotency_key = :key
    """, **scope)
    if previous is not None:
        if previous["request_digest"] != digest:
            raise MeasurementError(
                409,
                "IDEMPOTENCY_CONFLICT",
                "This key was used with a different request.",
            )
        return MeasurementsRead.model_validate(previous["response_body"])
    if original["status"] != "active":
        raise MeasurementError(409, "STALE_VERSION", "Reload and correct the current version.")
    try:
        body.validate_type(original["type"])
    except ValueError as exc:
        raise MeasurementError(422, "VALIDATION_ERROR", str(exc)) from exc

    row = await insert_reading(
        db, body, original["patient_user_id"], actor["id"], original["type"],
        "patient", previous=measurement_id, reason=body.reason,
    )
    reading = MeasurementsRead.model_validate(row)
    await mark_superseded(db, measurement_id)
    await audit(db, actor["id"], "measurement.corrected", reading, request_id, {
        "supersedes_id": str(measurement_id), "source": "patient",
    })
    await db.execute(text("""
        INSERT INTO api_idempotency_records (
            actor_type, actor_id, operation_key, target_key, idempotency_key,
            request_digest, response_status, response_body, expires_at
        ) VALUES (
            'user', :actor, 'correctPatientMeasurement', :target, :key,
            :digest, 201, CAST(:body AS jsonb), 'infinity'::timestamptz
        )
    """), dict(**scope, digest=digest, body=reading.model_dump_json()))
    return reading
