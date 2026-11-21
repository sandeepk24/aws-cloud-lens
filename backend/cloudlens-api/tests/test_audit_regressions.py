"""Regression tests for issues fixed in docs/audits/SECURITY_PERFORMANCE_AUDIT.md."""

from __future__ import annotations

import threading
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import boto3
import pytest
from botocore.stub import Stubber
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from app.accounts.models import ProfileAuth
from app.collectors.base import PaginatedCollector
from app.collectors.normalized import (
    CollectorError,
    NormalizedResource,
    ResourceEdge,
    ResourcePage,
)
from app.collectors.orchestration import (
    CollectionOrchestrator,
    CollectionScope,
    OrchestratorConfig,
)
from app.collectors.s3.collectors import S3BucketCollector
from app.connectors.aws.client import AWSConnector
from app.connectors.aws.errors import AWSReadOnlyViolationError
from app.connectors.aws.session import AWSSessionManager, _default_botocore_config
from app.core.metrics import LatencyRecorder, get_latency_recorder
from app.db.base import Base
from app.db.models import InventoryRun, InventoryRunStatus, Resource, ResourceRelationship

ACCOUNT = "111122223333"
REGION = "test-region-a"


@pytest.fixture
def factory(tmp_path) -> sessionmaker[Session]:
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'audit.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, autoflush=False, autocommit=False)
    engine.dispose()


def _scope(account: str = ACCOUNT) -> list[CollectionScope]:
    return [CollectionScope(account, "demo", "acct", REGION)]


def _res(
    rid: str, rtype: str = "instance", account: str = ACCOUNT, **kw: Any
) -> NormalizedResource:
    return NormalizedResource(
        account_id=account,
        region=REGION,
        service="ec2",
        resource_type=rtype,
        resource_id=rid,
        **kw,
    )


class _Pages(PaginatedCollector):
    """Returns pre-baked pages; an Exception entry is raised for that page."""

    name, service, resource_type, version, source = "t.inst", "ec2", "instance", "1", "test"

    def __init__(self, pages: list[ResourcePage | Exception], rtype: str = "instance") -> None:
        self._pages = pages
        self.resource_type = rtype

    def fetch_page(
        self, *, account_id: str, region: str, next_token: str | None = None
    ) -> ResourcePage:
        page = self._pages[int(next_token or 0)]
        if isinstance(page, Exception):
            raise page
        return page


def _orch(factory: sessionmaker[Session]) -> CollectionOrchestrator:
    return CollectionOrchestrator(
        factory, config=OrchestratorConfig(max_workers=1, max_retries=1), sleep=lambda _s: None
    )


# --------------------------------------------------------------------------- #
# Accuracy: partial scans must not mark unseen resources stale
# --------------------------------------------------------------------------- #
def test_partial_run_does_not_mark_unfetched_resources_stale(factory) -> None:
    orch = _orch(factory)
    orch.collect(_Pages([ResourcePage(items=[_res("i-a"), _res("i-b"), _res("i-c")])]), _scope())

    # Page 0 sees only i-a, page 1 fails: i-b / i-c were never checked.
    partial = _Pages(
        [
            ResourcePage(items=[_res("i-a")], next_token="1"),
            CollectorError(message="AccessDenied", error_code="AccessDenied", retryable=False),
        ]
    )
    result = orch.collect(partial, _scope())

    assert result.scopes[0].status == InventoryRunStatus.PARTIAL
    assert result.scopes[0].resources_stale == 0
    with factory() as s:
        stale = {r.resource_id: r.is_stale for r in s.scalars(select(Resource))}
        run = s.get(InventoryRun, result.scopes[0].run_id)
        assert run is not None
        assert run.stats["stale_marking"] == "skipped"
    assert stale == {"i-a": False, "i-b": False, "i-c": False}


