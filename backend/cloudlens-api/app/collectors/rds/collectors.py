"""RDS/Aurora collectors — configuration metadata only (no secrets/data)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from app.collectors.aws_api import AwsDescribeCollector, compact_edges, edge
from app.collectors.base import PaginatedCollector
from app.collectors.normalized import NormalizedResource, ResourceEdge
from app.connectors.aws.session import AWSSessionManager


def _parse_dt(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=UTC)
        return dt.isoformat()
    return str(value)


class RdsInstanceCollector(AwsDescribeCollector):
    """Collect RDS DB instances (including Aurora instance members)."""

    name = "rds.db_instance"
    resource_type = "rds-db-instance"
    describe_method = "describe_db_instances"
    result_key = "DBInstances"
    token_param = "Marker"
    token_response_key = "Marker"
    page_size_param = "MaxRecords"
    source = "aws.rds"

    def __init__(
        self,
        session_manager: AWSSessionManager,
        *,
        rds_service_name: str,
        ec2_service_name: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(session_manager, service_name=rds_service_name, **kwargs)
        self.source = "aws.rds"
        self._ec2_service_name = ec2_service_name.strip()

    def normalize(
        self, raw: Mapping[str, Any], *, account_id: str, region: str
    ) -> NormalizedResource:
        resource_id = str(raw["DBInstanceIdentifier"])
        arn = raw.get("DBInstanceArn")
        status = raw.get("DBInstanceStatus")
        engine = raw.get("Engine")
        endpoint = raw.get("Endpoint") or {}
        subnet_group = raw.get("DBSubnetGroup") or {}
        vpc_id = (
            subnet_group.get("VpcId") if isinstance(subnet_group, Mapping) else None
        )
        subnet_ids = [
            str(s.get("SubnetIdentifier"))
            for s in (
                (subnet_group.get("Subnets") or [])
                if isinstance(subnet_group, Mapping)
                else []
            )
            if isinstance(s, Mapping) and s.get("SubnetIdentifier")
        ]
        sg_ids = [
            str(g.get("VpcSecurityGroupId"))
            for g in (raw.get("VpcSecurityGroups") or [])
            if isinstance(g, Mapping) and g.get("VpcSecurityGroupId")
        ]
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
        for sg in sg_ids:
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
        cluster_id = raw.get("DBClusterIdentifier")
        if cluster_id:
            relationships.extend(
                compact_edges(
                    edge(
                        relationship_type="belongs_to",
                        account_id=account_id,
                        region=region,
                        service=self.service,
                        resource_type="rds-db-cluster",
                        resource_id=str(cluster_id),
                    )
                )
            )

        subnet_group_name = (
            subnet_group.get("DBSubnetGroupName") if isinstance(subnet_group, Mapping) else None
        )
        if subnet_group_name:
            relationships.append(
                ResourceEdge(
                    relationship_type="uses",
                    to_account_id=account_id,
                    to_region=region,
                    to_service=self.service,
                    to_resource_type="rds-db-subnet-group",
                    to_resource_id=str(subnet_group_name),
                    evidence="DescribeDBInstances:DBSubnetGroup.DBSubnetGroupName",
                )
            )

        # Intentionally omit MasterUsername password and any data-plane fields.
        return NormalizedResource(
            account_id=account_id,
            region=region,
            service=self.service,
            resource_type=self.resource_type,
            resource_id=resource_id,
            arn=str(arn) if arn else None,
            tags={
                str(t.get("Key")): str(t.get("Value") or "")
                for t in (raw.get("TagList") or [])
                if isinstance(t, Mapping) and t.get("Key") is not None
            },
            observed_state=self._base_state(
                account_id=account_id,
                region=region,
                resource_id=resource_id,
                name=resource_id,
                status=status,
                engine=engine,
                engine_version=raw.get("EngineVersion"),
                db_instance_class=raw.get("DBInstanceClass"),
                multi_az=raw.get("MultiAZ"),
                storage_type=raw.get("StorageType"),
                allocated_storage=raw.get("AllocatedStorage"),
                storage_encrypted=raw.get("StorageEncrypted"),
                publicly_accessible=raw.get("PubliclyAccessible"),
                availability_zone=raw.get("AvailabilityZone"),
                endpoint_address=(
                    endpoint.get("Address") if isinstance(endpoint, Mapping) else None
                ),
                endpoint_port=(
                    endpoint.get("Port") if isinstance(endpoint, Mapping) else None
                ),
                db_cluster_identifier=cluster_id,
                db_subnet_group_name=subnet_group_name,
                iam_database_authentication_enabled=raw.get(
                    "IAMDatabaseAuthenticationEnabled"
                ),
                deletion_protection=raw.get("DeletionProtection"),
                instance_create_time=_parse_dt(raw.get("InstanceCreateTime")),
                discovery_timestamp=datetime.now(UTC).isoformat(),
            ),
            relationships=relationships,
        )


class RdsClusterCollector(AwsDescribeCollector):
    """Collect Aurora / RDS DB clusters (configuration metadata only)."""

    name = "rds.db_cluster"
    resource_type = "rds-db-cluster"
    describe_method = "describe_db_clusters"
    result_key = "DBClusters"
    token_param = "Marker"
    token_response_key = "Marker"
    page_size_param = "MaxRecords"
    source = "aws.rds"

    def __init__(
        self,
        session_manager: AWSSessionManager,
        *,
        rds_service_name: str,
        ec2_service_name: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(session_manager, service_name=rds_service_name, **kwargs)
        self.source = "aws.rds"
        self._ec2_service_name = ec2_service_name.strip()

    def normalize(
        self, raw: Mapping[str, Any], *, account_id: str, region: str
    ) -> NormalizedResource:
        resource_id = str(raw["DBClusterIdentifier"])
        arn = raw.get("DBClusterArn")
        status = raw.get("Status")
        members = [
            str(m.get("DBInstanceIdentifier"))
            for m in (raw.get("DBClusterMembers") or [])
            if isinstance(m, Mapping) and m.get("DBInstanceIdentifier")
        ]
        sg_ids = [
            str(g.get("VpcSecurityGroupId"))
            for g in (raw.get("VpcSecurityGroups") or [])
            if isinstance(g, Mapping) and g.get("VpcSecurityGroupId")
        ]
        relationships = []
        for member in members:
            relationships.extend(
                compact_edges(
                    edge(
                        relationship_type="contains",
                        account_id=account_id,
                        region=region,
                        service=self.service,
                        resource_type="rds-db-instance",
                        resource_id=member,
                    )
                )
            )
        for sg in sg_ids:
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
        return NormalizedResource(
            account_id=account_id,
            region=region,
            service=self.service,
            resource_type=self.resource_type,
            resource_id=resource_id,
            arn=str(arn) if arn else None,
            tags={
                str(t.get("Key")): str(t.get("Value") or "")
                for t in (raw.get("TagList") or [])
                if isinstance(t, Mapping) and t.get("Key") is not None
            },
            observed_state=self._base_state(
                account_id=account_id,
                region=region,
                resource_id=resource_id,
                name=resource_id,
                status=status,
                engine=raw.get("Engine"),
                engine_version=raw.get("EngineVersion"),
                engine_mode=raw.get("EngineMode"),
                multi_az=raw.get("MultiAZ"),
                storage_encrypted=raw.get("StorageEncrypted"),
                endpoint=raw.get("Endpoint"),
                reader_endpoint=raw.get("ReaderEndpoint"),
                port=raw.get("Port"),
                db_subnet_group=raw.get("DBSubnetGroup"),
                db_cluster_members=members,
                deletion_protection=raw.get("DeletionProtection"),
                iam_database_authentication_enabled=raw.get(
                    "IAMDatabaseAuthenticationEnabled"
                ),
                cluster_create_time=_parse_dt(raw.get("ClusterCreateTime")),
                discovery_timestamp=datetime.now(UTC).isoformat(),
            ),
            relationships=relationships,
        )


def create_rds_collectors(
    session_manager: AWSSessionManager,
    *,
    rds_service_name: str,
    ec2_service_name: str,
    page_size: int = 100,
    max_retries: int = 5,
) -> list[PaginatedCollector]:
    common = {
        "rds_service_name": rds_service_name,
        "ec2_service_name": ec2_service_name,
        "page_size": page_size,
        "max_retries": max_retries,
    }
    return [
        RdsInstanceCollector(session_manager, **common),
        RdsClusterCollector(session_manager, **common),
    ]


class RdsDbSubnetGroupCollector(AwsDescribeCollector):
    """Collect DB subnet groups (DescribeDBSubnetGroups, Marker pagination)."""

    name = "rds.db_subnet_group"
    resource_type = "rds-db-subnet-group"
    describe_method = "describe_db_subnet_groups"
    result_key = "DBSubnetGroups"
    token_param = "Marker"
    token_response_key = "Marker"
    page_size_param = "MaxRecords"
    source = "aws.rds"

    def __init__(
        self,
        session_manager: AWSSessionManager,
        *,
        rds_service_name: str,
        ec2_service_name: str,
        **kwargs: Any,
    ) -> None:
        # DescribeDBSubnetGroups MaxRecords must be 20..100.
        kwargs["page_size"] = max(20, min(int(kwargs.get("page_size", 100)), 100))
        super().__init__(session_manager, service_name=rds_service_name, **kwargs)
        self.source = "aws.rds"
        self._ec2_service_name = ec2_service_name.strip()

    def normalize(
        self, raw: Mapping[str, Any], *, account_id: str, region: str
    ) -> NormalizedResource:
        name = str(raw["DBSubnetGroupName"])
        arn = raw.get("DBSubnetGroupArn")
        vpc_id = raw.get("VpcId")
        subnet_ids = [
            str(s.get("SubnetIdentifier"))
            for s in raw.get("Subnets") or []
            if isinstance(s, Mapping) and s.get("SubnetIdentifier")
        ]
        relationships: list[ResourceEdge] = []
        if vpc_id:
            relationships.append(
                ResourceEdge(
                    relationship_type="belongs_to", to_account_id=account_id,
                    to_region=region, to_service=self._ec2_service_name,
                    to_resource_type="vpc", to_resource_id=str(vpc_id),
                    evidence="DescribeDBSubnetGroups:VpcId",
                )
            )
        for subnet_id in subnet_ids:
            relationships.append(
                ResourceEdge(
                    relationship_type="contains", to_account_id=account_id,
                    to_region=region, to_service=self._ec2_service_name,
                    to_resource_type="subnet", to_resource_id=subnet_id,
                    evidence="DescribeDBSubnetGroups:Subnets[].SubnetIdentifier",
                )
            )
        return NormalizedResource(
            account_id=account_id,
            region=region,
            service=self.service,
            resource_type=self.resource_type,
            resource_id=name,
            arn=str(arn) if arn else None,
            observed_state=self._base_state(
                account_id=account_id,
                region=region,
                resource_id=name,
                name=name,
                status=raw.get("SubnetGroupStatus"),
                description=raw.get("DBSubnetGroupDescription"),
                vpc_id=vpc_id,
                subnet_ids=subnet_ids,
                subnet_availability_zones={
                    str(s.get("SubnetIdentifier")): (
                        (s.get("SubnetAvailabilityZone") or {}).get("Name")
                    )
                    for s in raw.get("Subnets") or []
                    if isinstance(s, Mapping) and s.get("SubnetIdentifier")
                },
                discovery_timestamp=datetime.now(UTC).isoformat(),
            ),
            relationships=relationships,
        )
