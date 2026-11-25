"""Seed connected, fictional clinical records for local demo rehearsals.

This module creates a synthetic private report file, but no Firebase accounts.
See the local demo documentation for identity and storage requirements.
"""

import argparse
import asyncio
import hashlib
import os
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.database import models as _models  # noqa: F401
from app.database.base import Base
from app.database.seed_demo import sample_id
from app.database.session import engine
from app.modules.documents import storage

REPORT_BYTES = Path(__file__).with_name("synthetic_lab_report.png").read_bytes()
PRESCRIPTION_BYTES = Path(__file__).with_name("synthetic_prescription.png").read_bytes()

NAMESPACE = UUID("57c09dcc-536b-43e4-9742-1b1ab772e7c0")


def demo_id(name: str) -> UUID:
    return uuid5(NAMESPACE, name)


async def seed(
    connection, now: datetime, patient_uid: str | None = None, doctor_uid: str | None = None
) -> None:
    """Insert only missing deterministic records in the caller's transaction."""
    tables = Base.metadata.tables

    async def add(table_name, values, key=("id",)):
        table = tables[table_name]
        existing = (
            (await connection.execute(select(table).filter_by(**{k: values[k] for k in key})))
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            # Never silently attach to records owned by someone else.
            identity_fields = {"storage_key", "sha256", "byte_size", "media_type"}
            if any(
                existing[k] != v
                for k, v in values.items()
                if k.endswith("_id")
                or bool(table.c[k].foreign_keys)
                or (table_name == "documents" and k in identity_fields)
                or (table_name == "users" and k == "firebase_uid")
            ):
                raise RuntimeError(f"Synthetic {table_name} identity collision")
            return
        await connection.execute(insert(table).values(**values))

    if (patient_uid is None) != (doctor_uid is None):
        raise ValueError("Both patient and doctor UIDs must be provided together")
    if patient_uid is None:
        patient_uid, doctor_uid = "synthetic:ml26:patient", "synthetic:ml26:doctor"

    async def resolve(uid, role):
        users, roles, assignments = tables["users"], tables["roles"], tables["user_roles"]
        stmt = (
            select(users.c.id)
            .join(assignments, users.c.id == assignments.c.user_id)
            .join(roles, roles.c.id == assignments.c.role_id)
            .where(
                users.c.firebase_uid == uid,
                users.c.status == "active",
                roles.c.code == role,
                assignments.c.revoked_at.is_(None),
            )
        )
        identifier = await connection.scalar(stmt)
        if identifier is None:
            raise RuntimeError(f"Active synthetic {role} test account with current role required")
        return identifier

    patient, doctor = await resolve(patient_uid, "patient"), await resolve(doctor_uid, "doctor")
    if patient == doctor:
        raise ValueError("The patient and doctor test accounts must be different")
    # The default identities have deterministic IDs; refuse pre-existing collisions.
    if patient_uid == "synthetic:ml26:patient" and patient != sample_id("user", "patient"):
        raise RuntimeError("Synthetic patient identity collision")
    if doctor_uid == "synthetic:ml26:doctor" and doctor != sample_id("user", "doctor"):
        raise RuntimeError("Synthetic doctor identity collision")

    if patient_uid != "synthetic:ml26:patient":
        if not await connection.scalar(
            select(tables["patient_profiles"].c.user_id).where(
                tables["patient_profiles"].c.user_id == patient
            )
        ):
            raise RuntimeError("Patient test account must already have a patient profile")
    if doctor_uid != "synthetic:ml26:doctor":
        profile = (
            (
                await connection.execute(
                    select(tables["professional_profiles"]).where(
                        tables["professional_profiles"].c.user_id == doctor
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if (
            profile is None
            or profile["role"] != "doctor"
            or profile["verification_status"] != "approved"
        ):
            raise RuntimeError("Doctor test account must already have an approved doctor profile")

    await add(
        "patient_profiles",
        {"user_id": patient, "date_of_birth": date(1990, 1, 1), "sex": "female"},
        ("user_id",),
    )
    await add(
        "professional_profiles",
        {
            "user_id": doctor,
            "role": "doctor",
            "doctor_reference": "SYNTHETIC-DEMO-DOCTOR",
            "verification_status": "approved",
        },
        ("user_id",),
    )
    for role, org_type, label in (
        ("pharmacist", "pharmacy", "Synthetic demo pharmacy"),
        ("lab_staff", "laboratory", "Synthetic demo laboratory"),
    ):
        user_id, org_id = demo_id(role), demo_id(org_type)
        await add(
            "users",
            {
                "id": user_id,
                "firebase_uid": f"synthetic:ml123:{role}",
                "display_name": f"Synthetic {role}",
                "status": "active",
            },
        )
        role_id = await connection.scalar(
            select(tables["roles"].c.id).where(tables["roles"].c.code == role)
        )
        if role_id is None:
            raise RuntimeError(f"Missing {role} role; run seed_demo first")
        await add("user_roles", {"user_id": user_id, "role_id": role_id}, ("user_id", "role_id"))
        await add(
            "professional_profiles",
            {"user_id": user_id, "role": role, "verification_status": "approved"},
            ("user_id",),
        )
        await add(
            "organizations", {"id": org_id, "name": label, "type": org_type, "status": "active"}
        )
        await add(
            "organization_memberships",
            {
                "id": demo_id(f"{role}-membership"),
                "organization_id": org_id,
                "user_id": user_id,
                "role": role,
                "status": "approved",
                "verified_at": now,
            },
        )

    grant = demo_id("grant")
    await add(
        "access_grants",
        {
            "id": grant,
            "patient_user_id": patient,
            "professional_user_id": doctor,
            "allow_consultation": True,
            "starts_at": now - timedelta(days=3),
            "expires_at": now + timedelta(days=365),
        },
    )
    for category in (
        "history_allergies",
        "prescriptions",
        "reports",
        "measurements_vaccinations",
        "medication_logs",
    ):
        await add(
            "grant_categories", {"grant_id": grant, "category": category}, ("grant_id", "category")
        )
    consultation = demo_id("consultation")
    await add(
        "consultations",
        {
            "id": consultation,
            "patient_user_id": patient,
            "doctor_user_id": doctor,
            "grant_id": grant,
            "started_at": now - timedelta(days=2),
            "ended_at": now - timedelta(days=2, minutes=-30),
            "summary": "Synthetic demo consultation",
        },
    )
    clinical = {
        "patient_user_id": patient,
        "consultation_id": consultation,
        "recorded_by": doctor,
        "source": "doctor",
    }
    await add(
        "conditions",
        {
            "id": demo_id("condition"),
            **clinical,
            "condition_name": "Synthetic condition",
            "recorded_at": now - timedelta(days=2),
        },
    )
    await add(
        "diagnoses",
        {
            "id": demo_id("diagnosis"),
            **clinical,
            "diagnosis_name": "Synthetic diagnosis",
            "diagnosed_at": now - timedelta(days=2),
        },
    )
    await add(
        "allergies",
        {
            "id": demo_id("allergy"),
            **clinical,
            "allergen": "Synthetic allergen",
            "reaction": "Synthetic reaction",
            "recorded_at": now - timedelta(days=2),
        },
    )
    await add(
        "clinical_notes",
        {
            "id": demo_id("note"),
            **clinical,
            "note_type": "consultation",
            "note_text": "Synthetic clinical note.",
            "recorded_at": now - timedelta(days=2),
        },
    )
    medicine, rx, item = (demo_id(x) for x in ("medicine", "prescription", "item"))
    await add(
        "medicines",
        {
            "id": medicine,
            "generic_name": "Synthetic medicine",
            "strength": "100 mg",
            "form": "tablet",
            "is_sample": True,
        },
    )
    await add(
        "prescriptions",
        {
            "id": rx,
            "patient_user_id": patient,
            "consultation_id": consultation,
            "prescriber_user_id": doctor,
            "source": "doctor",
            "status": "active",
            "issued_at": now - timedelta(days=2),
            "created_by": doctor,
        },
    )
    await add(
        "prescription_items",
        {
            "id": item,
            "prescription_id": rx,
            "medicine_id": medicine,
            "medicine_name_snapshot": "Synthetic medicine 100 mg",
            "dose_text": "1 tablet",
            "frequency": "daily",
            "quantity": Decimal("30"),
            "quantity_unit": "tablet",
        },
    )
    second_medicine = demo_id("medicine-second")
    await add(
        "medicines",
        {
            "id": second_medicine,
            "generic_name": "Synthetic medicine B",
            "strength": "25 mg",
            "form": "tablet",
            "is_sample": True,
        },
    )
    await add(
        "prescription_items",
        {
            "id": demo_id("item-second"),
            "prescription_id": rx,
            "medicine_id": second_medicine,
            "medicine_name_snapshot": "Synthetic medicine B 25 mg",
            "dose_text": "1 tablet",
            "frequency": "daily",
            "quantity": Decimal("14"),
            "quantity_unit": "tablet",
        },
    )
    original = demo_id("ocr-document")
    await add(
        "documents",
        {
            "id": original,
            "owner_user_id": patient,
            "patient_user_id": patient,
            "uploaded_by": patient,
            "source": "patient",
            "document_type": "prescription",
            "original_filename": "synthetic-prescription.png",
            "storage_key": original.hex,
            "media_type": "image/png",
            "byte_size": len(PRESCRIPTION_BYTES),
            "sha256": hashlib.sha256(PRESCRIPTION_BYTES).hexdigest(),
        },
    )
    await add(
        "prescription_ocr_jobs",
        {
            "id": demo_id("ocr-failed"),
            "patient_user_id": patient,
            "original_document_id": original,
            "requested_by_user_id": patient,
            "status": "failed",
            "attempt_count": 1,
            "error_code": "SYNTHETIC_OCR_FAILURE",
        },
    )
    patient_rx = demo_id("patient-prescription")
    await add(
        "prescriptions",
        {
            "id": patient_rx,
            "patient_user_id": patient,
            "source": "patient",
            "original_document_id": original,
            "status": "draft",
            "review_status": "not_reviewed",
            "created_by": patient,
        },
    )
    await add(
        "prescription_items",
        {
            "id": demo_id("patient-item"),
            "prescription_id": patient_rx,
            "medicine_name_snapshot": "Synthetic patient-added item",
            "dose_text": "1 tablet",
            "frequency": "daily",
        },
    )
    # Historical, revoked sharing evidence only: no usable claim token is seeded.
    share = demo_id("historical-share")
    await add(
        "prescription_shares",
        {
            "id": share,
            "prescription_id": rx,
            "patient_user_id": patient,
            "claim_token_hash": hashlib.sha256(b"fictional-revoked-demo-share").hexdigest(),
            "status": "revoked",
            "claim_expires_at": now - timedelta(hours=2),
            "claimed_by": demo_id("pharmacist"),
            "claimed_organization_id": demo_id("pharmacy"),
            "claimed_at": now - timedelta(hours=4),
            "confirmed_at": now - timedelta(hours=3, minutes=50),
            "confirmed_access_expires_at": now - timedelta(hours=1),
            "revoked_at": now - timedelta(hours=2),
            "created_at": now - timedelta(hours=5),
        },
    )
    event = demo_id("historical-dispensing")
    await add(
        "dispensing_events",
        {
            "id": event,
            "prescription_id": rx,
            "pharmacist_user_id": demo_id("pharmacist"),
            "organization_id": demo_id("pharmacy"),
            "share_id": share,
            "request_key": "fictional-historical-dispensing-ml123",
            "dispensed_at": now - timedelta(hours=3),
            "notes": "Synthetic partial dispensing",
        },
    )
    await add(
        "dispensing_items",
        {
            "id": demo_id("historical-dispensing-item"),
            "event_id": event,
            "prescription_item_id": item,
            "quantity": Decimal("10"),
            "quantity_unit": "tablet",
        },
    )
    schedule = demo_id("schedule")
    await add(
        "medication_schedules",
        {
            "id": schedule,
            "prescription_item_id": item,
            "patient_user_id": patient,
            "timezone": "Asia/Colombo",
            "starts_at": now - timedelta(days=1),
            "confirmed_at": now - timedelta(days=1),
        },
    )
    await add(
        "schedule_times",
        {"id": demo_id("schedule-time"), "schedule_id": schedule, "time_of_day": time(8, 0)},
    )
    occurrence = demo_id("dose")
    await add(
        "dose_occurrences",
        {
            "id": occurrence,
            "schedule_id": schedule,
            "scheduled_for": now - timedelta(hours=1),
            "status": "taken",
        },
    )
    await add(
        "medication_logs",
        {
            "id": demo_id("dose-log"),
            "dose_occurrence_id": occurrence,
            "response": "taken",
            "reported_taken_at": now - timedelta(minutes=50),
            "recorded_by": patient,
        },
    )
    for name, status, hours in (
        ("dose-taken-earlier", "taken", 4),
        ("dose-skipped", "skipped", 3),
        ("dose-unreported", "unreported", 2),
        ("dose-upcoming", "upcoming", -2),
    ):
        occurrence_id = demo_id(name)
        scheduled = now - timedelta(hours=hours)
        await add(
            "dose_occurrences",
            {
                "id": occurrence_id,
                "schedule_id": schedule,
                "scheduled_for": scheduled,
                "status": status,
            },
        )
        if status in {"taken", "skipped"}:
            log = {
                "id": demo_id(f"{name}-log"),
                "dose_occurrence_id": occurrence_id,
                "response": status,
                "recorded_by": patient,
            }
            if status == "taken":
                log["reported_taken_at"] = scheduled + timedelta(minutes=10)
            await add("medication_logs", log)
    for kind, value, unit in (("weight", "68.0", "kg"), ("blood_glucose", "5.4", "mmol/L")):
        await add(
            "measurements",
            {
                "id": demo_id(kind),
                "patient_user_id": patient,
                "type": kind,
                "value": Decimal(value),
                "unit": unit,
                "measured_at": now,
                "recorded_by": patient,
                "source": "patient",
            },
        )
    for index, weight in enumerate(("70.0", "69.0"), start=1):
        await add(
            "measurements",
            {
                "id": demo_id(f"weight-history-{index}"),
                "patient_user_id": patient,
                "type": "weight",
                "value": Decimal(weight),
                "unit": "kg",
                "measured_at": now - timedelta(days=30 * (3 - index)),
                "recorded_by": patient,
                "source": "patient",
            },
        )
    template = demo_id("template")
    await add(
        "prescription_templates",
        {
            "id": template,
            "doctor_user_id": doctor,
            "name": "Synthetic demo template",
            "template_type": "template",
        },
    )
    await add(
        "prescription_template_items",
        {
            "id": demo_id("template-item"),
            "template_id": template,
            "position": 1,
            "medicine_id": medicine,
            "medicine_name_snapshot": "Synthetic medicine 100 mg",
            "dose_text": "1 tablet",
            "frequency": "daily",
        },
    )
    document = demo_id("report-document")
    await add(
        "documents",
        {
            "id": document,
            "owner_user_id": patient,
            "patient_user_id": patient,
            "uploaded_by": patient,
            "source": "patient",
            "document_type": "report",
            "original_filename": "synthetic-report.png",
            "storage_key": document.hex,
            "media_type": "image/png",
            "byte_size": len(REPORT_BYTES),
            "sha256": hashlib.sha256(REPORT_BYTES).hexdigest(),
        },
    )
    await add(
        "reports",
        {
            "id": demo_id("report"),
            "patient_user_id": patient,
            "document_id": document,
            "report_type": "Synthetic lab report",
            "report_date": date(2025, 1, 1),
            "source": "patient",
            "uploaded_by": patient,
        },
    )
    await add(
        "vaccinations",
        {
            "id": demo_id("vaccination"),
            "patient_user_id": patient,
            "vaccine_name": "Synthetic vaccine",
            "administered_on": date(2025, 1, 1),
            "recorded_by": patient,
            "source": "patient",
        },
    )


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--patient-uid", help="Firebase UID of a dedicated synthetic patient test account"
    )
    parser.add_argument(
        "--doctor-uid", help="Firebase UID of a dedicated synthetic doctor test account"
    )
    args = parser.parse_args()
    if bool(args.patient_uid) != bool(args.doctor_uid):
        parser.error("provide both --patient-uid and --doctor-uid")
    if not args.apply:
        parser.error("pass --apply to insert synthetic demo records")
    if os.getenv("APP_ENV", "").lower() not in {"local", "development", "dev", "test"}:
        parser.error(
            "set APP_ENV explicitly to local, development, dev or test to seed synthetic records"
        )
    try:
        created_paths = []
        try:
            for name, content in (
                ("report-document", REPORT_BYTES),
                ("ocr-document", PRESCRIPTION_BYTES),
            ):
                path = storage._path(demo_id(name).hex)
                try:
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:
                    if not path.is_file() or path.is_symlink() or path.read_bytes() != content:
                        raise RuntimeError("Synthetic document storage collision; seed aborted")
                else:
                    created_paths.append(path)
                    with os.fdopen(fd, "wb") as output:
                        output.write(content)
            async with engine.begin() as connection:
                await seed(connection, datetime.now(UTC), args.patient_uid, args.doctor_uid)
        except BaseException:
            for path in created_paths:
                await storage.delete(path.name)
            raise
        print("Synthetic demo records seeded.")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