def test_successful_run_marks_unseen_stale_without_per_key_params(factory) -> None:
    orch = _orch(factory)
    orch.collect(_Pages([ResourcePage(items=[_res(f"i-{n}") for n in range(50)])]), _scope())

    engine = factory.kw["bind"]
    stale_params: list[int] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _capture(conn, cursor, statement, params, context, executemany):  # type: ignore[no-untyped-def]
        if statement.lstrip().upper().startswith("UPDATE RESOURCES SET IS_STALE"):
            stale_params.append(len(params))

    result = orch.collect(_Pages([ResourcePage(items=[_res("i-0")])]), _scope())
    event.remove(engine, "before_cursor_execute", _capture)

    assert result.scopes[0].status == InventoryRunStatus.SUCCEEDED
    assert result.scopes[0].resources_stale == 49
    # Parameter count no longer scales with the number of seen resources.
    assert stale_params and max(stale_params) < 20
    with factory() as s:
        run = s.get(InventoryRun, result.scopes[0].run_id)
        assert run is not None
        assert run.stats["stale_marking"] == "applied"
        assert run.stats["pages"] == 1
        assert {"duration_ms", "fetch_ms", "persist_ms"} <= set(run.stats)


# --------------------------------------------------------------------------- #
# Account isolation: foreign-account items are never persisted
# --------------------------------------------------------------------------- #
def test_cross_account_items_are_dropped(factory) -> None:
    mixed = ResourcePage(items=[_res("i-mine"), _res("i-foreign", account="999999999999")])
    _orch(factory).collect(_Pages([mixed]), _scope())
    with factory() as s:
        rows = {(r.aws_account_id, r.resource_id) for r in s.scalars(select(Resource))}
    assert rows == {(ACCOUNT, "i-mine")}


# --------------------------------------------------------------------------- #
# Performance: relationships resolved in batches, not N+1
# --------------------------------------------------------------------------- #
def test_relationship_upsert_is_batched(factory) -> None:
    orch = _orch(factory)
    orch.collect(
        _Pages(
            [ResourcePage(items=[_res(f"sg-{n}", "security-group") for n in range(20)])],
            rtype="security-group",
        ),
        _scope(),
    )
    instances = [
        _res(
            f"i-{n}",
            relationships=[
                ResourceEdge("uses-sg", ACCOUNT, REGION, "ec2", "security-group", f"sg-{n % 20}"),
                ResourceEdge(
                    "uses-sg", ACCOUNT, REGION, "ec2", "security-group", f"sg-{(n + 1) % 20}"
                ),
                # duplicate edge must not break the upsert (Postgres ON CONFLICT rule)
                ResourceEdge("uses-sg", ACCOUNT, REGION, "ec2", "security-group", f"sg-{n % 20}"),
                # target not in inventory: skipped, never fabricated
                ResourceEdge("uses-sg", ACCOUNT, REGION, "ec2", "security-group", "sg-missing"),
            ],
        )
        for n in range(300)
    ]
    engine = factory.kw["bind"]
    selects = Counter[str]()

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, params, context, executemany):  # type: ignore[no-untyped-def]
        selects[statement.split(None, 1)[0].upper()] += 1

    orch.collect(_Pages([ResourcePage(items=instances)]), _scope())
    event.remove(engine, "before_cursor_execute", _count)

    with factory() as s:
        edges = s.scalars(select(ResourceRelationship)).all()
    assert len(edges) == 600
    # Previously: 1 + edges SELECTs per instance (1500 here).
    assert selects["SELECT"] < 20


# --------------------------------------------------------------------------- #
# Session manager: client cache, thread safety, read-only guard, call counts
# --------------------------------------------------------------------------- #
def _connector() -> AWSConnector:
    return AWSConnector(
        project_alias="demo",
        account_alias="acct",
        account_id=ACCOUNT,
        regions=("us-east-1", "us-west-2"),
        enabled_collectors=(),
        auth=ProfileAuth(profile="test"),
    )


def _real_session_factory(**kwargs: Any) -> Any:
    kwargs.pop("profile_name", None)
    return boto3.Session(
        aws_access_key_id="AKIDTEST",
        aws_secret_access_key="test",
        aws_session_token="test",
        **kwargs,
    )


