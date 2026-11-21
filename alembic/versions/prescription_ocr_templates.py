"""Add retained OCR jobs and doctor-owned prescription templates.

Revision ID: prescription_ocr_templates
Revises: 74f97cc7d042
"""

from alembic import op

revision = "prescription_ocr_templates"
down_revision = "74f97cc7d042"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.execute(
        "CREATE TYPE prescription_ocr_job_status AS ENUM "
        "('queued', 'processing', 'ready_for_review', 'failed', 'draft_created')"
    )
    op.execute(
        "CREATE TYPE prescription_template_type AS ENUM ('favourite', 'template')"
    )
    op.execute("CREATE TYPE prescription_template_status AS ENUM ('active', 'archived')")
    op.execute(
        """
        CREATE TABLE prescription_ocr_jobs (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            patient_user_id uuid NOT NULL REFERENCES patient_profiles(user_id) ON DELETE RESTRICT,
            original_document_id uuid NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
            requested_by_user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            status prescription_ocr_job_status NOT NULL DEFAULT 'queued',
            extracted_fields jsonb,
            attempt_count integer NOT NULL DEFAULT 0,
            error_code text,
            resulting_prescription_id uuid UNIQUE REFERENCES prescriptions(id) ON DELETE RESTRICT,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_prescription_ocr_patient_document
                UNIQUE (patient_user_id, original_document_id),
            CONSTRAINT ck_prescription_ocr_attempts CHECK (attempt_count >= 0),
            CONSTRAINT ck_prescription_ocr_extracted_object CHECK (
                extracted_fields IS NULL OR jsonb_typeof(extracted_fields) = 'object'
            ),
            CONSTRAINT ck_prescription_ocr_draft_state CHECK (
                (resulting_prescription_id IS NULL) = (status <> 'draft_created')
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_prescription_ocr_patient_created "
        "ON prescription_ocr_jobs(patient_user_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX ix_prescription_ocr_status_created "
        "ON prescription_ocr_jobs(status, created_at DESC)"
    )
    op.execute(
        """
        CREATE TABLE prescription_templates (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            doctor_user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            name text NOT NULL,
            template_type prescription_template_type NOT NULL,
            status prescription_template_status NOT NULL DEFAULT 'active',
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            archived_at timestamptz,
            CONSTRAINT ck_prescription_template_archive CHECK (
                (status = 'archived') = (archived_at IS NOT NULL)
            ),
            CONSTRAINT ck_prescription_template_name CHECK (
                length(btrim(name)) BETWEEN 1 AND 120
            )
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_prescription_template_active_name "
        "ON prescription_templates(doctor_user_id, lower(name)) WHERE status = 'active'"
    )
    op.execute(
        "CREATE INDEX ix_prescription_templates_owner_status "
        "ON prescription_templates(doctor_user_id, status, created_at DESC)"
    )
    op.execute(
        """
        CREATE TABLE prescription_template_items (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            template_id uuid NOT NULL REFERENCES prescription_templates(id) ON DELETE RESTRICT,
            position smallint NOT NULL,
            medicine_id uuid REFERENCES medicines(id) ON DELETE RESTRICT,
            medicine_name_snapshot text NOT NULL,
            dose_text text NOT NULL,
            route text,
            frequency text NOT NULL,
            duration_days integer,
            instructions text,
            quantity numeric(12, 3),
            quantity_unit text,
            is_prn boolean NOT NULL DEFAULT false,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_prescription_template_item_position UNIQUE (template_id, position),
            CONSTRAINT ck_prescription_template_item_position CHECK (position > 0),
            CONSTRAINT ck_prescription_template_item_duration CHECK (
                duration_days IS NULL OR duration_days > 0
            ),
            CONSTRAINT ck_prescription_template_item_quantity CHECK (
                quantity IS NULL OR quantity > 0
            ),
            CONSTRAINT ck_prescription_template_item_quantity_unit CHECK (
                (quantity IS NULL) = (quantity_unit IS NULL)
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_prescription_template_items_template "
        "ON prescription_template_items(template_id, position)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE prescription_template_items")
    op.execute("DROP TABLE prescription_templates")
    op.execute("DROP TABLE prescription_ocr_jobs")
    op.execute("DROP TYPE prescription_template_status")
    op.execute("DROP TYPE prescription_template_type")
    op.execute("DROP TYPE prescription_ocr_job_status")
