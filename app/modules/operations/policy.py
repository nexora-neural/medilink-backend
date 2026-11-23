"""Fail-closed visibility rules. New actions require explicit review."""

from sqlalchemy import and_, exists, or_, select

from app.modules.audit.models import AuditEvent
from app.modules.clinical_records.models import (
    AllergiesModel,
    ClinicalNotesModel,
    ConditionsModel,
    DiagnosesModel,
)
from app.modules.consent.models import AccessGrant
from app.modules.consultations.models import Consultation
from app.modules.documents.models import Document
from app.modules.laboratory.models import LabUploadRequest, Report
from app.modules.measurements.models import Measurement
from app.modules.medication.models import MedicationSchedule
from app.modules.prescriptions.core_models import Prescription
from app.modules.vaccinations.models import Vaccination

# Admin sees delegated operational evidence only. Privileged identity, client,
# bootstrap, security and clinical events are deliberately absent.
DELEGATED_ACTIONS = {
    "organization.submitted",
    "organization.decision",
    "membership.requested",
    "membership.decision",
    "medicine.created",
    "job.retry.requested",
    "job.retry.completed",
    "job.retry.cancelled",
    "job.attempt",
    "backup.run",
}

# Ownership always comes from a persisted resource, never the event's actor.
PATIENT_TARGETS = (
    ("access_grant", AccessGrant, {"access_grant.created", "access_grant.revoked"}),
    ("consultation", Consultation, {"consultation.started", "consultation.ended"}),
    ("document", Document, {"document.upload", "document.download", "document.metadata_read"}),
    (
        "lab_upload_request",
        LabUploadRequest,
        {"lab_upload_request.created", "lab_upload_request.revoked", "lab_upload_request.resolved"},
    ),
    ("measurement", Measurement, {"measurement.created", "measurement.corrected"}),
    ("medication_schedule", MedicationSchedule, {"medication_schedule.replaced"}),
    ("prescriptions", Prescription, {"prescription.draft.updated"}),
    (
        "report",
        Report,
        {
            "report.created",
            "report.published",
            "report.corrected",
            "report.read",
        },
    ),
    ("vaccination", Vaccination, {"vaccination.created", "vaccination.corrected"}),
    ("condition", ConditionsModel, {"condition.created", "condition.corrected"}),
    ("diagnosis", DiagnosesModel, {"diagnosis.created", "diagnosis.corrected"}),
    ("allergy", AllergiesModel, {"allergy.created", "allergy.corrected"}),
    ("clinical_note", ClinicalNotesModel, {"clinical_note.created", "clinical_note.corrected"}),
)


def patient_predicate(patient_id):
    owned = [
        and_(
            AuditEvent.target_type == kind,
            AuditEvent.action.in_(actions),
            exists(
                select(model.id).where(
                    model.id == AuditEvent.target_id,
                    model.patient_user_id == patient_id,
                )
            ),
        )
        for kind, model, actions in PATIENT_TARGETS
    ]
    owned.append(
        and_(
            AuditEvent.target_type == "patient_profile",
            AuditEvent.target_id == patient_id,
            AuditEvent.action == "patient_profile.updated",
        )
    )
    return or_(*owned)


SAFE_OUTPUT_METADATA = {
    "status",
    "previous_status",
    "attempt_count",
    "job_type",
    "database_included",
    "files_included",
    "restore_check_status",
    "role",
    "source",
    "created",
    "result_count",
}


def safe_metadata(value):
    # Read projection remains safe even for legacy rows that bypassed validators.
    enums = {
        "status": {
            "active",
            "pending",
            "approved",
            "rejected",
            "suspended",
            "inactive",
            "queued",
            "running",
            "succeeded",
            "failed",
            "cancelled",
            "more_info_requested",
        },
        "previous_status": {
            "active",
            "pending",
            "approved",
            "rejected",
            "suspended",
            "inactive",
            "queued",
            "running",
            "succeeded",
            "failed",
            "cancelled",
        },
        "restore_check_status": {"not_run", "passed", "failed"},
        "role": {"doctor", "pharmacist", "lab_staff", "patient", "admin", "super_admin"},
        "source": {"patient", "doctor", "lab", "integration"},
        "job_type": {"operations.database_check"},
    }
    result = {}
    for key, item in (value or {}).items():
        if key in enums and isinstance(item, str) and item in enums[key]:
            result[key] = item
        elif key in {"attempt_count", "result_count"} and type(item) is int and item >= 0:
            result[key] = item
        elif key in {"database_included", "files_included", "created"} and type(item) is bool:
            result[key] = item
    return result
