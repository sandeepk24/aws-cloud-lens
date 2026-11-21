"""Relationships between normalized inventory resources."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.resource import Resource


class ResourceRelationship(Base):
    """Directed edge between two resources (e.g. instance → security-group)."""

    __tablename__ = "resource_relationships"
    __table_args__ = (
        UniqueConstraint(
            "from_resource_id",
            "to_resource_id",
            "relationship_type",
            name="uq_resource_relationships_edge",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    from_resource_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("resources.id", ondelete="CASCADE"), nullable=False
    )
    to_resource_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("resources.id", ondelete="CASCADE"), nullable=False
    )
    relationship_type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)

    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    is_stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stale_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    collector_version: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    from_resource: Mapped[Resource] = relationship(
        back_populates="outbound_relationships",
        foreign_keys=[from_resource_id],
    )
    to_resource: Mapped[Resource] = relationship(
        back_populates="inbound_relationships",
        foreign_keys=[to_resource_id],
    )
