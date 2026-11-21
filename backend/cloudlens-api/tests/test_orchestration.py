"""Tests for paginated collection orchestration."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from app.collectors.base import PaginatedCollector
from app.collectors.normalized import CollectorError, NormalizedResource, ResourcePage
from app.collectors.orchestration import (
    CollectionOrchestrator,
    CollectionScope,
    OrchestratorConfig,
)
from app.db.base import Base
from app.db.models import CollectionError as CollectionErrorModel
from app.db.models import InventoryRun, InventoryRunStatus, Resource


class _StubCollector(PaginatedCollector):
    name = "stub.instances"
    service = "ec2"
    resource_type = "instance"
    version = "9.9.9"
    source = "stub"

    def __init__(self, pages: dict[str, list[ResourcePage]]) -> None:
        self._pages = pages
        self.calls: list[tuple[str, str | None]] = []

    def fetch_page(
        self,
        *,
        account_id: str,
        region: str,
        next_token: str | None = None,
    ) -> ResourcePage:
        self.calls.append((region, next_token))
        queue = self._pages.get(region, [])
        if not queue:
            return ResourcePage(items=[])
        # Consume pages in order keyed by call index for the region.
        idx = len([c for c in self.calls if c[0] == region]) - 1
        if idx < len(queue):
            return queue[idx]
        return ResourcePage(items=[])


class _FlakyCollector(PaginatedCollector):
    name = "flaky.collector"
    service = "ec2"
    resource_type = "instance"
    version = "1.0.0"

    def __init__(self) -> None:
        self.attempts = 0

    def fetch_page(
        self,
        *,
        account_id: str,
        region: str,
        next_token: str | None = None,
    ) -> ResourcePage:
        self.attempts += 1
        if self.attempts < 3:
            raise CollectorError(message="throttled", error_code="Throttling", retryable=True)
        return ResourcePage(
            items=[
                NormalizedResource(
                    account_id=account_id,
                    region=region,
                    service=self.service,
                    resource_type=self.resource_type,
                    resource_id="i-retry",
                )
            ]
        )


@pytest.fixture
def session_factory(tmp_path) -> sessionmaker[Session]:
    # File-backed SQLite so concurrent workers get separate connections.
    db_path = tmp_path / "orchestration.db"
    engine = create_engine(
        f"sqlite+pysqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    yield factory
    engine.dispose()


def _scope(region: str) -> CollectionScope:
    return CollectionScope(
        aws_account_id="111122223333",
        project_alias="demo",
        account_alias="acct",
        region=region,
    )


def test_paginated_orchestration_upserts_and_marks_stale(
    session_factory: sessionmaker[Session],
) -> None:
    region = "test-region-a"
    page1 = ResourcePage(
        items=[
            NormalizedResource(
                account_id="111122223333",
                region=region,
                service="ec2",
                resource_type="instance",
                resource_id="i-1",
                tags={"env": "dev"},
                observed_state={"az": "a"},
                discovered_at=datetime.now(UTC),
            ),
            NormalizedResource(
                account_id="111122223333",
                region=region,
                service="ec2",
                resource_type="instance",
                resource_id="i-2",
            ),
        ],
        next_token="page-2",
    )
    page2 = ResourcePage(
        items=[
            NormalizedResource(
                account_id="111122223333",
                region=region,
                service="ec2",
                resource_type="instance",
                resource_id="i-3",
            )
        ],
        next_token=None,
    )
    collector = _StubCollector({region: [page1, page2]})
    orchestrator = CollectionOrchestrator(
        session_factory,
        config=OrchestratorConfig(max_workers=2, max_retries=2, mark_stale=True),
        sleep=lambda _s: None,
    )

    # Seed a resource that will become stale.
    seed_session = session_factory()
    from app.db.repository import create_inventory_run, get_or_create_account, upsert_resources

    account = get_or_create_account(
        seed_session,
        aws_account_id="111122223333",
        project_alias="demo",
        account_alias="acct",
    )
    run = create_inventory_run(
        seed_session,
        account=account,
        collector_name=collector.name,
        collector_version="0.0.1",
        source="seed",
        region=region,
        service=collector.service,
        resource_type=collector.resource_type,
    )
    upsert_resources(
        seed_session,
        account=account,
        run=run,
        resources=[
            NormalizedResource(
                account_id="111122223333",
                region=region,
                service="ec2",
                resource_type="instance",
                resource_id="i-old",
            )
        ],
        collector_version="0.0.1",
        source="seed",
    )
    seed_session.commit()
    seed_session.close()

    result = orchestrator.collect(collector, [_scope(region)])
    assert len(result.scopes) == 1
    assert result.scopes[0].status == InventoryRunStatus.SUCCEEDED
    assert result.scopes[0].resources_seen == 3
    assert result.scopes[0].resources_stale == 1

    session = session_factory()
    resources = list(session.scalars(select(Resource)))
    by_id = {r.resource_id: r for r in resources}
    assert set(by_id) >= {"i-1", "i-2", "i-3", "i-old"}
    assert by_id["i-old"].is_stale is True
    assert by_id["i-1"].is_stale is False
    assert by_id["i-1"].tags["env"] == "dev"
    assert by_id["i-1"].collector_version == "9.9.9"
    assert by_id["i-1"].source == "stub"
    runs = list(session.scalars(select(InventoryRun)))
    assert any(r.status == InventoryRunStatus.SUCCEEDED.value for r in runs)
    session.close()


def test_orchestration_retries_then_succeeds(
    session_factory: sessionmaker[Session],
) -> None:
    collector = _FlakyCollector()
    orchestrator = CollectionOrchestrator(
        session_factory,
        config=OrchestratorConfig(max_retries=5, max_workers=1),
        sleep=lambda _s: None,
    )
    result = orchestrator.collect(collector, [_scope("test-region-b")])
    assert result.scopes[0].status == InventoryRunStatus.SUCCEEDED
    assert collector.attempts == 3

    session = session_factory()
    assert session.scalar(select(Resource.resource_id)) == "i-retry"
    session.close()


def test_orchestration_records_collection_errors(
    session_factory: sessionmaker[Session],
) -> None:
    class _Broken(PaginatedCollector):
        name = "broken"
        service = "ec2"
        resource_type = "instance"
        version = "1.0.0"

        def fetch_page(self, *, account_id: str, region: str, next_token: str | None = None):
            raise CollectorError(message="boom", error_code="HardFail", retryable=False)

    orchestrator = CollectionOrchestrator(
        session_factory,
        config=OrchestratorConfig(max_retries=2),
        sleep=lambda _s: None,
    )
    result = orchestrator.collect(_Broken(), [_scope("test-region-c")])
    assert result.scopes[0].status == InventoryRunStatus.FAILED

    session = session_factory()
    err = session.scalar(select(CollectionErrorModel))
    assert err is not None
    assert err.message == "boom"
    assert err.error_code == "HardFail"
    session.close()


def test_bounded_concurrency_across_regions(
    session_factory: sessionmaker[Session],
) -> None:
    regions = ["r1", "r2", "r3"]
    pages = {
        region: [
            ResourcePage(
                items=[
                    NormalizedResource(
                        account_id="111122223333",
                        region=region,
                        service="ec2",
                        resource_type="instance",
                        resource_id=f"i-{region}",
                    )
                ]
            )
        ]
        for region in regions
    }
    collector = _StubCollector(pages)
    orchestrator = CollectionOrchestrator(
        session_factory,
        config=OrchestratorConfig(max_workers=2),
        sleep=lambda _s: None,
    )
    result = orchestrator.collect(collector, [_scope(r) for r in regions])
    assert len(result.scopes) == 3
    assert all(s.status == InventoryRunStatus.SUCCEEDED for s in result.scopes)

    session = session_factory()
    ids = set(session.scalars(select(Resource.resource_id)))
    assert ids == {"i-r1", "i-r2", "i-r3"}
    session.close()
