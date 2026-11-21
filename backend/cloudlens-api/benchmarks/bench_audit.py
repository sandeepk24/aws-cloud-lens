"""Reproducible local benchmarks for the security/performance audit.

No network access and no AWS credentials are used: AWS clients are either
in-process stubs or real botocore clients constructed with dummy credentials
(construction only — no requests are sent).

Usage (from backend/cloudlens-api):
    .venv/bin/python benchmarks/bench_audit.py --label after \
        --out ../../docs/audits/benchmarks/after.json

Every scenario only uses interfaces that existed before the audit, so the
same script produces comparable "before" and "after" numbers.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _pct(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1))))
    return ordered[k]


def _summary(values_ms: list[float]) -> dict[str, float]:
    return {
        "n": len(values_ms),
        "p50_ms": round(_pct(values_ms, 50), 3),
        "p95_ms": round(_pct(values_ms, 95), 3),
        "mean_ms": round(statistics.fmean(values_ms), 3),
    }


def _engine(path: Path):  # type: ignore[no-untyped-def]
    from sqlalchemy import create_engine

    from app.db.base import Base

    engine = create_engine(
        f"sqlite+pysqlite:///{path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    return engine


def _count_statements(engine: Any) -> Counter[str]:
    from sqlalchemy import event

    counter: Counter[str] = Counter()

    @event.listens_for(engine, "before_cursor_execute")
    def _on_exec(conn, cursor, statement, params, context, executemany):  # type: ignore[no-untyped-def]
        counter[statement.split(None, 1)[0].upper()] += 1

    return counter


# --------------------------------------------------------------------------- #
# Scenario 1: orchestration with relationships (N+1 resolve_resource_pk)
# --------------------------------------------------------------------------- #
def bench_orchestration_relationships(tmp: Path) -> dict[str, Any]:
    from sqlalchemy.orm import sessionmaker

    from app.collectors.base import PaginatedCollector
    from app.collectors.normalized import NormalizedResource, ResourceEdge, ResourcePage
    from app.collectors.orchestration import (
        CollectionOrchestrator,
        CollectionScope,
        OrchestratorConfig,
    )

    acct, region = "111122223333", "us-east-1"
    n_sg, n_inst, edges_per, page = 200, 2000, 3, 100

    class SgCollector(PaginatedCollector):
        name, service, resource_type, version, source = (
            "b.sg",
            "ec2",
            "security-group",
            "1",
            "bench",
        )

        def fetch_page(
            self, *, account_id: str, region: str, next_token: str | None = None
        ) -> ResourcePage:
            return ResourcePage(
                items=[
                    NormalizedResource(
                        account_id=account_id,
                        region=region,
                        service="ec2",
                        resource_type="security-group",
                        resource_id=f"sg-{i}",
                    )
                    for i in range(n_sg)
                ]
            )

    class InstCollector(PaginatedCollector):
        name, service, resource_type, version, source = "b.inst", "ec2", "instance", "1", "bench"

        def fetch_page(
            self, *, account_id: str, region: str, next_token: str | None = None
        ) -> ResourcePage:
            start = int(next_token or 0)
            items = [
                NormalizedResource(
                    account_id=account_id,
                    region=region,
                    service="ec2",
                    resource_type="instance",
                    resource_id=f"i-{i}",
                    relationships=[
                        ResourceEdge(
                            "uses-security-group",
                            account_id,
                            region,
                            "ec2",
                            "security-group",
                            f"sg-{(i + k) % n_sg}",
                        )
                        for k in range(edges_per)
                    ],
                )
                for i in range(start, min(start + page, n_inst))
            ]
            nxt = start + page
            return ResourcePage(items=items, next_token=str(nxt) if nxt < n_inst else None)

    engine = _engine(tmp / "rel.db")
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    orch = CollectionOrchestrator(factory, config=OrchestratorConfig(max_workers=1))
    scope = [CollectionScope(acct, "p", "a", region)]
    orch.collect(SgCollector(), scope)

    counter = _count_statements(engine)
    t0 = time.perf_counter()
    result = orch.collect(InstCollector(), scope)
    elapsed = (time.perf_counter() - t0) * 1000
    from sqlalchemy import func, select

    from app.db.models import ResourceRelationship

    with factory() as s:
        edges = s.scalar(select(func.count()).select_from(ResourceRelationship))
    engine.dispose()
    return {
        "instances": n_inst,
        "edges_per_instance": edges_per,
        "status": result.scopes[0].status.value,
        "relationships_persisted": edges,
        "elapsed_ms": round(elapsed, 1),
        "sql_statements_total": sum(counter.values()),
        "sql_select_statements": counter["SELECT"],
    }


# --------------------------------------------------------------------------- #
# Scenario 2: stale marking with a large seen set (NOT IN parameter list)
# --------------------------------------------------------------------------- #
def bench_mark_stale_large_scope(tmp: Path) -> dict[str, Any]:
    from sqlalchemy.orm import sessionmaker

    from app.collectors.base import PaginatedCollector
    from app.collectors.normalized import NormalizedResource, ResourcePage
    from app.collectors.orchestration import (
        CollectionOrchestrator,
        CollectionScope,
        OrchestratorConfig,
    )

    total, page = 40_000, 1000

    class Big(PaginatedCollector):
        name, service, resource_type, version, source = "b.logs", "logs", "log-group", "1", "bench"

        def fetch_page(
            self, *, account_id: str, region: str, next_token: str | None = None
        ) -> ResourcePage:
            start = int(next_token or 0)
            items = [
                NormalizedResource(
                    account_id=account_id,
                    region=region,
                    service="logs",
                    resource_type="log-group",
                    resource_id=f"/aws/lg/{i}",
                )
                for i in range(start, min(start + page, total))
            ]
            nxt = start + page
            return ResourcePage(items=items, next_token=str(nxt) if nxt < total else None)

    engine = _engine(tmp / "stale.db")
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    orch = CollectionOrchestrator(factory, config=OrchestratorConfig(max_workers=1))
    scope = [CollectionScope("111122223333", "p", "a", "us-east-1")]
    orch.collect(Big(), scope)  # seed

    # Measure only the stale-marking UPDATE on the second (steady-state) run.
    stale_ms: list[float] = []
    from sqlalchemy import event

    @event.listens_for(engine, "before_cursor_execute")
    def _b(conn, cursor, statement, params, context, executemany):  # type: ignore[no-untyped-def]
        conn.info["t0"] = time.perf_counter()

    @event.listens_for(engine, "after_cursor_execute")
    def _a(conn, cursor, statement, params, context, executemany):  # type: ignore[no-untyped-def]
        if statement.lstrip().upper().startswith("UPDATE RESOURCES SET IS_STALE"):
            stale_ms.append((time.perf_counter() - conn.info["t0"]) * 1000)
            n_params = len(params) if isinstance(params, (list, tuple, dict)) else -1
            conn.info["stale_params"] = n_params

    t0 = time.perf_counter()
    result = orch.collect(Big(), scope)
    elapsed = (time.perf_counter() - t0) * 1000
    params = None
    with engine.connect() as c:
        params = c.info.get("stale_params")
    engine.dispose()
    return {
        "resources_in_scope": total,
        "status": result.scopes[0].status.value,
        "stale_marked": result.scopes[0].resources_stale,
        "stale_update_ms": round(sum(stale_ms), 2),
        "stale_update_bind_params": params,
        "full_run_ms": round(elapsed, 1),
        "note": "PostgreSQL rejects >65535 bind params; not testable locally (no Postgres).",
    }


# --------------------------------------------------------------------------- #
# Scenario 3: boto3 client construction per fetch_page
# --------------------------------------------------------------------------- #
def bench_client_construction() -> dict[str, Any]:
    import boto3

    from app.accounts.models import ProfileAuth
    from app.connectors.aws.client import AWSConnector
    from app.connectors.aws.session import AWSSessionManager

    connector = AWSConnector(
        project_alias="p",
        account_alias="a",
        account_id="111122223333",
        regions=("us-east-1", "us-west-2"),
        enabled_collectors=(),
        auth=ProfileAuth(profile="bench"),
    )

    def factory(**kwargs: Any) -> Any:
        kwargs.pop("profile_name", None)
        return boto3.Session(
            aws_access_key_id="AKIDBENCH",
            aws_secret_access_key="bench",
            aws_session_token="bench",
            **kwargs,
        )

    mgr = AWSSessionManager(connector, sts_service_name="sts", session_factory=factory)
    mgr.client("ec2", "us-east-1")  # warm loader caches
    samples: list[float] = []
    for i in range(100):
        t0 = time.perf_counter()
        mgr.client("ec2", "us-east-1" if i % 2 else "us-west-2")
        samples.append((time.perf_counter() - t0) * 1000)
    return {"calls": 100, **_summary(samples), "total_ms": round(sum(samples), 1)}


# --------------------------------------------------------------------------- #
# Scenario 4: S3 API call counts across regions
# --------------------------------------------------------------------------- #
def bench_s3_api_calls() -> dict[str, Any]:
    from app.collectors.s3.collectors import S3BucketCollector

    regions = ["us-east-1", "us-west-2", "eu-west-1"]
    buckets = [{"Name": f"b{i}", "BucketRegion": regions[i % 3]} for i in range(60)]
    calls: Counter[str] = Counter()

    class Stub:
        def __getattr__(self, op: str) -> Any:
            def _call(**kw: Any) -> Any:
                calls[op] += 1
                if op == "list_buckets":
                    return {"Buckets": buckets}
                if op == "get_bucket_location":
                    region = next(b["BucketRegion"] for b in buckets if b["Name"] == kw["Bucket"])
                    return {"LocationConstraint": None if region == "us-east-1" else region}
                return {}

            return _call

    collector = S3BucketCollector(
        None,  # type: ignore[arg-type]
        s3_service_name="s3",
        s3_default_region="us-east-1",
        client_factory=lambda _region: Stub(),
    )
    found = 0
    for region in regions:
        found += len(collector.fetch_page(account_id="111122223333", region=region).items)
    return {
        "regions": len(regions),
        "buckets": len(buckets),
        "buckets_collected": found,
        "api_calls": dict(calls),
        "api_calls_total": sum(calls.values()),
    }


# --------------------------------------------------------------------------- #
# Scenario 5: retry amplification under persistent throttling
# --------------------------------------------------------------------------- #
def bench_retry_amplification(tmp: Path) -> dict[str, Any]:
    from sqlalchemy.orm import sessionmaker

    from app.collectors.network.collectors import VpcCollector
    from app.collectors.orchestration import (
        CollectionOrchestrator,
        CollectionScope,
        OrchestratorConfig,
    )
    from app.connectors.aws.session import _default_botocore_config

    calls = {"n": 0}

    class Throttled:
        def describe_vpcs(self, **kw: Any) -> Any:
            calls["n"] += 1
            raise ClientError(
                {
                    "Error": {"Code": "Throttling", "Message": "Rate exceeded"},
                    "ResponseMetadata": {"HTTPStatusCode": 400},
                },
                "DescribeVpcs",
            )

    collector = VpcCollector(
        None,
        ec2_service_name="ec2",  # type: ignore[arg-type]
        client_factory=lambda _r: Throttled(),
        sleep=lambda _s: None,
    )
    engine = _engine(tmp / "retry.db")
    orch = CollectionOrchestrator(
        sessionmaker(bind=engine), config=OrchestratorConfig(max_workers=1), sleep=lambda _s: None
    )
    orch.collect(collector, [CollectionScope("111122223333", "p", "a", "us-east-1")])
    engine.dispose()
    botocore_attempts = _default_botocore_config().retries["max_attempts"]
    return {
        "app_level_attempts_per_page": calls["n"],
        "botocore_max_attempts_config": botocore_attempts,
        "worst_case_http_requests_per_page": calls["n"] * botocore_attempts,
    }


# --------------------------------------------------------------------------- #
# Scenario 6: inventory API latency
# --------------------------------------------------------------------------- #
def bench_api_latency(tmp: Path) -> dict[str, Any]:
    db = tmp / "api.db"
    os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///{db}"
    from app.core.config import get_settings
    from app.db.session import get_engine, get_session_factory

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    engine = _engine(db)
    from datetime import UTC, datetime
    from uuid import uuid4

    from app.db.models import AwsAccount, Resource

    now = datetime.now(UTC)
    rows = []
    with engine.begin() as conn:
        accounts = []
        for a in range(3):
            aid = str(uuid4())
            accounts.append((aid, f"11112222333{a}"))
            conn.execute(
                AwsAccount.__table__.insert(),
                {
                    "id": aid,
                    "aws_account_id": f"11112222333{a}",
                    "project_alias": "p",
                    "account_alias": f"a{a}",
                },
            )
        types = [("ec2", "instance"), ("ec2", "subnet"), ("logs", "log-group"), ("s3", "s3-bucket")]
        for i in range(20_000):
            apk, aid = accounts[i % 3]
            svc, rt = types[i % 4]
            rid = f"r-{i}"
            rows.append(
                {
                    "id": str(uuid4()),
                    "account_pk": apk,
                    "aws_account_id": aid,
                    "region": ["us-east-1", "us-west-2"][i % 2],
                    "service": svc,
                    "resource_type": rt,
                    "resource_id": rid,
                    "arn": None,
                    "identity_key": rid,
                    "tags": {"Name": rid},
                    "observed_state": {"resource_id": rid, "status": "ok"},
                    "discovered_at": now,
                    "last_seen_at": now,
                    "collector_version": "1",
                    "source": "bench",
                    "is_stale": i % 10 == 0,
                    "stale_at": None,
                    "last_inventory_run_id": None,
                }
            )
        conn.execute(Resource.__table__.insert(), rows)

    from fastapi.testclient import TestClient

    from app.main import create_app

    client = TestClient(create_app())
    out: dict[str, Any] = {"resources_seeded": len(rows)}
    for name, path in [
        ("counts_all", "/inventory/counts"),
        ("counts_filtered", "/inventory/counts?account_id=111122223331&service=ec2"),
        ("list_limit100", "/inventory/resources?limit=100"),
        ("list_filtered_offset", "/inventory/resources?service=logs&limit=100&offset=2000"),
    ]:
        for _ in range(10):
            client.get(path)
        samples = []
        for _ in range(200):
            t0 = time.perf_counter()
            r = client.get(path)
            samples.append((time.perf_counter() - t0) * 1000)
            assert r.status_code == 200, r.text
        out[name] = _summary(samples)
    engine.dispose()
    get_engine().dispose()
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    import sqlalchemy

    results: dict[str, Any] = {
        "label": args.label,
        "env": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "sqlalchemy": sqlalchemy.__version__,
            "db": "sqlite (file, local)",
        },
    }
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        for name, fn in [
            ("orchestration_relationships", lambda: bench_orchestration_relationships(tmp)),
            ("mark_stale_large_scope", lambda: bench_mark_stale_large_scope(tmp)),
            ("client_construction", bench_client_construction),
            ("s3_api_calls", bench_s3_api_calls),
            ("retry_amplification", lambda: bench_retry_amplification(tmp)),
            ("api_latency", lambda: bench_api_latency(tmp)),
        ]:
            t0 = time.perf_counter()
            try:
                results[name] = fn()
            except Exception as exc:  # noqa: BLE001 - report, don't hide
                results[name] = {"error": f"{type(exc).__name__}: {exc}"}
            print(
                f"{name}: {json.dumps(results[name])}  ({time.perf_counter() - t0:.1f}s)",
                flush=True,
            )
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(results, indent=2) + "\n")


if __name__ == "__main__":
    main()
