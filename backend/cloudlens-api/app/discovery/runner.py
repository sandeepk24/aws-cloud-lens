"""Discovery runner: collect an existing AWS architecture into inventory.

For each configured account it:
1. builds a session manager from the accounts YAML (profile or AssumeRole);
2. calls STS GetCallerIdentity and refuses to collect if the live account ID
   differs from the configured one (prevents storing one account's resources
   under another account's ID);
3. runs every enabled collector through ``CollectionOrchestrator`` — regional
   collectors across the account's configured Regions, global collectors
   (CloudFront, Route 53, CloudFront-scope WAF) once in ``global_control_region``.

Only read-only AWS operations are possible: session clients reject anything
outside Describe*/List*/Get* before signing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.accounts.models import AccountConfig, AccountsConfig, ProjectConfig
from app.collectors.base import PaginatedCollector
from app.collectors.orchestration import (
    CollectionOrchestrator,
    CollectionScope,
    OrchestratorConfig,
)
from app.connectors.aws.client import connector_from_account
from app.connectors.aws.errors import AWSAuthenticationError
from app.connectors.aws.session import AWSSessionManager, create_session_manager

logger = logging.getLogger(__name__)

KUBERNETES_COLLECTOR = "kubernetes.workloads"

SessionManagerFactory = Callable[[ProjectConfig, AccountConfig, AccountsConfig], Any]


@dataclass(slots=True)
class DiscoveryOptions:
    collectors: list[str] | None = None  # subset of the account's enabled collectors
    regions: list[str] | None = None  # subset of the account's configured regions
    max_workers: int = 4
    max_retries: int = 5
    page_size: int = 100


@dataclass(slots=True)
class CollectorOutcome:
    collector: str
    region: str
    status: str
    resources_seen: int = 0
    resources_stale: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AccountDiscoveryReport:
    project_alias: str
    account_alias: str
    account_id: str
    identity_verified: bool = False
    identity_arn: str | None = None
    aborted_reason: str | None = None
    outcomes: list[CollectorOutcome] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.aborted_reason is None and all(o.status == "succeeded" for o in self.outcomes)


def _default_session_manager(
    project: ProjectConfig, account: AccountConfig, config: AccountsConfig
) -> AWSSessionManager:
    return create_session_manager(
        connector_from_account(project, account),
        sts_service_name=config.aws_api.sts_service_name,
    )


def discover_account(
    config: AccountsConfig,
    project_alias: str,
    account_alias: str,
    db_session_factory: sessionmaker[Session],
    *,
    options: DiscoveryOptions | None = None,
    session_manager_factory: SessionManagerFactory | None = None,
    kubernetes_client_factory: Callable[[Any], Any] | None = None,
) -> AccountDiscoveryReport:
    from app.collectors.registry import COLLECTOR_SPECS, build_collector

    opts = options or DiscoveryOptions()
    project, account = config.find_account(project_alias, account_alias)
    report = AccountDiscoveryReport(
        project_alias=project.alias, account_alias=account.alias, account_id=account.account_id
    )
    manager = (session_manager_factory or _default_session_manager)(project, account, config)

    try:
        identity = manager.validate_identity()
    except AWSAuthenticationError as exc:
        report.aborted_reason = f"identity validation failed: {exc}"
        logger.error("Discovery aborted for %s/%s: %s", project.alias, account.alias, exc)
        return report
    report.identity_verified = True
    report.identity_arn = identity.arn

    regions = [r for r in account.regions if not opts.regions or r in opts.regions]
    wanted = [c for c in account.collectors.enabled if not opts.collectors or c in opts.collectors]
    orchestrator = CollectionOrchestrator(
        db_session_factory,
        config=OrchestratorConfig(max_workers=opts.max_workers, max_retries=opts.max_retries),
    )

    def scopes(region_list: list[str]) -> list[CollectionScope]:
        return [
            CollectionScope(
                aws_account_id=account.account_id,
                project_alias=project.alias,
                account_alias=account.alias,
                region=r,
            )
            for r in region_list
        ]

    def run(collector: PaginatedCollector, region_list: list[str]) -> None:
        result = orchestrator.collect(collector, scopes(region_list))
        for scope in result.scopes:
            report.outcomes.append(
                CollectorOutcome(
                    collector=collector.name,
                    region=scope.region,
                    status=scope.status.value,
                    resources_seen=scope.resources_seen,
                    resources_stale=scope.resources_stale,
                    errors=list(scope.errors),
                )
            )

    for name in wanted:
        if name == KUBERNETES_COLLECTOR:
            _run_kubernetes(
                account,
                report,
                run,
                kubernetes_client_factory,
                config.aws_api.ec2_service_name,
            )
            continue
        spec = COLLECTOR_SPECS.get(name)
        if spec is None:
            report.skipped.append(f"{name}: unknown collector")
            continue
        try:
            collector = build_collector(
                name,
                manager,
                config.aws_api,
                page_size=opts.page_size,
                max_retries=opts.max_retries,
            )
        except ValueError as exc:
            report.skipped.append(f"{name}: {exc}")
            continue
        run(collector, [config.aws_api.global_control_region] if spec.is_global else regions)
    return report


def _run_kubernetes(
    account: AccountConfig,
    report: AccountDiscoveryReport,
    run: Callable[[PaginatedCollector, list[str]], None],
    client_factory: Callable[[Any], Any] | None,
    ec2_service_name: str,
) -> None:
    clusters = list(getattr(getattr(account, "kubernetes", None), "clusters", []) or [])
    if not clusters:
        report.skipped.append(f"{KUBERNETES_COLLECTOR}: no kubernetes.clusters configured")
        return
    from app.collectors.kubernetes import create_kubernetes_collectors, kubeconfig_read_client

    for cluster in clusters:
        try:
            client = (
                client_factory(cluster)
                if client_factory
                else kubeconfig_read_client(
                    cluster.kubeconfig_context, kubeconfig_path=cluster.kubeconfig_path
                )
            )
        except Exception as exc:  # noqa: BLE001 - optional adapter; report and continue
            report.skipped.append(f"{KUBERNETES_COLLECTOR}[{cluster.eks_cluster_name}]: {exc}")
            continue
        for collector in create_kubernetes_collectors(
            client,
            cluster_name=cluster.eks_cluster_name,
            cluster_region=cluster.region,
            ec2_service_name=ec2_service_name,
        ):
            run(collector, [cluster.region])


def discover_project(
    config: AccountsConfig,
    project_alias: str,
    db_session_factory: sessionmaker[Session],
    **kwargs: Any,
) -> list[AccountDiscoveryReport]:
    project = next((p for p in config.projects if p.alias == project_alias), None)
    if project is None:
        raise KeyError(f"project '{project_alias}' not found")
    return [
        discover_account(config, project.alias, account.alias, db_session_factory, **kwargs)
        for account in project.accounts
    ]
