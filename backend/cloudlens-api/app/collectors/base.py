"""Base collector abstractions."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.collectors.normalized import ResourcePage


@dataclass(slots=True)
class CollectionResult:
    """Normalized output of a non-paginated collector run."""

    collector: str
    resource_type: str
    items: list[dict[str, Any]] = field(default_factory=list)
    collected_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def count(self) -> int:
        return len(self.items)


class BaseCollector(ABC):
    """Abstract resource collector (legacy single-shot interface)."""

    name: str
    resource_type: str
    service: str
    version: str = "0.1.0"

    @abstractmethod
    def collect(self) -> CollectionResult:
        """Gather resources and return a normalized result."""


class PaginatedCollector(ABC):
    """Collector that yields pages of ``NormalizedResource`` items.

    Implementations must not hardcode account IDs or regions — those are
    provided by orchestration from external configuration.
    """

    name: str
    service: str
    resource_type: str
    version: str = "0.1.0"
    source: str = "collector"

    @abstractmethod
    def fetch_page(
        self,
        *,
        account_id: str,
        region: str,
        next_token: str | None = None,
    ) -> ResourcePage:
        """Fetch one page for the given account/region scope."""


class CollectorRegistry:
    """In-memory registry of available collectors."""

    def __init__(self) -> None:
        self._collectors: dict[str, BaseCollector | PaginatedCollector] = {}

    def register(self, collector: BaseCollector | PaginatedCollector) -> None:
        self._collectors[collector.name] = collector

    def get(self, name: str) -> BaseCollector | PaginatedCollector | None:
        return self._collectors.get(name)

    def list(self) -> list[str]:
        return sorted(self._collectors.keys())

    def run_all(self) -> list[CollectionResult]:
        results: list[CollectionResult] = []
        for collector in self._collectors.values():
            if isinstance(collector, BaseCollector):
                results.append(collector.collect())
        return results
