"""Analytics engine — computes summaries over collector results.

No AWS API calls; operates on already-collected inventory payloads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.collectors.base import CollectionResult


@dataclass(slots=True)
class AnalyticsSummary:
    """High-level inventory analytics snapshot."""

    total_resources: int = 0
    by_resource_type: dict[str, int] = field(default_factory=dict)
    collectors_run: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_resources": self.total_resources,
            "by_resource_type": self.by_resource_type,
            "collectors_run": self.collectors_run,
            "notes": self.notes,
        }


class AnalyticsEngine:
    """Aggregates collection results into actionable summaries."""

    def summarize(self, results: list[CollectionResult]) -> AnalyticsSummary:
        by_type: dict[str, int] = {}
        total = 0
        collectors: list[str] = []

        for result in results:
            collectors.append(result.collector)
            by_type[result.resource_type] = (
                by_type.get(result.resource_type, 0) + result.count
            )
            total += result.count

        notes: list[str] = []
        if not results:
            notes.append("No collector results available yet.")

        return AnalyticsSummary(
            total_resources=total,
            by_resource_type=by_type,
            collectors_run=collectors,
            notes=notes,
        )
