"""Health check endpoint."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends

from app import __version__
from app.api.deps import settings_dep
from app.core.config import Settings
from app.core.metrics import get_latency_recorder
from app.models.schemas import HealthResponse, MetricsResponse, RouteLatency

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
def health(settings: Settings = Depends(settings_dep)) -> HealthResponse:
    """Return service liveness information."""
    return HealthResponse(
        status="ok",
        service=settings.app_name,
        version=__version__,
        environment=settings.app_env,
        timestamp=datetime.now(UTC),
    )


@router.get("/metrics", response_model=MetricsResponse)
def metrics() -> MetricsResponse:
    """Per-route request counts, 5xx counts and p50/p95 latency (this process)."""
    return MetricsResponse(
        routes=[RouteLatency(**row) for row in get_latency_recorder().snapshot()]
    )
