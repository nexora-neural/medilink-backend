"""Bind staged report documents to the laboratory upload request."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "a104c116f001"
down_revision = "prescription_ocr_templates"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("documents", sa.Column("staged_report_request_id", UUID(), nullable=True))
    op.create_foreign_key(
        "fk_documents_staged_report_request", "documents", "lab_upload_requests",
        ["staged_report_request_id"], ["id"], ondelete="RESTRICT",
    )


def downgrade():
    op.drop_constraint("fk_documents_staged_report_request", "documents", type_="foreignkey")
    op.drop_column("documents", "staged_report_request_id")
