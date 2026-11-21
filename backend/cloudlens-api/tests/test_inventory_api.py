"""Tests for inventory counts and filtered resource APIs."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import AwsAccount, Resource
from app.db.session import get_db
from app.main import create_app


@pytest.fixture
def client() -> TestClient:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = factory()

    account = AwsAccount(
        id=str(uuid4()),
        aws_account_id="111122223333",
        project_alias="demo",
        account_alias="acct",
    )
    session.add(account)
    session.flush()
    now = datetime.now(UTC)
    session.add_all(
        [
            Resource(
                id=str(uuid4()),
                account_pk=account.id,
                aws_account_id="111122223333",
                region="test-region-a",
                service="ec2",
                resource_type="instance",
                resource_id="i-1",
                identity_key="i-1",
                tags={"Name": "web"},
                observed_state={"status": "running", "instance_type": "t3.micro"},
                discovered_at=now,
                last_seen_at=now,
                collector_version="1.0.0",
                source="test",
                is_stale=False,
            ),
            Resource(
                id=str(uuid4()),
                account_pk=account.id,
                aws_account_id="111122223333",
                region="test-region-a",
                service="ec2",
                resource_type="volume",
                resource_id="vol-1",
                identity_key="vol-1",
                tags={},
                observed_state={"status": "in-use"},
                discovered_at=now,
                last_seen_at=now,
                collector_version="1.0.0",
                source="test",
                is_stale=False,
            ),
            Resource(
                id=str(uuid4()),
                account_pk=account.id,
                aws_account_id="111122223333",
                region="test-region-b",
                service="elbv2",
                resource_type="load-balancer",
                resource_id="lb-1",
                identity_key="lb-1",
                tags={},
                observed_state={"status": "active", "scheme": "internal"},
                discovered_at=now,
                last_seen_at=now,
                collector_version="1.0.0",
                source="test",
                is_stale=True,
            ),
        ]
    )
    session.commit()

    app = create_app()

    def _override() -> Session:
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db] = _override
    test_client = TestClient(app)
    try:
        yield test_client
    finally:
        app.dependency_overrides.clear()
        session.close()
        engine.dispose()


def test_inventory_counts(client: TestClient) -> None:
    response = client.get("/inventory/counts")
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 3
    assert len(payload["buckets"]) == 3

    filtered = client.get(
        "/inventory/counts",
        params={"account_id": "111122223333", "resource_type": "instance"},
    )
    assert filtered.json()["total"] == 1


def test_inventory_resources_filtered(client: TestClient) -> None:
    response = client.get(
        "/inventory/resources",
        params={
            "service": "ec2",
            "region": "test-region-a",
            "is_stale": False,
            "limit": 10,
            "offset": 0,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert len(payload["items"]) == 2
    assert all(item["service"] == "ec2" for item in payload["items"])
    assert all("discovered_at" in item and "last_seen_at" in item for item in payload["items"])
    assert any(item["observed_state"].get("status") == "running" for item in payload["items"])

    stale = client.get("/inventory/resources", params={"is_stale": True})
    assert stale.json()["total"] == 1
    assert stale.json()["items"][0]["resource_type"] == "load-balancer"
