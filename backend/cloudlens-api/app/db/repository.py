"""Persistence helpers for accounts, resources, relationships, and errors."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.collectors.normalized import NormalizedResource, ResourceEdge
from app.db.models import (
    AwsAccount,
    CollectionError,
    InventoryRun,
    InventoryRunStatus,
    Resource,
    ResourceRelationship,
)
from app.db.upsert import upsert_mappings


def get_or_create_account(
    session: Session,
    *,
    aws_account_id: str,
    project_alias: str,
    account_alias: str,
) -> AwsAccount:
    existing = session.scalar(select(AwsAccount).where(AwsAccount.aws_account_id == aws_account_id))
    if existing is not None:
        existing.project_alias = project_alias
        existing.account_alias = account_alias
        session.flush()
        return existing

    account = AwsAccount(
        id=str(uuid4()),
        aws_account_id=aws_account_id,
        project_alias=project_alias,
        account_alias=account_alias,
    )
    try:
        with session.begin_nested():
            session.add(account)
            session.flush()
        return account
    except IntegrityError:
        existing = session.scalar(
            select(AwsAccount).where(AwsAccount.aws_account_id == aws_account_id)
        )
        if existing is None:
            raise
        existing.project_alias = project_alias
        existing.account_alias = account_alias
        session.flush()
        return existing


def create_inventory_run(
    session: Session,
    *,
    account: AwsAccount,
    collector_name: str,
    collector_version: str,
    source: str,
    region: str | None = None,
    service: str | None = None,
    resource_type: str | None = None,
) -> InventoryRun:
    run = InventoryRun(
        id=str(uuid4()),
        account_pk=account.id,
        status=InventoryRunStatus.RUNNING.value,
        collector_name=collector_name,
        collector_version=collector_version,
        source=source,
        region=region,
        service=service,
        resource_type=resource_type,
        started_at=datetime.now(UTC),
        stats={},
    )
    session.add(run)
    session.flush()
    return run


def finish_inventory_run(
    session: Session,
    run: InventoryRun,
    *,
    status: InventoryRunStatus,
    message: str | None = None,
) -> None:
    run.status = status.value
    run.finished_at = datetime.now(UTC)
    run.message = message
    session.flush()


def record_collection_error(
    session: Session,
    *,
    run: InventoryRun,
    account: AwsAccount,
    collector_name: str,
    message: str,
    error_code: str | None = None,
    retryable: bool = False,
    region: str | None = None,
    service: str | None = None,
    resource_type: str | None = None,
    details: dict[str, Any] | None = None,
) -> CollectionError:
    err = CollectionError(
        id=str(uuid4()),
        inventory_run_id=run.id,
        account_pk=account.id,
        collector_name=collector_name,
        message=message,
        error_code=error_code,
        retryable=retryable,
        region=region,
        service=service,
        resource_type=resource_type,
        details=details or {},
        occurred_at=datetime.now(UTC),
    )
    session.add(err)
    run.error_count = int(run.error_count or 0) + 1
    session.flush()
    return err


def serialize_edges(
    item: NormalizedResource,
    *,
    collector_name: str | None,
    collector_version: str,
    source_api: str | None,
) -> list[dict[str, Any]]:
    """JSON form of a resource's declared edges, including evidence."""
    out: list[dict[str, Any]] = []
    for e in item.relationships:
        out.append(
            {
                "relationship_type": e.relationship_type,
                "to_account_id": e.to_account_id,
                "to_region": e.to_region,
                "to_service": e.to_service,
                "to_resource_type": e.to_resource_type,
                "to_resource_id": e.to_resource_id,
                "to_arn": e.to_arn,
                "confidence": e.confidence,
                # Only the collector's own annotation; never guess the API field.
                "evidence": e.evidence,
                "collector": collector_name,
                "collector_version": collector_version,
            }
        )
    return out


