"""Read-side queries for inventory counts and filtered resource listing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.db.models import Resource


@dataclass(slots=True)
class InventoryFilters:
    aws_account_id: str | None = None
    region: str | None = None
    service: str | None = None
    resource_type: str | None = None
    is_stale: bool | None = None
    identity_key: str | None = None
    resource_id: str | None = None


def _apply_filters(stmt: Select[Any], filters: InventoryFilters) -> Select[Any]:
    if filters.aws_account_id:
        stmt = stmt.where(Resource.aws_account_id == filters.aws_account_id)
    if filters.region:
        stmt = stmt.where(Resource.region == filters.region)
    if filters.service:
        stmt = stmt.where(Resource.service == filters.service)
    if filters.resource_type:
        stmt = stmt.where(Resource.resource_type == filters.resource_type)
    if filters.is_stale is not None:
        stmt = stmt.where(Resource.is_stale.is_(filters.is_stale))
    if filters.identity_key:
        stmt = stmt.where(Resource.identity_key == filters.identity_key)
    if filters.resource_id:
        stmt = stmt.where(Resource.resource_id == filters.resource_id)
    return stmt


def count_resources(session: Session, filters: InventoryFilters) -> int:
    stmt = select(func.count()).select_from(Resource)
    stmt = _apply_filters(stmt, filters)
    return int(session.scalar(stmt) or 0)


def resource_counts_by_type(
    session: Session, filters: InventoryFilters
) -> list[dict[str, Any]]:
    stmt = (
        select(
            Resource.aws_account_id,
            Resource.region,
            Resource.service,
            Resource.resource_type,
            Resource.is_stale,
            func.count().label("count"),
            func.min(Resource.last_seen_at).label("oldest_last_seen_at"),
            func.max(Resource.last_seen_at).label("newest_last_seen_at"),
        )
        .group_by(
            Resource.aws_account_id,
            Resource.region,
            Resource.service,
            Resource.resource_type,
            Resource.is_stale,
        )
        .order_by(
            Resource.aws_account_id,
            Resource.region,
            Resource.service,
            Resource.resource_type,
        )
    )
    stmt = _apply_filters(stmt, filters)
    rows = session.execute(stmt).all()
    return [
        {
            "aws_account_id": row.aws_account_id,
            "region": row.region,
            "service": row.service,
            "resource_type": row.resource_type,
            "is_stale": bool(row.is_stale),
            "count": int(row.count),
            "oldest_last_seen_at": row.oldest_last_seen_at,
            "newest_last_seen_at": row.newest_last_seen_at,
        }
        for row in rows
    ]


def list_resources(
    session: Session,
    filters: InventoryFilters,
    *,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[Resource], int]:
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")
    if offset < 0:
        raise ValueError("offset must be >= 0")

    total = count_resources(session, filters)
    stmt = select(Resource).order_by(
        Resource.aws_account_id,
        Resource.region,
        Resource.service,
        Resource.resource_type,
        Resource.identity_key,
    )
    stmt = _apply_filters(stmt, filters).limit(limit).offset(offset)
    items = list(session.scalars(stmt))
    return items, total
