"""Inventory query endpoints — counts and filtered resource listing."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.inventory_queries import InventoryFilters, list_resources, resource_counts_by_type
from app.db.session import get_db
from app.models.schemas import (
    InventoryCountsResponse,
    InventoryListResponse,
    InventoryResource,
    ResourceCountBucket,
)

router = APIRouter(prefix="/inventory", tags=["inventory"])


def _filters(
    aws_account_id: str | None = Query(default=None, alias="account_id"),
    region: str | None = None,
    service: str | None = None,
    resource_type: str | None = None,
    is_stale: bool | None = None,
    identity_key: str | None = None,
    resource_id: str | None = None,
) -> InventoryFilters:
    return InventoryFilters(
        aws_account_id=aws_account_id,
        region=region,
        service=service,
        resource_type=resource_type,
        is_stale=is_stale,
        identity_key=identity_key,
        resource_id=resource_id,
    )


@router.get("/counts", response_model=InventoryCountsResponse)
def get_inventory_counts(
    filters: InventoryFilters = Depends(_filters),
    db: Session = Depends(get_db),
) -> InventoryCountsResponse:
    """Return total and grouped resource counts for the given filters.

    ``total`` is the deterministic sum of the bucket counts from a single
    grouped query, so the two can never disagree (previously a second COUNT
    could observe a concurrent write and diverge).
    """
    buckets = [
        ResourceCountBucket(**row) for row in resource_counts_by_type(db, filters)
    ]
    oldest = [b.oldest_last_seen_at for b in buckets if b.oldest_last_seen_at]
    newest = [b.newest_last_seen_at for b in buckets if b.newest_last_seen_at]
    return InventoryCountsResponse(
        total=sum(b.count for b in buckets),
        buckets=buckets,
        oldest_last_seen_at=min(oldest) if oldest else None,
        newest_last_seen_at=max(newest) if newest else None,
    )


@router.get("/resources", response_model=InventoryListResponse)
def get_inventory_resources(
    filters: InventoryFilters = Depends(_filters),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> InventoryListResponse:
    """Return a filtered, paginated inventory resource list."""
    try:
        items, total = list_resources(db, filters, limit=limit, offset=offset)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return InventoryListResponse(
        total=total,
        limit=limit,
        offset=offset,
        items=[
            InventoryResource(
                id=item.id,
                aws_account_id=item.aws_account_id,
                region=item.region,
                service=item.service,
                resource_type=item.resource_type,
                resource_id=item.resource_id,
                arn=item.arn,
                identity_key=item.identity_key,
                tags=item.tags or {},
                observed_state=item.observed_state or {},
                discovered_at=item.discovered_at,
                last_seen_at=item.last_seen_at,
                collector_version=item.collector_version,
                source=item.source,
                is_stale=item.is_stale,
                stale_at=item.stale_at,
            )
            for item in items
        ],
    )
