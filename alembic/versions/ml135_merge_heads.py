"""Merge concurrent database migration heads."""

revision = "ml135_merge_heads"
down_revision = ("a104c116f001", "ml67_retry_requests", "ml68_clinical_codes")
branch_labels = None
depends_on = None

def upgrade() -> None:
    pass

def downgrade() -> None:
    pass
