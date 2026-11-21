"""EKS collectors using Boto3 control-plane APIs only (no Kubernetes API / kubectl)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from app.collectors.aws_api import AwsDescribeCollector, compact_edges, edge
from app.collectors.base import PaginatedCollector
from app.collectors.normalized import NormalizedResource, ResourcePage
from app.connectors.aws.session import AWSSessionManager


def _parse_dt(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=UTC)
        return dt.isoformat()
    return str(value)


class EksClusterCollector(AwsDescribeCollector):
    """Collect EKS clusters: name, status, version, endpoint configuration.

    Uses only ``list_clusters`` / ``describe_cluster`` — never the Kubernetes API.
    """

    name = "eks.cluster"
    resource_type = "eks-cluster"
    describe_method = "list_clusters"
    result_key = "clusters"
    source = "aws.eks"

    def __init__(
        self,
        session_manager: AWSSessionManager,
        *,
        eks_service_name: str,
        ec2_service_name: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(session_manager, service_name=eks_service_name, **kwargs)
        self.source = "aws.eks"
        self._ec2_service_name = ec2_service_name.strip()

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
        names = [str(n) for n in (listed.get("clusters") or []) if n]
        items: list[NormalizedResource] = []
        for name in names:
            response = self._invoke_soft(
                client,
                "describe_cluster",
                {"name": name},
                region=region,
                context=f"cluster={name}",
            )
            if response is None:
                items.append(
                    NormalizedResource(
                        account_id=account_id,
                        region=region,
                        service=self.service,
                        resource_type=self.resource_type,
                        resource_id=name,
                        observed_state=self._base_state(
                            account_id=account_id,
                            region=region,
                            resource_id=name,
                            name=name,
                            status="access_failed",
                            access_failure=True,
                            discovery_timestamp=datetime.now(UTC).isoformat(),
                        ),
                    )
                )
                continue
            raw = response.get("cluster")
            if isinstance(raw, Mapping):
                items.append(self.normalize(raw, account_id=account_id, region=region))
        token = listed.get("nextToken")
        return ResourcePage(items=items, next_token=str(token) if token else None)

    def normalize(
        self, raw: Mapping[str, Any], *, account_id: str, region: str
    ) -> NormalizedResource:
        name = str(raw.get("name") or "")
        arn = raw.get("arn")
        status = raw.get("status")
        version = raw.get("version")
        endpoint = raw.get("endpoint")
        resources_vpc = raw.get("resourcesVpcConfig") or {}
        vpc_id = resources_vpc.get("vpcId") if isinstance(resources_vpc, Mapping) else None
        subnet_ids = (
            [str(s) for s in (resources_vpc.get("subnetIds") or []) if s]
            if isinstance(resources_vpc, Mapping)
            else []
        )
        security_group_ids = (
            [str(g) for g in (resources_vpc.get("securityGroupIds") or []) if g]
            if isinstance(resources_vpc, Mapping)
            else []
        )
        endpoint_public = (
            resources_vpc.get("endpointPublicAccess")
            if isinstance(resources_vpc, Mapping)
            else None
        )
        endpoint_private = (
            resources_vpc.get("endpointPrivateAccess")
            if isinstance(resources_vpc, Mapping)
            else None
        )
        public_cidrs = (
            list(resources_vpc.get("publicAccessCidrs") or [])
            if isinstance(resources_vpc, Mapping)
            else []
        )

        relationships = compact_edges(
            edge(
                relationship_type="belongs_to",
                account_id=account_id,
                region=region,
                service=self._ec2_service_name,
                resource_type="vpc",
                resource_id=vpc_id if isinstance(vpc_id, str) else None,
            )
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
        for sg in security_group_ids:
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

        created = raw.get("createdAt")
        discovered_at = created if isinstance(created, datetime) else datetime.now(UTC)
        if isinstance(discovered_at, datetime) and discovered_at.tzinfo is None:
            discovered_at = discovered_at.replace(tzinfo=UTC)

        return NormalizedResource(
            account_id=account_id,
            region=region,
            service=self.service,
            resource_type=self.resource_type,
            resource_id=name,
            arn=str(arn) if arn else None,
            tags={str(k): str(v) for k, v in (raw.get("tags") or {}).items()},
            observed_state=self._base_state(
                account_id=account_id,
                region=region,
                resource_id=name,
                name=name,
                status=status,
                version=version,
                endpoint=endpoint,
                endpoint_configuration={
                    "public_access": endpoint_public,
                    "private_access": endpoint_private,
                    "public_access_cidrs": public_cidrs,
                },
                vpc_id=vpc_id,
                subnet_ids=subnet_ids,
                security_group_ids=security_group_ids,
                role_arn=raw.get("roleArn"),
                platform_version=raw.get("platformVersion"),
                kubernetes={
                    "cluster_logging": (raw.get("logging") or {}).get("clusterLogging"),
                },
                created_at=_parse_dt(created),
                discovery_timestamp=datetime.now(UTC).isoformat(),
            ),
            relationships=relationships,
            discovered_at=discovered_at,
        )


class EksNodeGroupCollector(AwsDescribeCollector):
    """Collect EKS managed node groups (AWS API only)."""

    name = "eks.node_group"
    resource_type = "eks-nodegroup"
    describe_method = "list_nodegroups"
    result_key = "nodegroups"
    source = "aws.eks"

    def __init__(
        self,
        session_manager: AWSSessionManager,
        *,
        eks_service_name: str,
        ec2_service_name: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(session_manager, service_name=eks_service_name, **kwargs)
        self.source = "aws.eks"
        self._ec2_service_name = ec2_service_name.strip()

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
        cluster_names = [str(n) for n in (clusters_listed.get("clusters") or []) if n]
        items: list[NormalizedResource] = []
        for cluster_name in cluster_names:
            nodegroups = self._list_all_names(
                client,
                method="list_nodegroups",
                result_key="nodegroups",
                base_params={"clusterName": cluster_name},
                region=region,
                context=f"nodegroups cluster={cluster_name}",
            )
            if nodegroups is None:
                continue
            for ng_name in nodegroups:
                response = self._invoke_soft(
                    client,
                    "describe_nodegroup",
                    {"clusterName": cluster_name, "nodegroupName": ng_name},
                    region=region,
                    context=f"nodegroup={cluster_name}/{ng_name}",
                )
                if response is None:
                    items.append(
                        NormalizedResource(
                            account_id=account_id,
                            region=region,
                            service=self.service,
                            resource_type=self.resource_type,
                            resource_id=f"{cluster_name}/{ng_name}",
                            observed_state=self._base_state(
                                account_id=account_id,
                                region=region,
                                resource_id=f"{cluster_name}/{ng_name}",
                                name=ng_name,
                                status="access_failed",
                                access_failure=True,
                                cluster_name=cluster_name,
                                discovery_timestamp=datetime.now(UTC).isoformat(),
                            ),
                        )
                    )
                    continue
                raw = response.get("nodegroup")
                if isinstance(raw, Mapping):
                    items.append(
                        self.normalize(
                            raw,
                            account_id=account_id,
                            region=region,
                            cluster_name=cluster_name,
                        )
                    )
        token = clusters_listed.get("nextToken")
        return ResourcePage(items=items, next_token=str(token) if token else None)

    def _list_all_names(
        self,
        client: Any,
        *,
        method: str,
        result_key: str,
        base_params: dict[str, Any],
        region: str,
        context: str,
    ) -> list[str] | None:
        names: list[str] = []
        token: str | None = None
        while True:
            params = {
                **base_params,
                "maxResults": min(self._page_size, 100),
            }
            if token:
                params["nextToken"] = token
            response = self._invoke_soft(
                client, method, params, region=region, context=context
            )
            if response is None:
                return None
            names.extend(str(n) for n in (response.get(result_key) or []) if n)
            token = response.get("nextToken")
            if not token:
                break
        return names

    def normalize(
        self,
        raw: Mapping[str, Any],
        *,
        account_id: str,
        region: str,
        cluster_name: str | None = None,
    ) -> NormalizedResource:
        ng_name = str(raw.get("nodegroupName") or "")
        cluster = str(cluster_name or raw.get("clusterName") or "")
        resource_id = f"{cluster}/{ng_name}"
        arn = raw.get("nodegroupArn")
        status = raw.get("status")
        scaling = raw.get("scalingConfig") or {}
        instance_types = [str(t) for t in (raw.get("instanceTypes") or []) if t]
        subnets = [str(s) for s in (raw.get("subnets") or []) if s]
        relationships = compact_edges(
            edge(
                relationship_type="belongs_to",
                account_id=account_id,
                region=region,
                service=self.service,
                resource_type="eks-cluster",
                resource_id=cluster or None,
            )
        )
        for subnet_id in subnets:
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
        return NormalizedResource(
            account_id=account_id,
            region=region,
            service=self.service,
            resource_type=self.resource_type,
            resource_id=resource_id,
            arn=str(arn) if arn else None,
            tags={str(k): str(v) for k, v in (raw.get("tags") or {}).items()},
            observed_state=self._base_state(
                account_id=account_id,
                region=region,
                resource_id=resource_id,
                name=ng_name,
                status=status,
                cluster_name=cluster,
                ami_type=raw.get("amiType"),
                capacity_type=raw.get("capacityType"),
                instance_types=instance_types,
                disk_size=raw.get("diskSize"),
                scaling_config={
                    "min_size": scaling.get("minSize") if isinstance(scaling, Mapping) else None,
                    "max_size": scaling.get("maxSize") if isinstance(scaling, Mapping) else None,
                    "desired_size": (
                        scaling.get("desiredSize") if isinstance(scaling, Mapping) else None
                    ),
                },
                subnets=subnets,
                node_role=raw.get("nodeRole"),
                version=raw.get("version"),
                release_version=raw.get("releaseVersion"),
                created_at=_parse_dt(raw.get("createdAt")),
                discovery_timestamp=datetime.now(UTC).isoformat(),
            ),
            relationships=relationships,
        )


class EksFargateProfileCollector(AwsDescribeCollector):
    """Collect EKS Fargate profiles (AWS API only)."""

    name = "eks.fargate_profile"
    resource_type = "eks-fargate-profile"
    describe_method = "list_fargate_profiles"
    result_key = "fargateProfileNames"
    source = "aws.eks"

    def __init__(
        self,
        session_manager: AWSSessionManager,
        *,
        eks_service_name: str,
        ec2_service_name: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(session_manager, service_name=eks_service_name, **kwargs)
        self.source = "aws.eks"
        self._ec2_service_name = ec2_service_name.strip()

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
        cluster_names = [str(n) for n in (clusters_listed.get("clusters") or []) if n]
        items: list[NormalizedResource] = []
        for cluster_name in cluster_names:
            profiles = self._list_all_profile_names(
                client, cluster_name=cluster_name, region=region
            )
            if profiles is None:
                continue
            for profile_name in profiles:
                response = self._invoke_soft(
                    client,
                    "describe_fargate_profile",
                    {
                        "clusterName": cluster_name,
                        "fargateProfileName": profile_name,
                    },
                    region=region,
                    context=f"fargate={cluster_name}/{profile_name}",
                )
                if response is None:
                    items.append(
                        NormalizedResource(
                            account_id=account_id,
                            region=region,
                            service=self.service,
                            resource_type=self.resource_type,
                            resource_id=f"{cluster_name}/{profile_name}",
                            observed_state=self._base_state(
                                account_id=account_id,
                                region=region,
                                resource_id=f"{cluster_name}/{profile_name}",
                                name=profile_name,
                                status="access_failed",
                                access_failure=True,
                                cluster_name=cluster_name,
                                discovery_timestamp=datetime.now(UTC).isoformat(),
                            ),
                        )
                    )
                    continue
                raw = response.get("fargateProfile")
                if isinstance(raw, Mapping):
                    items.append(
                        self.normalize(
                            raw,
                            account_id=account_id,
                            region=region,
                            cluster_name=cluster_name,
                        )
                    )
        token = clusters_listed.get("nextToken")
        return ResourcePage(items=items, next_token=str(token) if token else None)

    def _list_all_profile_names(
        self, client: Any, *, cluster_name: str, region: str
    ) -> list[str] | None:
        names: list[str] = []
        token: str | None = None
        while True:
            params: dict[str, Any] = {
                "clusterName": cluster_name,
                "maxResults": min(self._page_size, 100),
            }
            if token:
                params["nextToken"] = token
            response = self._invoke_soft(
                client,
                "list_fargate_profiles",
                params,
                region=region,
                context=f"list_fargate_profiles cluster={cluster_name}",
            )
            if response is None:
                return None
            names.extend(
                str(n) for n in (response.get("fargateProfileNames") or []) if n
            )
            token = response.get("nextToken")
            if not token:
                break
        return names

    def normalize(
        self,
        raw: Mapping[str, Any],
        *,
        account_id: str,
        region: str,
        cluster_name: str | None = None,
    ) -> NormalizedResource:
        profile_name = str(raw.get("fargateProfileName") or "")
        cluster = str(cluster_name or raw.get("clusterName") or "")
        resource_id = f"{cluster}/{profile_name}"
        arn = raw.get("fargateProfileArn")
        status = raw.get("status")
        subnets = [str(s) for s in (raw.get("subnets") or []) if s]
        selectors = [
            {
                "namespace": s.get("namespace"),
                "labels": s.get("labels") or {},
            }
            for s in (raw.get("selectors") or [])
            if isinstance(s, Mapping)
        ]
        relationships = compact_edges(
            edge(
                relationship_type="belongs_to",
                account_id=account_id,
                region=region,
                service=self.service,
                resource_type="eks-cluster",
                resource_id=cluster or None,
            )
        )
        for subnet_id in subnets:
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
        return NormalizedResource(
            account_id=account_id,
            region=region,
            service=self.service,
            resource_type=self.resource_type,
            resource_id=resource_id,
            arn=str(arn) if arn else None,
            tags={str(k): str(v) for k, v in (raw.get("tags") or {}).items()},
            observed_state=self._base_state(
                account_id=account_id,
                region=region,
                resource_id=resource_id,
                name=profile_name,
                status=status,
                cluster_name=cluster,
                pod_execution_role_arn=raw.get("podExecutionRoleArn"),
                subnets=subnets,
                selectors=selectors,
                created_at=_parse_dt(raw.get("createdAt")),
                discovery_timestamp=datetime.now(UTC).isoformat(),
            ),
            relationships=relationships,
        )


def create_eks_collectors(
    session_manager: AWSSessionManager,
    *,
    eks_service_name: str,
    ec2_service_name: str,
    page_size: int = 100,
    max_retries: int = 5,
) -> list[PaginatedCollector]:
    common = {
        "eks_service_name": eks_service_name,
        "ec2_service_name": ec2_service_name,
        "page_size": page_size,
        "max_retries": max_retries,
    }
    return [
        EksClusterCollector(session_manager, **common),
        EksNodeGroupCollector(session_manager, **common),
        EksFargateProfileCollector(session_manager, **common),
    ]
