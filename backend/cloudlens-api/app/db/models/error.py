"""Collection error records captured during inventory runs."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.account import AwsAccount
    from app.db.models.run import InventoryRun


class CollectionError(Base):
    """An error encountered while collecting inventory."""

    __tablename__ = "collection_errors"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    inventory_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("inventory_runs.id", ondelete="CASCADE"), nullable=False
    )
    account_pk: Mapped[str] = mapped_column(
        String(36), ForeignKey("aws_accounts.id", ondelete="CASCADE"), nullable=False
    )

    region: Mapped[str | None] = mapped_column(String(64), nullable=True)
    service: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resource_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    collector_name: Mapped[str] = mapped_column(String(128), nullable=False)

    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    inventory_run: Mapped[InventoryRun] = relationship(back_populates="collection_errors")
    account: Mapped[AwsAccount] = relationship(back_populates="collection_errors")
