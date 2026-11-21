"""High-level graph operations shared by the API and the export CLI."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounts.models import AccountsConfig, ProjectConfig
from app.db.models import AwsAccount, CollectionError, InventoryRun, Resource
from app.graph.builder import BuiltGraph, GraphBuilder, observation_range
from app.graph.models import (
    AccountSummary,
    CollectorCoverage,
    Coverage,
    EntryPoint,
    GraphDocument,
    GraphScope,
    MissingPermission,
    NodeDetail,
    ProjectSummary,
    VpcSummary,
)
from app.graph.views import ENTRY_POINT_TYPES, materialize

DEFAULT_MAX_NODES = 400
HARD_MAX_NODES = 2000
APPLICATION_TAG_KEYS = ("Application", "application", "App", "app", "Service", "Project")

ASSUMPTIONS = [
    "Only resources collected by CloudLens for the accounts in scope are shown; "
    "nothing is inferred from naming or shared networks.",
    "Solid edges are verified: the target identifier appears in the source resource's "
    "AWS API record, or two API records name the same DNS endpoint.",
    "Dashed edges are inferred (e.g. a registered IP target matched to an ENI's private "
    "IP at collection time) and must not be treated as proof.",
    "Security group and WAF associations show configuration, not observed network traffic.",
    "Resources in the same VPC, subnet or security group are not connected unless an "
    "API record links them.",
    "External nodes are endpoints referenced by an AWS API record that CloudLens did "
    "not collect (other accounts, non-AWS origins, uncollected services).",
    "Data reflects the last successful collection; see coverage for freshness, "
    "failed collectors and missing permissions.",
]

_IAM_PREFIX = {
    "elbv2": "elasticloadbalancing",
    "apigatewayv2": "apigateway",
    "logs": "logs",
}
_IAM_SPECIAL = {
    ("s3", "list_buckets"): "s3:ListAllMyBuckets",
    ("s3", "get_bucket_encryption"): "s3:GetEncryptionConfiguration",
    ("s3", "get_public_access_block"): "s3:GetBucketPublicAccessBlock",
    ("apigateway", "*"): "apigateway:GET",
    ("apigatewayv2", "*"): "apigateway:GET",
}
_METHOD_RE = re.compile(r"^(?P<method>[a-z0-9_]+) failed:")
_PERMISSION_CODES = {
    "AccessDenied",
    "AccessDeniedException",
    "UnauthorizedOperation",
    "UnauthorizedException",
    "AuthorizationError",
}


def iam_action_for(service: str | None, method: str | None) -> str | None:
    if not service or not method:
        return None
    special = _IAM_SPECIAL.get((service, method)) or _IAM_SPECIAL.get((service, "*"))
    if special:
        return special
    prefix = _IAM_PREFIX.get(service, service)
    return f"{prefix}:{''.join(part.capitalize() for part in method.split('_'))}"


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class GraphScopeError(ValueError):
    """Requested project/account is not authorized by the accounts configuration."""


def authorized_accounts(
    config: AccountsConfig, project_alias: str, requested: list[str] | None
) -> tuple[ProjectConfig, list[str]]:
    """Resolve the account set for a request. Never widens beyond the project."""
    project = next((p for p in config.projects if p.alias == project_alias), None)
    if project is None:
        raise GraphScopeError(f"Unknown project '{project_alias}'")
    allowed = [a.account_id for a in project.accounts]
    if not requested:
        return project, allowed
    outside = sorted(set(requested) - set(allowed))
    if outside:
        raise GraphScopeError(
            f"Account(s) {', '.join(outside)} are not part of project '{project_alias}'"
        )
    return project, sorted(set(requested))


def load_graph(session: Session, account_ids: list[str]) -> BuiltGraph:
    rows = session.scalars(select(Resource).where(Resource.aws_account_id.in_(account_ids))).all()
    return GraphBuilder(rows, account_ids=account_ids).build()


def coverage(session: Session, built: BuiltGraph) -> Coverage:
    accounts = list(built.account_ids)
    rows = session.execute(
        select(InventoryRun, AwsAccount.aws_account_id)
        .join(AwsAccount, InventoryRun.account_pk == AwsAccount.id)
        .where(AwsAccount.aws_account_id.in_(accounts))
    ).all()
    epoch = datetime.min.replace(tzinfo=UTC)
    latest: dict[tuple[str, str | None, str], InventoryRun] = {}
    for run, acct in rows:
        key = (acct, run.region, run.collector_name)
        current = latest.get(key)
        started = _aware(run.started_at) or epoch
        if current is None or started > (_aware(current.started_at) or epoch):
            latest[key] = run
    acct_of_run = {run.id: acct for (acct, _r, _c), run in latest.items()}

    collectors = [
        CollectorCoverage(
            account_id=acct,
            region=region,
            collector=name,
            status=run.status,
            finished_at=run.finished_at,
            resources_seen=run.resources_seen or 0,
            error_count=run.error_count or 0,
        )
        for (acct, region, name), run in sorted(latest.items(), key=lambda kv: str(kv[0]))
    ]
    run_ids = [run.id for run in latest.values()]
    missing: list[MissingPermission] = []
    if run_ids:
        errors = session.scalars(
            select(CollectionError).where(CollectionError.inventory_run_id.in_(run_ids))
        ).all()
        for err in errors:
            if err.error_code not in _PERMISSION_CODES:
                continue
            match = _METHOD_RE.match(err.message or "")
            missing.append(
                MissingPermission(
                    account_id=acct_of_run[err.inventory_run_id],
                    region=err.region,
                    collector=err.collector_name,
                    iam_action=iam_action_for(
                        err.service, match.group("method") if match else None
                    ),
                    error_code=err.error_code,
                    message=err.message[:500],
                )
            )
    # Per-item soft failures (one denied cluster / service) are stored as
    # ``access_failed`` placeholders by collectors; surface them too.
    for _nid, row in built.rows.items():
        state = row.observed_state or {}
        if state.get("access_failure"):
            missing.append(
                MissingPermission(
                    account_id=row.aws_account_id,
                    region=row.region,
                    collector=f"{row.service}.{row.resource_type}",
                    iam_action=None,
                    error_code="access_failed",
                    message=f"Details unavailable for {row.resource_id} (access denied)",
                )
            )
    oldest, newest = observation_range(built.rows.values())
    return Coverage(
        collectors=collectors,
        missing_permissions=missing,
        unresolved_references=built.unresolved,
        stale_resources=sum(1 for r in built.rows.values() if r.is_stale),
        oldest_observation=oldest,
        newest_observation=newest,
    )


def build_document(
    session: Session, config: AccountsConfig, scope: GraphScope, *, max_nodes: int
) -> GraphDocument:
    _, accounts = authorized_accounts(config, scope.project_alias, scope.account_ids)
    scope = scope.model_copy(update={"account_ids": accounts})
    built = load_graph(session, accounts)
    nodes, edges, groups, stats = materialize(
        built, scope, max_nodes=max(1, min(max_nodes, HARD_MAX_NODES))
    )
    return GraphDocument(
        generated_at=datetime.now(UTC),
        scope=scope,
        nodes=nodes,
        edges=edges,
        groups=groups,
        stats=stats,
        coverage=coverage(session, built),
        assumptions=ASSUMPTIONS,
    )


def node_detail(
    session: Session, config: AccountsConfig, project_alias: str, nid: str
) -> NodeDetail | None:
    _, accounts = authorized_accounts(config, project_alias, None)
    account = nid.split(":", 1)[0]
    if account not in accounts:
        return None  # never reveal whether another account's node exists
    built = load_graph(session, accounts)
    if nid not in built.graph:
        return None
    g = built.graph
    row = g.nodes[nid]["row"]
    return NodeDetail(
        node=g.nodes[nid]["model"],
        observed_state=dict(row.observed_state or {}) if row is not None else {},
        inbound=[d["model"] for _, _, d in g.in_edges(nid, data=True)],
        outbound=[d["model"] for _, _, d in g.out_edges(nid, data=True)],
    )


def project_summaries(session: Session, config: AccountsConfig) -> list[ProjectSummary]:
    out: list[ProjectSummary] = []
    for project in config.projects:
        accounts = [a.account_id for a in project.accounts]
        rows = session.scalars(
            select(Resource).where(
                Resource.aws_account_id.in_(accounts), Resource.is_stale.is_(False)
            )
        ).all()
        vpc_counts: Counter[tuple[str, str, str]] = Counter()
        vpc_labels: dict[tuple[str, str, str], str] = {}
        app_tags: dict[str, set[str]] = defaultdict(set)
        entries: list[EntryPoint] = []
        for row in rows:
            state = row.observed_state or {}
            if row.resource_type == "vpc":
                key = (row.aws_account_id, row.region, row.resource_id)
                vpc_labels[key] = (row.tags or {}).get("Name") or row.resource_id
            elif isinstance(state.get("vpc_id"), str):
                vpc_counts[(row.aws_account_id, row.region, state["vpc_id"])] += 1
            for key in APPLICATION_TAG_KEYS:
                value = (row.tags or {}).get(key)
                if value:
                    app_tags[key].add(str(value))
            if row.resource_type in ENTRY_POINT_TYPES:
                entries.append(
                    EntryPoint(
                        node_id=f"{row.aws_account_id}:{row.region}:{row.resource_type}:{row.identity_key}",
                        label=(row.tags or {}).get("Name") or state.get("name") or row.resource_id,
                        resource_type=row.resource_type,
                        account_id=row.aws_account_id,
                        region=row.region,
                    )
                )
        vpcs = [
            VpcSummary(
                account_id=a,
                region=r,
                vpc_id=v,
                label=vpc_labels.get((a, r, v), v),
                resource_count=vpc_counts.get((a, r, v), 0),
            )
            for (a, r, v) in sorted(set(vpc_labels) | set(vpc_counts))
        ]
        out.append(
            ProjectSummary(
                project_alias=project.alias,
                accounts=[
                    AccountSummary(
                        account_alias=a.alias, account_id=a.account_id, regions=a.regions
                    )
                    for a in project.accounts
                ],
                vpcs=vpcs,
                services=dict(sorted(Counter(r.service for r in rows).items())),
                entry_points=sorted(entries, key=lambda e: (e.resource_type, e.label))[:500],
                application_tags={k: sorted(v)[:200] for k, v in sorted(app_tags.items())},
            )
        )
    return out
