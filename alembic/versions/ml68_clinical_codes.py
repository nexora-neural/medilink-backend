"""Retain contract clinical codes on allergy and note records.

Revision ID: ml68_clinical_codes
Revises: prescription_ocr_templates
"""

import sqlalchemy as sa

from alembic import op

revision = "ml68_clinical_codes"
down_revision = "prescription_ocr_templates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("allergies", sa.Column("clinical_code", sa.Text(), nullable=True))
    op.add_column("clinical_notes", sa.Column("clinical_code", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("clinical_notes", "clinical_code")
    op.drop_column("allergies", "clinical_code")
