"""ECS collectors using Boto3 control-plane APIs only."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from app.collectors.aws_api import (
    AwsDescribeCollector,
    chunked,
    compact_edges,
    edge,
)
from app.collectors.base import PaginatedCollector
from app.collectors.normalized import NormalizedResource, ResourceEdge, ResourcePage
from app.connectors.aws.session import AWSSessionManager

# ECS describe_services accepts at most 10 service names per call.
_ECS_DESCRIBE_SERVICES_BATCH = 10
# ECS describe_clusters accepts at most 100 cluster names/ARNs per call.
_ECS_DESCRIBE_CLUSTERS_BATCH = 100
# ECS describe_tasks accepts at most 100 task ARNs per call.
_ECS_DESCRIBE_TASKS_BATCH = 100


def _arn_name(arn_or_name: str) -> str:
    if "/" in arn_or_name:
        return arn_or_name.rsplit("/", 1)[-1]
    return arn_or_name


class EcsClusterCollector(AwsDescribeCollector):
    """Collect ECS clusters (status, capacity providers, counts)."""

    name = "ecs.cluster"
    resource_type = "ecs-cluster"
    describe_method = "list_clusters"
    result_key = "clusterArns"
    source = "aws.ecs"

    def __init__(
        self,
        session_manager: AWSSessionManager,
        *,
        ecs_service_name: str,
        describe_batch_size: int = _ECS_DESCRIBE_CLUSTERS_BATCH,
        **kwargs: Any,
    ) -> None:
        super().__init__(session_manager, service_name=ecs_service_name, **kwargs)
        self.source = "aws.ecs"
        self._describe_batch_size = min(describe_batch_size, _ECS_DESCRIBE_CLUSTERS_BATCH)

    def fetch_page(
        self,
        *,
        account_id: str,
        region: str,
        next_token: str | None = None,
    ) -> ResourcePage:
        client = self._get_client(region)
        params: dict[str, Any] = {"maxResults": min(self._page_size, 100)}
        if next_token:
            params["nextToken"] = next_token
        listed = self._invoke(client, "list_clusters", params, region=region)
        arns = [str(a) for a in (listed.get("clusterArns") or []) if a]
        items: list[NormalizedResource] = []
        for batch in chunked(arns, self._describe_batch_size):
            response = self._invoke_soft(
                client,
                "describe_clusters",
                {
                    "clusters": batch,
                    "include": [
                        "ATTACHMENTS",
                        "CONFIGURATIONS",
                        "SETTINGS",
                        "STATISTICS",
                        "TAGS",
                    ],
                },
                region=region,
                context=f"clusters={len(batch)}",
            )
            if response is None:
                continue
            for raw in response.get("clusters") or []:
                if isinstance(raw, Mapping):
                    items.append(
                        self.normalize(raw, account_id=account_id, region=region)
                    )
            # Per-resource failures from ECS are reported but do not abort the page.
            for failure in response.get("failures") or []:
                if isinstance(failure, Mapping):
                    items.append(
                        self._failure_resource(
                            failure,
                            account_id=account_id,
                            region=region,
                            resource_type=self.resource_type,
                        )
                    )
        token = listed.get("nextToken")
        return ResourcePage(items=items, next_token=str(token) if token else None)

    def normalize(
        self, raw: Mapping[str, Any], *, account_id: str, region: str
    ) -> NormalizedResource:
        arn = str(raw.get("clusterArn") or raw.get("clusterName"))
        name = str(raw.get("clusterName") or _arn_name(arn))
        status = raw.get("status")
        capacity_providers = [str(p) for p in (raw.get("capacityProviders") or []) if p]
        default_strategy = [
            {
                "capacity_provider": s.get("capacityProvider"),
                "weight": s.get("weight"),
                "base": s.get("base"),
            }
            for s in (raw.get("defaultCapacityProviderStrategy") or [])
            if isinstance(s, Mapping)
        ]
        tags = {
            str(t.get("key")): str(t.get("value") or "")
            for t in (raw.get("tags") or [])
            if isinstance(t, Mapping) and t.get("key") is not None
        }
        return NormalizedResource(
            account_id=account_id,
            region=region,
            service=self.service,
            resource_type=self.resource_type,
            resource_id=arn,
            arn=arn if arn.startswith("arn:") else None,
            tags=tags,
            observed_state=self._base_state(
                account_id=account_id,
                region=region,
                resource_id=arn,
                name=name,
                status=status,
                active_services_count=raw.get("activeServicesCount"),
                running_tasks_count=raw.get("runningTasksCount"),
                pending_tasks_count=raw.get("pendingTasksCount"),
                registered_container_instances_count=raw.get(
                    "registeredContainerInstancesCount"
                ),
                capacity_providers=capacity_providers,
                default_capacity_provider_strategy=default_strategy,
                settings=raw.get("settings") or [],
                statistics=raw.get("statistics") or [],
                discovery_timestamp=datetime.now(UTC).isoformat(),
            ),
        )

    def _failure_resource(
        self,
        failure: Mapping[str, Any],
        *,
        account_id: str,
        region: str,
        resource_type: str,
    ) -> NormalizedResource:
        arn = str(failure.get("arn") or "unknown")
        reason = str(failure.get("reason") or "unknown")
        return NormalizedResource(
            account_id=account_id,
            region=region,
            service=self.service,
            resource_type=resource_type,
            resource_id=arn,
            arn=arn if arn.startswith("arn:") else None,
            observed_state=self._base_state(
                account_id=account_id,
                region=region,
                resource_id=arn,
                status="access_failed",
                access_failure=True,
                failure_reason=reason,
                discovery_timestamp=datetime.now(UTC).isoformat(),
            ),
        )


class EcsServiceCollector(AwsDescribeCollector):
    """Collect ECS services: launch type, capacity providers, tasks, networking, TGs."""

    name = "ecs.service"
    resource_type = "ecs-service"
    describe_method = "list_services"
    result_key = "serviceArns"
    source = "aws.ecs"

    def __init__(
        self,
        session_manager: AWSSessionManager,
        *,
        ecs_service_name: str,
        elbv2_service_name: str,
        ec2_service_name: str,
        describe_batch_size: int = _ECS_DESCRIBE_SERVICES_BATCH,
        **kwargs: Any,
    ) -> None:
        super().__init__(session_manager, service_name=ecs_service_name, **kwargs)
        self.source = "aws.ecs"
        self._elbv2_service_name = elbv2_service_name.strip()
        self._ec2_service_name = ec2_service_name.strip()
        self._describe_batch_size = min(describe_batch_size, _ECS_DESCRIBE_SERVICES_BATCH)

    def fetch_page(
        self,
        *,
        account_id: str,
        region: str,
        next_token: str | None = None,
    ) -> ResourcePage:
        client = self._get_client(region)
        cluster_params: dict[str, Any] = {"maxResults": min(self._page_size, 100)}
        if next_token:
            cluster_params["nextToken"] = next_token
        clusters_listed = self._invoke(
            client, "list_clusters", cluster_params, region=region
        )
        cluster_arns = [str(a) for a in (clusters_listed.get("clusterArns") or []) if a]
        items: list[NormalizedResource] = []

        for cluster_arn in cluster_arns:
            service_arns = self._list_all_service_arns(
                client, cluster_arn=cluster_arn, region=region
            )
            if service_arns is None:
                # Soft failure listing services for this cluster — continue others.
                continue
            for batch in chunked(service_arns, self._describe_batch_size):
                # describe_services wants short names or ARNs; ARNs are fine.
                response = self._invoke_soft(
                    client,
                    "describe_services",
                    {
                        "cluster": cluster_arn,
                        "services": batch,
                        "include": ["TAGS"],
                    },
                    region=region,
                    context=f"cluster={cluster_arn}",
                )
                if response is None:
                    continue
                for raw in response.get("services") or []:
                    if isinstance(raw, Mapping):
                        items.append(
                            self.normalize(
                                raw,
                                account_id=account_id,
                                region=region,
                                cluster_arn=cluster_arn,
                            )
                        )
                for failure in response.get("failures") or []:
                    if isinstance(failure, Mapping):
                        items.append(
                            NormalizedResource(
                                account_id=account_id,
                                region=region,
                                service=self.service,
                                resource_type=self.resource_type,
                                resource_id=str(failure.get("arn") or "unknown"),
                                observed_state=self._base_state(
                                    account_id=account_id,
                                    region=region,
                                    resource_id=str(failure.get("arn") or "unknown"),
                                    status="access_failed",
                                    access_failure=True,
                                    failure_reason=failure.get("reason"),
                                    cluster_arn=cluster_arn,
                                    discovery_timestamp=datetime.now(UTC).isoformat(),
                                ),
                            )
                        )

        token = clusters_listed.get("nextToken")
        return ResourcePage(items=items, next_token=str(token) if token else None)

    def _list_all_service_arns(
        self, client: Any, *, cluster_arn: str, region: str
    ) -> list[str] | None:
        arns: list[str] = []
        token: str | None = None
        while True:
            params: dict[str, Any] = {
                "cluster": cluster_arn,
                "maxResults": min(self._page_size, 100),
            }
            if token:
                params["nextToken"] = token
            response = self._invoke_soft(
                client,
                "list_services",
                params,
                region=region,
                context=f"list_services cluster={cluster_arn}",
            )
            if response is None:
                return None
            arns.extend(str(a) for a in (response.get("serviceArns") or []) if a)
            token = response.get("nextToken")
            if not token:
                break
        return arns

    def normalize(
        self,
        raw: Mapping[str, Any],
        *,
        account_id: str,
        region: str,
        cluster_arn: str | None = None,
    ) -> NormalizedResource:
        arn = str(raw.get("serviceArn") or raw.get("serviceName"))
        name = str(raw.get("serviceName") or _arn_name(arn))
        status = raw.get("status")
        cluster = str(cluster_arn or raw.get("clusterArn") or "")
        task_definition = raw.get("taskDefinition")
        launch_type = raw.get("launchType")
        capacity_provider_strategy = [
            {
                "capacity_provider": s.get("capacityProvider"),
                "weight": s.get("weight"),
                "base": s.get("base"),
            }
            for s in (raw.get("capacityProviderStrategy") or [])
            if isinstance(s, Mapping)
        ]
        network = raw.get("networkConfiguration") or {}
        awsvpc = (network.get("awsvpcConfiguration") or {}) if isinstance(network, Mapping) else {}
        subnet_ids = [str(s) for s in (awsvpc.get("subnets") or []) if s]
        security_groups = [str(g) for g in (awsvpc.get("securityGroups") or []) if g]
        load_balancers = [
            {
                "target_group_arn": lb.get("targetGroupArn"),
                "container_name": lb.get("containerName"),
                "container_port": lb.get("containerPort"),
                "load_balancer_name": lb.get("loadBalancerName"),
            }
            for lb in (raw.get("loadBalancers") or [])
            if isinstance(lb, Mapping)
        ]

        relationships = compact_edges(
            edge(
                relationship_type="belongs_to",
                account_id=account_id,
                region=region,
                service=self.service,
                resource_type="ecs-cluster",
                resource_id=cluster or None,
            ),
            edge(
                relationship_type="uses",
                account_id=account_id,
                region=region,
                service=self.service,
                resource_type="ecs-task-definition",
                resource_id=str(task_definition) if task_definition else None,
            ),
        )
        for subnet_id in subnet_ids:
            relationships.extend(
                compact_edges(
                    edge(
                        relationship_type="associated_with",
                        account_id=account_id,
                        region=region,
                        service=self._ec2_service_name,
                        resource_type="subnet",
                        resource_id=subnet_id,
                    )
                )
            )
        for sg in security_groups:
            relationships.extend(
                compact_edges(
                    edge(
                        relationship_type="uses",
                        account_id=account_id,
                        region=region,
                        service=self._ec2_service_name,
                        resource_type="security-group",
                        resource_id=sg,
                    )
                )
            )
        for lb in load_balancers:
            relationships.extend(
                compact_edges(
                    edge(
                        relationship_type="forwards_to",
                        account_id=account_id,
                        region=region,
                        service=self._elbv2_service_name,
                        resource_type="target-group",
                        resource_id=lb.get("target_group_arn"),
                    )
                )
            )

        tags = {
            str(t.get("key")): str(t.get("value") or "")
            for t in (raw.get("tags") or [])
            if isinstance(t, Mapping) and t.get("key") is not None
        }
        return NormalizedResource(
            account_id=account_id,
            region=region,
            service=self.service,
            resource_type=self.resource_type,
            resource_id=arn,
            arn=arn if arn.startswith("arn:") else None,
            tags=tags,
            observed_state=self._base_state(
                account_id=account_id,
                region=region,
                resource_id=arn,
                name=name,
                status=status,
                cluster_arn=cluster,
                task_definition=task_definition,
                launch_type=launch_type,
                capacity_provider_strategy=capacity_provider_strategy,
                desired_count=raw.get("desiredCount"),
                running_count=raw.get("runningCount"),
                pending_count=raw.get("pendingCount"),
                scheduling_strategy=raw.get("schedulingStrategy"),
                platform_version=raw.get("platformVersion"),
                network_configuration={
                    "subnets": subnet_ids,
                    "security_groups": security_groups,
                    "assign_public_ip": awsvpc.get("assignPublicIp"),
                },
                load_balancers=load_balancers,
                discovery_timestamp=datetime.now(UTC).isoformat(),
            ),
            relationships=relationships,
        )


class EcsTaskDefinitionCollector(AwsDescribeCollector):
    """Collect ECS task definitions (family, revision, network mode, containers)."""

    name = "ecs.task_definition"
    resource_type = "ecs-task-definition"
    describe_method = "list_task_definitions"
    result_key = "taskDefinitionArns"
    source = "aws.ecs"

    def __init__(
        self,
        session_manager: AWSSessionManager,
        *,
        ecs_service_name: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(session_manager, service_name=ecs_service_name, **kwargs)
        self.source = "aws.ecs"

    def fetch_page(
        self,
        *,
        account_id: str,
        region: str,
        next_token: str | None = None,
    ) -> ResourcePage:
        client = self._get_client(region)
        params: dict[str, Any] = {
            "maxResults": min(self._page_size, 100),
            "status": "ACTIVE",
        }
        if next_token:
            params["nextToken"] = next_token
        listed = self._invoke(client, "list_task_definitions", params, region=region)
        arns = [str(a) for a in (listed.get("taskDefinitionArns") or []) if a]
        items: list[NormalizedResource] = []
        for arn in arns:
            response = self._invoke_soft(
                client,
                "describe_task_definition",
                {"taskDefinition": arn, "include": ["TAGS"]},
                region=region,
                context=f"taskDefinition={arn}",
            )
            if response is None:
                items.append(
                    NormalizedResource(
                        account_id=account_id,
                        region=region,
                        service=self.service,
                        resource_type=self.resource_type,
                        resource_id=arn,
                        arn=arn,
                        observed_state=self._base_state(
                            account_id=account_id,
                            region=region,
                            resource_id=arn,
                            status="access_failed",
                            access_failure=True,
                            discovery_timestamp=datetime.now(UTC).isoformat(),
                        ),
                    )
                )
                continue
            raw = response.get("taskDefinition")
            if isinstance(raw, Mapping):
                items.append(self.normalize(raw, account_id=account_id, region=region))
        token = listed.get("nextToken")
        return ResourcePage(items=items, next_token=str(token) if token else None)

    def normalize(
        self, raw: Mapping[str, Any], *, account_id: str, region: str
    ) -> NormalizedResource:
        arn = str(raw.get("taskDefinitionArn") or "")
        family = raw.get("family")
        revision = raw.get("revision")
        containers = [
            {
                "name": c.get("name"),
                "image": c.get("image"),
                "cpu": c.get("cpu"),
                "memory": c.get("memory"),
                "essential": c.get("essential"),
                "port_mappings": c.get("portMappings") or [],
            }
            for c in (raw.get("containerDefinitions") or [])
            if isinstance(c, Mapping)
        ]
        return NormalizedResource(
            account_id=account_id,
            region=region,
            service=self.service,
            resource_type=self.resource_type,
            resource_id=arn or f"{family}:{revision}",
            arn=arn or None,
            observed_state=self._base_state(
                account_id=account_id,
                region=region,
                resource_id=arn or f"{family}:{revision}",
                name=str(family) if family else None,
                status=raw.get("status"),
                family=family,
                revision=revision,
                network_mode=raw.get("networkMode"),
                requires_compatibilities=raw.get("requiresCompatibilities") or [],
                cpu=raw.get("cpu"),
                memory=raw.get("memory"),
                container_definitions=containers,
                discovery_timestamp=datetime.now(UTC).isoformat(),
            ),
        )


def create_ecs_collectors(
    session_manager: AWSSessionManager,
    *,
    ecs_service_name: str,
    elbv2_service_name: str,
    ec2_service_name: str,
    page_size: int = 100,
    max_retries: int = 5,
) -> list[PaginatedCollector]:
    common = {
        "ecs_service_name": ecs_service_name,
        "page_size": page_size,
        "max_retries": max_retries,
    }
    return [
        EcsClusterCollector(session_manager, **common),
        EcsServiceCollector(
            session_manager,
            elbv2_service_name=elbv2_service_name,
            ec2_service_name=ec2_service_name,
            **common,
        ),
        EcsTaskDefinitionCollector(session_manager, **common),
    ]


def _attachment_details(task: Mapping[str, Any]) -> list[dict[str, str]]:
    """ENI attachment details from DescribeTasks (awsvpc / Fargate tasks)."""
    found: list[dict[str, str]] = []
    for attachment in task.get("attachments") or []:
        if not isinstance(attachment, Mapping):
            continue
        if attachment.get("type") != "ElasticNetworkInterface":
            continue
        details = {
            str(d.get("name")): str(d.get("value"))
            for d in attachment.get("details") or []
            if isinstance(d, Mapping) and d.get("name") and d.get("value") is not None
        }
        found.append(details)
    return found


class EcsTaskCollector(AwsDescribeCollector):
    """Collect running ECS tasks: clusters page -> list_tasks -> describe_tasks.

    Tasks are ephemeral; each run records what was RUNNING at collection time.
    The task -> service link comes from the task ``group`` ("service:<name>"),
    which ECS sets for service-launched tasks.
    """

    name = "ecs.task"
    resource_type = "ecs-task"
    describe_method = "describe_tasks"
    result_key = "tasks"
    source = "aws.ecs"

    def __init__(
        self,
        session_manager: AWSSessionManager,
        *,
        ecs_service_name: str,
        ec2_service_name: str,
        describe_batch_size: int = _ECS_DESCRIBE_TASKS_BATCH,
        **kwargs: Any,
    ) -> None:
        super().__init__(session_manager, service_name=ecs_service_name, **kwargs)
        self.source = "aws.ecs"
        self._ec2_service_name = ec2_service_name.strip()
        self._describe_batch_size = max(1, min(describe_batch_size, _ECS_DESCRIBE_TASKS_BATCH))

    def fetch_page(
        self,
        *,
        account_id: str,
        region: str,
        next_token: str | None = None,
    ) -> ResourcePage:
        client = self._get_client(region)
        params: dict[str, Any] = {"maxResults": min(self._page_size, 100)}
        if next_token:
            params["nextToken"] = next_token
        listed = self._invoke(client, "list_clusters", params, region=region)
        items: list[NormalizedResource] = []
        for cluster_arn in [str(a) for a in (listed.get("clusterArns") or []) if a]:
            task_arns = self._list_task_arns(client, cluster_arn, region=region)
            for batch in chunked(task_arns, self._describe_batch_size):
                response = self._invoke_soft(
                    client,
                    "describe_tasks",
                    {"cluster": cluster_arn, "tasks": batch, "include": ["TAGS"]},
                    region=region,
                    context=f"tasks cluster={cluster_arn}",
                )
                if response is None:
                    continue
                for raw in response.get("tasks") or []:
                    if isinstance(raw, Mapping) and raw.get("taskArn"):
                        items.append(
                            self.normalize(raw, account_id=account_id, region=region)
                        )
        token = listed.get("nextToken")
        return ResourcePage(items=items, next_token=str(token) if token else None)

    def _list_task_arns(self, client: Any, cluster_arn: str, *, region: str) -> list[str]:
        arns: list[str] = []
        token: str | None = None
        while True:
            params: dict[str, Any] = {"cluster": cluster_arn, "maxResults": 100}
            if token:
                params["nextToken"] = token
            response = self._invoke_soft(
                client, "list_tasks", params, region=region,
                context=f"list_tasks cluster={cluster_arn}",
            )
            if response is None:
                return arns
            arns.extend(str(a) for a in (response.get("taskArns") or []) if a)
            token = response.get("nextToken")
            if not token:
                return arns

    def normalize(
        self, raw: Mapping[str, Any], *, account_id: str, region: str
    ) -> NormalizedResource:
        arn = str(raw["taskArn"])
        cluster_arn = str(raw.get("clusterArn") or "")
        task_def = raw.get("taskDefinitionArn")
        group = raw.get("group")
        service_name = (
            group.split(":", 1)[1]
            if isinstance(group, str) and group.startswith("service:")
            else None
        )
        attachments = _attachment_details(raw)
        eni_ids = [a["networkInterfaceId"] for a in attachments if a.get("networkInterfaceId")]
        subnet_ids = sorted({a["subnetId"] for a in attachments if a.get("subnetId")})
        private_ips = [a["privateIPv4Address"] for a in attachments if a.get("privateIPv4Address")]
        tags = {
            str(t.get("key")): str(t.get("value") or "")
            for t in (raw.get("tags") or [])
            if isinstance(t, Mapping) and t.get("key") is not None
        }

        def _edge(rel: str, service: str, rtype: str, rid: str, evidence: str,
                  arn_: str | None = None) -> ResourceEdge:
            return ResourceEdge(
                relationship_type=rel, to_account_id=account_id, to_region=region,
                to_service=service, to_resource_type=rtype, to_resource_id=rid,
                to_arn=arn_, evidence=evidence,
            )

        relationships: list[ResourceEdge] = []
        if cluster_arn:
            relationships.append(_edge("belongs_to", self.service, "ecs-cluster", cluster_arn,
                                       "DescribeTasks:clusterArn", cluster_arn))
        if task_def:
            relationships.append(_edge("uses", self.service, "ecs-task-definition", str(task_def),
                                       "DescribeTasks:taskDefinitionArn", str(task_def)))
        for eni in eni_ids:
            relationships.append(_edge("uses", self._ec2_service_name, "network-interface", eni,
                                       "DescribeTasks:attachments[].details.networkInterfaceId"))
        for subnet_id in subnet_ids:
            relationships.append(_edge("located_in", self._ec2_service_name, "subnet", subnet_id,
                                       "DescribeTasks:attachments[].details.subnetId"))
        if service_name and cluster_arn:
            relationships.append(_edge("member_of", self.service, "ecs-service",
                                       f"{cluster_arn}/{service_name}", "DescribeTasks:group"))

        return NormalizedResource(
            account_id=account_id,
            region=region,
            service=self.service,
            resource_type=self.resource_type,
            resource_id=arn,
            arn=arn,
            tags=tags,
            observed_state=self._base_state(
                account_id=account_id,
                region=region,
                resource_id=arn,
                name=_arn_name(arn),
                status=raw.get("lastStatus"),
                cluster_arn=cluster_arn or None,
                task_definition_arn=task_def,
                group=group,
                service_name=service_name,
                launch_type=raw.get("launchType"),
                last_status=raw.get("lastStatus"),
                desired_status=raw.get("desiredStatus"),
                health_status=raw.get("healthStatus"),
                availability_zone=raw.get("availabilityZone"),
                eni_ids=eni_ids,
                private_ips=private_ips,
                subnet_id=subnet_ids[0] if len(subnet_ids) == 1 else None,
                subnet_ids=subnet_ids,
                started_at=raw["startedAt"].isoformat()
                if isinstance(raw.get("startedAt"), datetime)
                else raw.get("startedAt"),
                discovery_timestamp=datetime.now(UTC).isoformat(),
            ),
            relationships=relationships,
        )
