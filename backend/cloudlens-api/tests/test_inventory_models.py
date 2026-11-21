"""Tests for inventory ORM models, upserts, and stale handling."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.collectors.normalized import NormalizedResource, ResourceEdge, build_identity_key
from app.db.base import Base
from app.db.models import Resource, ResourceRelationship
from app.db.repository import (
    create_inventory_run,
    get_or_create_account,
    mark_stale_resources,
    upsert_relationships,
    upsert_resources,
)


@pytest.fixture
def db_session() -> Session:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_build_identity_key_prefers_arn() -> None:
    assert (
        build_identity_key(arn="arn:aws:ec2:r:1:instance/i-1", resource_id="i-1")
        == "arn:aws:ec2:r:1:instance/i-1"
    )
    assert build_identity_key(arn=None, resource_id="i-1") == "i-1"


def test_upsert_resource_and_preserve_discovered_at(db_session: Session) -> None:
    account = get_or_create_account(
        db_session,
        aws_account_id="111122223333",
        project_alias="demo",
        account_alias="acct",
    )
    run = create_inventory_run(
        db_session,
        account=account,
        collector_name="test.collector",
        collector_version="1.0.0",
        source="test",
        region="test-region-a",
        service="ec2",
        resource_type="instance",
    )
    first_seen = datetime(2024, 1, 1, tzinfo=UTC)
    resource = NormalizedResource(
        account_id="111122223333",
        region="test-region-a",
        service="ec2",
        resource_type="instance",
        resource_id="i-abc",
        arn="arn:aws:ec2:test-region-a:111122223333:instance/i-abc",
        tags={"Name": "one"},
        observed_state={"state": "running"},
        discovered_at=first_seen,
    )
    upsert_resources(
        db_session,
        account=account,
        run=run,
        resources=[resource],
        collector_version="1.0.0",
        source="test",
    )
    db_session.commit()

    stored = db_session.scalar(select(Resource))
    assert stored is not None
    assert stored.identity_key == resource.arn
    assert stored.discovered_at.replace(tzinfo=UTC) == first_seen
    original_id = stored.id

    resource.tags = {"Name": "two"}
    resource.observed_state = {"state": "stopped"}
    resource.discovered_at = datetime(2025, 1, 1, tzinfo=UTC)
    upsert_resources(
        db_session,
        account=account,
        run=run,
        resources=[resource],
        collector_version="1.1.0",
        source="test",
    )
    db_session.commit()

    rows = list(db_session.scalars(select(Resource)))
    assert len(rows) == 1
    assert rows[0].id == original_id
    assert rows[0].tags["Name"] == "two"
    assert rows[0].observed_state["state"] == "stopped"
    assert rows[0].collector_version == "1.1.0"
    # discovered_at preserved on upsert
    assert rows[0].discovered_at.replace(tzinfo=UTC) == first_seen


def test_mark_stale_resources(db_session: Session) -> None:
    account = get_or_create_account(
        db_session,
        aws_account_id="111122223333",
        project_alias="demo",
        account_alias="acct",
    )
    run = create_inventory_run(
        db_session,
        account=account,
        collector_name="test.collector",
        collector_version="1.0.0",
        source="test",
        region="test-region-a",
        service="ec2",
        resource_type="instance",
    )
    kept = NormalizedResource(
        account_id="111122223333",
        region="test-region-a",
        service="ec2",
        resource_type="instance",
        resource_id="i-keep",
        arn=None,
    )
    gone = NormalizedResource(
        account_id="111122223333",
        region="test-region-a",
        service="ec2",
        resource_type="instance",
        resource_id="i-gone",
        arn=None,
    )
    upsert_resources(
        db_session,
        account=account,
        run=run,
        resources=[kept, gone],
        collector_version="1.0.0",
        source="test",
    )
    db_session.commit()

    stale = mark_stale_resources(
        db_session,
        account=account,
        run=run,
        region="test-region-a",
        service="ec2",
        resource_type="instance",
        seen_identity_keys={kept.identity_key},
        observed_at=datetime.now(UTC),
    )
    db_session.commit()
    assert stale == 1

    by_id = {
        r.resource_id: r for r in db_session.scalars(select(Resource)).all()
    }
    assert by_id["i-keep"].is_stale is False
    assert by_id["i-gone"].is_stale is True
    assert by_id["i-gone"].stale_at is not None


def test_upsert_relationships(db_session: Session) -> None:
    account = get_or_create_account(
        db_session,
        aws_account_id="111122223333",
        project_alias="demo",
        account_alias="acct",
    )
    run = create_inventory_run(
        db_session,
        account=account,
        collector_name="test.collector",
        collector_version="1.0.0",
        source="test",
    )
    instance = NormalizedResource(
        account_id="111122223333",
        region="test-region-a",
        service="ec2",
        resource_type="instance",
        resource_id="i-1",
        relationships=[
            ResourceEdge(
                relationship_type="uses",
                to_account_id="111122223333",
                to_region="test-region-a",
                to_service="ec2",
                to_resource_type="security-group",
                to_resource_id="sg-1",
            )
        ],
    )
    sg = NormalizedResource(
        account_id="111122223333",
        region="test-region-a",
        service="ec2",
        resource_type="security-group",
        resource_id="sg-1",
    )
    upsert_resources(
        db_session,
        account=account,
        run=run,
        resources=[instance, sg],
        collector_version="1.0.0",
        source="test",
    )
    count = upsert_relationships(
        db_session,
        from_resource=instance,
        edges=instance.relationships,
        collector_version="1.0.0",
        source="test",
    )
    db_session.commit()
    assert count == 1
    assert db_session.scalar(select(ResourceRelationship)) is not None