def upsert_resources(
    session: Session,
    *,
    account: AwsAccount,
    run: InventoryRun,
    resources: list[NormalizedResource],
    collector_version: str,
    source: str,
    collector_name: str | None = None,
    source_api: str | None = None,
) -> list[str]:
    """Upsert resources and return their identity keys (seen set)."""
    if not resources:
        return []

    now = datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    seen: list[str] = []
    for item in resources:
        identity_key = item.identity_key
        seen.append(identity_key)
        rows.append(
            {
                "id": str(uuid4()),
                "account_pk": account.id,
                "aws_account_id": item.account_id,
                "region": item.region,
                "service": item.service,
                "resource_type": item.resource_type,
                "resource_id": item.resource_id,
                "arn": item.arn,
                "identity_key": identity_key,
                "tags": item.tags,
                "observed_state": item.observed_state,
                "declared_edges": serialize_edges(
                    item,
                    collector_name=collector_name,
                    collector_version=collector_version,
                    source_api=source_api,
                ),
                "discovered_at": item.discovered_at,
                "last_seen_at": now,
                "collector_version": collector_version,
                "source": source,
                "is_stale": False,
                "stale_at": None,
                "last_inventory_run_id": run.id,
            }
        )

    # On conflict, keep original id/discovered_at; refresh mutable fields.
    upsert_mappings(
        session,
        table=Resource.__table__,
        rows=rows,
        conflict_columns=[
            "aws_account_id",
            "region",
            "service",
            "resource_type",
            "identity_key",
        ],
        update_columns=[
            "resource_id",
            "arn",
            "tags",
            "observed_state",
            "declared_edges",
            "last_seen_at",
            "collector_version",
            "source",
            "is_stale",
            "stale_at",
            "last_inventory_run_id",
            "account_pk",
        ],
    )
    run.resources_seen = int(run.resources_seen or 0) + len(rows)
    run.resources_upserted = int(run.resources_upserted or 0) + len(rows)
    session.flush()
    return seen


def resolve_resource_pk(
    session: Session,
    *,
    aws_account_id: str,
    region: str,
    service: str,
    resource_type: str,
    identity_key: str,
) -> str | None:
    return session.scalar(
        select(Resource.id).where(
            Resource.aws_account_id == aws_account_id,
            Resource.region == region,
            Resource.service == service,
            Resource.resource_type == resource_type,
            Resource.identity_key == identity_key,
        )
    )


_NaturalKey = tuple[str, str, str, str, str]

# Keep IN-lists well below the PostgreSQL (65535) and legacy SQLite (999)
# bind-parameter limits.
_RESOLVE_CHUNK = 500


def resolve_resource_pks(session: Session, keys: set[_NaturalKey]) -> dict[_NaturalKey, str]:
    """Resolve many natural keys to resource primary keys in bounded batches.

    Keys are grouped by (account, region, service, type) so each batch is a
    simple ``identity_key IN (...)`` lookup served by ``uq_resources_natural_key``.
    """
    groups: dict[tuple[str, str, str, str], list[str]] = {}
    for account_id, region, service, resource_type, identity_key in keys:
        groups.setdefault((account_id, region, service, resource_type), []).append(identity_key)

    resolved: dict[_NaturalKey, str] = {}
    for (account_id, region, service, resource_type), identity_keys in groups.items():
        for start in range(0, len(identity_keys), _RESOLVE_CHUNK):
            chunk = identity_keys[start : start + _RESOLVE_CHUNK]
            rows = session.execute(
                select(Resource.identity_key, Resource.id).where(
                    Resource.aws_account_id == account_id,
                    Resource.region == region,
                    Resource.service == service,
                    Resource.resource_type == resource_type,
                    Resource.identity_key.in_(chunk),
                )
            ).all()
            for identity_key, pk in rows:
                resolved[(account_id, region, service, resource_type, identity_key)] = pk
    return resolved


def _from_key(resource: NormalizedResource) -> _NaturalKey:
    return (
        resource.account_id,
        resource.region,
        resource.service,
        resource.resource_type,
        resource.identity_key,
    )


def _to_key(edge: ResourceEdge) -> _NaturalKey:
    return (
        edge.to_account_id,
        edge.to_region,
        edge.to_service,
        edge.to_resource_type,
        edge.to_identity_key,
    )


