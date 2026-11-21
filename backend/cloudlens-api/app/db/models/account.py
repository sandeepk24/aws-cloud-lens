"""Persisted AWS account records (aliases from external config)."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import DateTime, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.error import CollectionError
    from app.db.models.resource import Resource
    from app.db.models.run import InventoryRun


class AwsAccount(Base):
    """An AWS account known to CloudLens."""

    __tablename__ = "aws_accounts"
    __table_args__ = (
        UniqueConstraint("aws_account_id", name="uq_aws_accounts_aws_account_id"),
        UniqueConstraint(
            "project_alias",
            "account_alias",
            name="uq_aws_accounts_project_account_alias",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    aws_account_id: Mapped[str] = mapped_column(String(12), nullable=False, index=True)
    project_alias: Mapped[str] = mapped_column(String(128), nullable=False)
    account_alias: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    inventory_runs: Mapped[list[InventoryRun]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    resources: Mapped[list[Resource]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    collection_errors: Mapped[list[CollectionError]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