def test_client_is_cached_per_service_and_region() -> None:
    session = MagicMock()
    session.client.side_effect = lambda *a, **k: MagicMock()
    mgr = AWSSessionManager(
        _connector(), sts_service_name="sts", session_factory=MagicMock(return_value=session)
    )
    a = mgr.client("ec2", "us-east-1")
    assert mgr.client("ec2", "us-east-1") is a
    assert mgr.client("ec2", "us-west-2") is not a
    assert session.client.call_count == 2


def test_client_cache_cleared_on_refresh() -> None:
    sessions = [MagicMock(), MagicMock()]
    for s in sessions:
        s.client.side_effect = lambda *a, **k: MagicMock()
    mgr = AWSSessionManager(
        _connector(), sts_service_name="sts", session_factory=MagicMock(side_effect=sessions)
    )
    first = mgr.client("ec2", "us-east-1")
    mgr.get_session(force_refresh=True)
    assert mgr.client("ec2", "us-east-1") is not first


def test_concurrent_client_creation_builds_one_client() -> None:
    session = MagicMock()
    created = []

    def _make(*a: Any, **k: Any) -> Any:
        created.append(1)
        return MagicMock()

    session.client.side_effect = _make
    mgr = AWSSessionManager(
        _connector(), sts_service_name="sts", session_factory=MagicMock(return_value=session)
    )
    barrier = threading.Barrier(8)

    def _worker() -> None:
        barrier.wait()
        mgr.client("ec2", "us-east-1")

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(created) == 1


def test_mutating_aws_operation_is_blocked_before_request() -> None:
    mgr = AWSSessionManager(
        _connector(), sts_service_name="sts", session_factory=_real_session_factory
    )
    ec2 = mgr.client("ec2", "us-east-1")
    with pytest.raises(AWSReadOnlyViolationError, match="CreateVpc"):
        ec2.create_vpc(CidrBlock="10.0.0.0/16")
    with pytest.raises(AWSReadOnlyViolationError, match="TerminateInstances"):
        ec2.terminate_instances(InstanceIds=["i-1"])


def test_read_operations_are_allowed_and_counted() -> None:
    mgr = AWSSessionManager(
        _connector(), sts_service_name="sts", session_factory=_real_session_factory
    )
    ec2 = mgr.client("ec2", "us-east-1")
    with Stubber(ec2) as stub:
        stub.add_response("describe_vpcs", {"Vpcs": []})
        stub.add_response("describe_vpcs", {"Vpcs": []})
        ec2.describe_vpcs()
        ec2.describe_vpcs()
    assert mgr.api_call_counts() == {"ec2:DescribeVpcs": 2}
    assert mgr.describe()["api_calls"] == {"ec2:DescribeVpcs": 2}


def test_botocore_retry_budget_matches_sdk_default() -> None:
    assert _default_botocore_config().retries["max_attempts"] == 3


# --------------------------------------------------------------------------- #
# S3: no redundant GetBucketLocation when ListBuckets returns BucketRegion
# --------------------------------------------------------------------------- #
class _S3Stub:
    def __init__(self, buckets: list[dict[str, Any]]) -> None:
        self.buckets = buckets
        self.calls: Counter[str] = Counter()

    def __getattr__(self, op: str) -> Any:
        def _call(**kw: Any) -> Any:
            self.calls[op] += 1
            if op == "list_buckets":
                return {"Buckets": self.buckets}
            if op == "get_bucket_location":
                return {"LocationConstraint": "us-west-2"}
            return {}

        return _call


def _s3(stub: _S3Stub) -> S3BucketCollector:
    return S3BucketCollector(
        None,
        s3_service_name="s3",
        s3_default_region="us-east-1",  # type: ignore[arg-type]
        client_factory=lambda _r: stub,
    )


def test_s3_uses_listed_bucket_region() -> None:
    stub = _S3Stub(
        [{"Name": "a", "BucketRegion": "us-east-1"}, {"Name": "b", "BucketRegion": "us-west-2"}]
    )
    page = _s3(stub).fetch_page(account_id=ACCOUNT, region="us-east-1")
    assert [r.resource_id for r in page.items] == ["a"]
    assert stub.calls["get_bucket_location"] == 0


