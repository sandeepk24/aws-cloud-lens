"""Pydantic v2 API schemas."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """Liveness / readiness payload for /health."""

    status: Literal["ok", "degraded", "error"] = "ok"
    service: str = Field(description="Service identifier")
    version: str = Field(description="API package version")
    environment: str = Field(description="Deployment environment name")
    timestamp: datetime = Field(description="UTC timestamp of the check")


class ResourceCountBucket(BaseModel):
    aws_account_id: str
    region: str
    service: str
    resource_type: str
    is_stale: bool
    count: int
    oldest_last_seen_at: datetime | None = Field(
        default=None,
        description="Earliest last_seen_at in this bucket (data freshness lower bound).",
    )
    newest_last_seen_at: datetime | None = Field(
        default=None,
        description="Latest last_seen_at in this bucket.",
    )


class InventoryCountsResponse(BaseModel):
    total: int
    buckets: list[ResourceCountBucket]
    oldest_last_seen_at: datetime | None = Field(
        default=None,
        description="Oldest observation across all returned buckets.",
    )
    newest_last_seen_at: datetime | None = Field(
        default=None,
        description="Newest observation across all returned buckets.",
    )


class RouteLatency(BaseModel):
    method: str
    route: str
    requests: int
    server_errors: int
    window: int = Field(description="Samples in the rolling window used for percentiles")
    p50_ms: float
    p95_ms: float
    max_ms: float


class MetricsResponse(BaseModel):
    scope: Literal["process"] = "process"
    routes: list[RouteLatency]


class InventoryResource(BaseModel):
    id: str
    aws_account_id: str
    region: str
    service: str
    resource_type: str
    resource_id: str
    arn: str | None = None
    identity_key: str
    tags: dict[str, Any] = Field(default_factory=dict)
    observed_state: dict[str, Any] = Field(default_factory=dict)
    discovered_at: datetime
    last_seen_at: datetime
    collector_version: str
    source: str
    is_stale: bool
    stale_at: datetime | None = None


class InventoryListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[InventoryResource]
