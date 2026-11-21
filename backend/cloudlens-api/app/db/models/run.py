"""Inventory run records."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db.base import Base
from app.db.models.enums import InventoryRunStatus

if TYPE_CHECKING:
    from app.db.models.account import AwsAccount
    from app.db.models.error import CollectionError
    from app.db.models.resource import Resource


class InventoryRun(Base):
    """One collection pass for an account (optionally scoped by region/collector)."""

    __tablename__ = "inventory_runs"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    account_pk: Mapped[str] = mapped_column(
        String(36), ForeignKey("aws_accounts.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=InventoryRunStatus.PENDING.value, index=True
    )
    collector_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    collector_version: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="collector")
    region: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    service: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resource_type: Mapped[str | None] = mapped_column(String(128), nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    resources_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    resources_upserted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    resources_stale: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stats: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)

    account: Mapped[AwsAccount] = relationship(back_populates="inventory_runs")
    resources: Mapped[list[Resource]] = relationship(back_populates="last_inventory_run")
    collection_errors: Mapped[list[CollectionError]] = relationship(
        back_populates="inventory_run", cascade="all, delete-orphan"
    )
