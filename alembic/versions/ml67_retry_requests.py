"""ML-67 durable retry queue and retained retry reasons.

Revision ID: ml67_retry_requests
Revises: prescription_ocr_templates
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "ml67_retry_requests"
down_revision = "prescription_ocr_templates"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "job_retry_requests",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("job_runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "requested_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'queued'")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('queued','running','succeeded','failed','cancelled')",
            name="retry_request_status",
        ),
        sa.CheckConstraint("attempt_number > 0", name="retry_attempt_positive"),
    )
    op.create_index(
        "uq_job_retry_request_open",
        "job_retry_requests",
        ["job_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued','running')"),
    )


def downgrade():
    # Operational evidence must not be silently discarded by a downgrade.
    raise RuntimeError(
        "ML-67 retry history is retained; downgrade requires an approved archival migration."
    )
