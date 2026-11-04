
"""SQLAlchemy ORM models for organizations and memberships."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base
from app.modules.identity.enums import (
    professional_role_type,
    verification_status_type,
)
from app.modules.organizations.enums import (
    entity_status_type,
    organization_type_type,
)


class Organization(Base):
    """Healthcare organization ORM model."""

    __tablename__ = "organizations"

    __table_args__ = (
        PrimaryKeyConstraint(
            "id",
            name="organizations_pkey",
        ),
        Index(
            "ix_organizations_type_status",
            "type",
            "status",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        server_default=text("gen_random_uuid()"),
    )

    name: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    type: Mapped[str] = mapped_column(
        organization_type_type,
        nullable=False,
    )

    registration_ref: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    status: Mapped[str] = mapped_column(
        entity_status_type,
        nullable=False,
        server_default=text("'pending'::entity_status"),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # ORM relationship: Organization -> Memberships
    memberships: Mapped[list["OrganizationMembership"]] = relationship(
        "OrganizationMembership",
        back_populates="organization",
    )


class OrganizationMembership(Base):
    """Membership linking a user to an organization."""

    __tablename__ = "organization_memberships"

    __table_args__ = (
        PrimaryKeyConstraint(
            "id",
            name="organization_memberships_pkey",
        ),
        UniqueConstraint(
            "organization_id",
            "user_id",
            "role",
            name="uq_membership_org_user_role",
        ),
        Index(
            "ix_memberships_user_status",
            "user_id",
            "status",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        server_default=text("gen_random_uuid()"),
    )

    organization_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "organizations.id",
            name="organization_memberships_organization_id_fkey",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )

    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "users.id",
            name="organization_memberships_user_id_fkey",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )

    role: Mapped[str] = mapped_column(
        professional_role_type,
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        verification_status_type,
        nullable=False,
        server_default=text("'pending'::verification_status"),
    )

    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    # ORM relationship: Membership -> Organization
    organization: Mapped["Organization"] = relationship(
        "Organization",
        back_populates="memberships",
    )

    # ORM relationship: Membership -> User
    user: Mapped["User"] = relationship(
        "User",
    )
