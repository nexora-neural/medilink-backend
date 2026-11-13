"""Database models for users, role assignments and support decisions."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    PrimaryKeyConstraint,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.modules.identity.enums import account_status_type


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="users_pkey"),
        UniqueConstraint("firebase_uid", name="users_firebase_uid_key"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False,
        server_default=text("gen_random_uuid()"),
    )
    firebase_uid: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    phone: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        account_status_type, nullable=False, server_default=text("'pending'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        server_default=func.now(), onupdate=func.now(),
    )


# Matches the ER diagram: unique lower(email), allowing multiple NULL emails.
Index(
    "uq_users_email_ci", func.lower(User.email), unique=True,
    postgresql_where=User.email.is_not(None),
)


class Role(Base):
    __tablename__ = "roles"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="roles_pkey"),
        UniqueConstraint("code", name="roles_code_key"),
        UniqueConstraint("label", name="roles_label_key"),
        CheckConstraint(
            "code IN ('patient', 'doctor', 'pharmacist', "
            "'lab_staff', 'admin', 'super_admin')", name="code",
        ),
    )

    id: Mapped[int] = mapped_column(
        SmallInteger, Identity(always=True), nullable=False,
    )
    code: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)


class UserRole(Base):
    __tablename__ = "user_roles"
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "role_id", name="user_roles_pkey"),
    )

    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", name="user_roles_user_id_fkey", ondelete="RESTRICT"),
        nullable=False,
    )
    role_id: Mapped[int] = mapped_column(
        SmallInteger,
        ForeignKey("roles.id", name="user_roles_role_id_fkey", ondelete="RESTRICT"),
        nullable=False,
    )
    assigned_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", name="user_roles_assigned_by_fkey", ondelete="RESTRICT"),
        nullable=True,
    )
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AccountSupportDecisionReason(Base):
    """Private reason for an append-only account support audit event."""

    __tablename__ = "account_support_decision_reasons"

    audit_event_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "audit_events.id",
            name="account_support_decision_reasons_audit_event_id_fkey",
            ondelete="RESTRICT",
        ),
        primary_key=True,
    )
    reason: Mapped[str] = mapped_column(Text)