def test_s3_falls_back_to_get_bucket_location() -> None:
    stub = _S3Stub([{"Name": "legacy"}])
    page = _s3(stub).fetch_page(account_id=ACCOUNT, region="us-west-2")
    assert [r.region for r in page.items] == ["us-west-2"]
    assert stub.calls["get_bucket_location"] == 1


# --------------------------------------------------------------------------- #
# API: CORS, counts consistency/freshness, latency metrics
# --------------------------------------------------------------------------- #
def test_cors_does_not_allow_credentials_or_mutating_methods(client: TestClient) -> None:
    origin = "http://localhost:5173"
    ok = client.options(
        "/inventory/counts", headers={"Origin": origin, "Access-Control-Request-Method": "GET"}
    )
    assert ok.headers.get("access-control-allow-origin") == origin
    assert "access-control-allow-credentials" not in ok.headers
    bad = client.options(
        "/inventory/counts", headers={"Origin": origin, "Access-Control-Request-Method": "DELETE"}
    )
    assert bad.status_code == 400
    evil = client.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in evil.headers


def test_counts_total_matches_buckets_and_reports_freshness(tmp_path, monkeypatch) -> None:
    from app.core.config import get_settings
    from app.db.session import get_engine, get_session_factory
    from app.main import create_app

    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'api.db'}")
    for fn in (get_settings, get_engine, get_session_factory):
        fn.cache_clear()
    try:
        Base.metadata.create_all(get_engine())
        factory = get_session_factory()
        orch = _orch(factory)
        orch.collect(_Pages([ResourcePage(items=[_res("i-1"), _res("i-2")])]), _scope())
        orch.collect(_Pages([ResourcePage(items=[_res("i-1")])]), _scope())  # i-2 -> stale

        body = TestClient(create_app()).get("/inventory/counts").json()
        assert body["total"] == sum(b["count"] for b in body["buckets"]) == 2
        assert {b["is_stale"] for b in body["buckets"]} == {True, False}
        assert all(b["newest_last_seen_at"] for b in body["buckets"])
        assert body["oldest_last_seen_at"] <= body["newest_last_seen_at"]
    finally:
        get_engine().dispose()
        for fn in (get_settings, get_engine, get_session_factory):
            fn.cache_clear()


def test_metrics_endpoint_reports_route_templates(client: TestClient) -> None:
    get_latency_recorder().reset()
    for _ in range(3):
        client.get("/health")
    client.get("/does-not-exist-123")
    routes = {(r["method"], r["route"]): r for r in client.get("/metrics").json()["routes"]}
    assert routes[("GET", "/health")]["requests"] == 3
    assert routes[("GET", "/health")]["p95_ms"] >= routes[("GET", "/health")]["p50_ms"] >= 0
    assert ("GET", "/does-not-exist-123") not in routes
    assert ("GET", "<unmatched>") in routes


def test_latency_recorder_is_bounded_and_counts_5xx() -> None:
    rec = LatencyRecorder(window=10)
    for i in range(100):
        rec.record("GET", "/x", float(i), 500 if i % 10 == 0 else 200)
    (row,) = rec.snapshot()
    assert row["requests"] == 100 and row["window"] == 10 and row["server_errors"] == 10
    assert row["p50_ms"] >= 90


def test_stale_inventory_is_visible_not_hidden(factory) -> None:
    """Old observations stay queryable with their timestamps (freshness is reported)."""
    orch = _orch(factory)
    orch.collect(_Pages([ResourcePage(items=[_res("i-1")])]), _scope())
    with factory() as s:
        row = s.scalar(select(Resource))
        assert row is not None
        assert row.last_seen_at is not None
        assert row.source == "test" and row.aws_account_id == ACCOUNT and row.region == REGION
        assert datetime.now(UTC) - row.last_seen_at.replace(tzinfo=UTC) < timedelta(minutes=5)
