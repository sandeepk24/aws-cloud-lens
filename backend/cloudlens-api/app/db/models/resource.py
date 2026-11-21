"""Normalized AWS inventory resources."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.account import AwsAccount
    from app.db.models.relationship import ResourceRelationship
    from app.db.models.run import InventoryRun


class Resource(Base):
    """A discovered cloud resource, uniquely identified per account scope.

    Identity key is the ARN when present, otherwise the canonical resource ID.
    Composite natural key: (aws_account_id, region, service, resource_type, identity_key).
    """

    __tablename__ = "resources"
    __table_args__ = (
        UniqueConstraint(
            "aws_account_id",
            "region",
            "service",
            "resource_type",
            "identity_key",
            name="uq_resources_natural_key",
        ),
        Index("ix_resources_account_service_type", "aws_account_id", "service", "resource_type"),
        Index("ix_resources_last_seen_at", "last_seen_at"),
        Index("ix_resources_is_stale", "is_stale"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    account_pk: Mapped[str] = mapped_column(
        String(36), ForeignKey("aws_accounts.id", ondelete="CASCADE"), nullable=False
    )
    aws_account_id: Mapped[str] = mapped_column(String(12), nullable=False, index=True)
    region: Mapped[str] = mapped_column(String(64), nullable=False)
    service: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(128), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(512), nullable=False)
    arn: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    identity_key: Mapped[str] = mapped_column(String(2048), nullable=False)

    tags: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    observed_state: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    # Every relationship the collector read from the API for this resource,
    # resolved or not, with evidence. The graph builder resolves these at
    # read time, so collection order does not drop edges.
    declared_edges: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]"
    )

    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    collector_version: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)

    is_stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stale_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    last_inventory_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("inventory_runs.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    account: Mapped[AwsAccount] = relationship(back_populates="resources")
    last_inventory_run: Mapped[InventoryRun | None] = relationship(
        back_populates="resources"
    )
    outbound_relationships: Mapped[list[ResourceRelationship]] = relationship(
        back_populates="from_resource",
        foreign_keys="ResourceRelationship.from_resource_id",
        cascade="all, delete-orphan",
    )
    inbound_relationships: Mapped[list[ResourceRelationship]] = relationship(
        back_populates="to_resource",
        foreign_keys="ResourceRelationship.to_resource_id",
        cascade="all, delete-orphan",
    )