def upsert_relationships_batch(
    session: Session,
    *,
    resources: list[NormalizedResource],
    collector_version: str,
    source: str,
) -> int:
    """Upsert outbound edges for many resources with batched key resolution.

    Replaces one ``SELECT`` per resource plus one per edge (N+1) with a few
    bounded ``IN`` lookups per page. Edges whose target is not yet in inventory
    are skipped (unchanged behaviour) — relationships are only persisted when
    both endpoints were observed.
    """
    with_edges = [r for r in resources if r.relationships]
    if not with_edges:
        return 0

    keys: set[_NaturalKey] = set()
    for resource in with_edges:
        keys.add(_from_key(resource))
        keys.update(_to_key(edge) for edge in resource.relationships)
    pks = resolve_resource_pks(session, keys)

    now = datetime.now(UTC)
    rows_by_edge: dict[tuple[str, str, str], dict[str, Any]] = {}
    for resource in with_edges:
        from_pk = pks.get(_from_key(resource))
        if from_pk is None:
            continue
        for edge in resource.relationships:
            to_pk = pks.get(_to_key(edge))
            if to_pk is None:
                # Target not yet in inventory — skip until both sides exist.
                continue
            # De-duplicate within the statement: Postgres rejects an
            # ON CONFLICT DO UPDATE that touches the same row twice.
            rows_by_edge[(from_pk, to_pk, edge.relationship_type)] = {
                "id": str(uuid4()),
                "from_resource_id": from_pk,
                "to_resource_id": to_pk,
                "relationship_type": edge.relationship_type,
                "discovered_at": now,
                "last_seen_at": now,
                "is_stale": False,
                "stale_at": None,
                "collector_version": collector_version,
                "source": source,
            }

    rows = list(rows_by_edge.values())
    if not rows:
        return 0

    upsert_mappings(
        session,
        table=ResourceRelationship.__table__,
        rows=rows,
        conflict_columns=["from_resource_id", "to_resource_id", "relationship_type"],
        update_columns=[
            "last_seen_at",
            "is_stale",
            "stale_at",
            "collector_version",
            "source",
        ],
    )
    session.flush()
    return len(rows)


def upsert_relationships(
    session: Session,
    *,
    from_resource: NormalizedResource,
    edges: list[ResourceEdge],
    collector_version: str,
    source: str,
) -> int:
    """Upsert edges for a single resource (kept for API compatibility)."""
    if not edges:
        return 0
    single = NormalizedResource(
        account_id=from_resource.account_id,
        region=from_resource.region,
        service=from_resource.service,
        resource_type=from_resource.resource_type,
        resource_id=from_resource.resource_id,
        arn=from_resource.arn,
        relationships=list(edges),
    )
    return upsert_relationships_batch(
        session,
        resources=[single],
        collector_version=collector_version,
        source=source,
    )


def mark_stale_resources(
    session: Session,
    *,
    account: AwsAccount,
    run: InventoryRun,
    region: str,
    service: str,
    resource_type: str,
    seen_identity_keys: set[str] | None,
    observed_at: datetime,
) -> int:
    """Mark resources in scope that were not observed as stale.

    When ``seen_identity_keys`` is ``None``, "observed" means the row was
    upserted by ``run`` (``last_inventory_run_id == run.id``). This avoids
    binding one parameter per resource, which fails on PostgreSQL above
    65535 parameters and grows linearly with scope size. Passing an explicit
    set keeps the original identity-key semantics.
    """
    stmt = (
        update(Resource)
        .where(
            and_(
                Resource.account_pk == account.id,
                Resource.region == region,
                Resource.service == service,
                Resource.resource_type == resource_type,
                Resource.is_stale.is_(False),
            )
        )
        .values(is_stale=True, stale_at=observed_at)
    )
    if seen_identity_keys is None:
        stmt = stmt.where(
            or_(
                Resource.last_inventory_run_id.is_(None),
                Resource.last_inventory_run_id != run.id,
            )
        )
    elif seen_identity_keys:
        stmt = stmt.where(Resource.identity_key.not_in(seen_identity_keys))

    result = session.execute(stmt)
    stale_count = result.rowcount or 0
    run.resources_stale = int(run.resources_stale or 0) + int(stale_count)
    session.flush()
    return int(stale_count)
