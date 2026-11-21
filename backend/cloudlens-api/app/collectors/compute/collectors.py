"""EC2 instance, EBS volume, and Elastic IP collectors."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from app.collectors.aws_api import (
    AwsDescribeCollector,
    chunked,
    compact_edges,
    edge,
    tag_name,
    tags_to_dict,
)
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


class Ec2InstanceCollector(AwsDescribeCollector):
    """Collect EC2 instances (type/state, volumes, networking)."""

    name = "compute.ec2_instance"
    resource_type = "instance"
    describe_method = "describe_instances"
    result_key = "Reservations"
    source = "aws.ec2"

    def __init__(self, session_manager: AWSSessionManager, *, ec2_service_name: str, **kwargs: Any):
        super().__init__(session_manager, service_name=ec2_service_name, **kwargs)
        self.source = "aws.ec2"

    def fetch_page(
        self,
        *,
        account_id: str,
        region: str,
        next_token: str | None = None,
    ) -> ResourcePage:
        client = self._get_client(region)
        params: dict[str, Any] = {self.page_size_param: self._page_size}
        if next_token:
            params[self.token_param] = next_token
        response = self._invoke(client, self.describe_method, params, region=region)

        items: list[NormalizedResource] = []
        for reservation in response.get(self.result_key) or []:
            if not isinstance(reservation, Mapping):
                continue
            for instance in reservation.get("Instances") or []:
                if isinstance(instance, Mapping):
                    items.append(
                        self.normalize(instance, account_id=account_id, region=region)
                    )
        token = response.get(self.token_response_key)
        return ResourcePage(
            items=items,
            next_token=str(token) if token else None,
        )

    def normalize(
        self, raw: Mapping[str, Any], *, account_id: str, region: str
    ) -> NormalizedResource:
        resource_id = str(raw["InstanceId"])
        tags = tags_to_dict(raw.get("Tags"))
        name = tag_name(tags)
        state = (raw.get("State") or {}).get("Name")
        vpc_id = raw.get("VpcId")
        subnet_id = raw.get("SubnetId")

        attached_volumes: list[dict[str, Any]] = []
        relationships = compact_edges(
            edge(
                relationship_type="belongs_to",
                account_id=account_id,
                region=region,
                service=self.service,
                resource_type="vpc",
                resource_id=vpc_id if isinstance(vpc_id, str) else None,
            ),
            edge(
                relationship_type="located_in",
                account_id=account_id,
                region=region,
                service=self.service,
                resource_type="subnet",
                resource_id=subnet_id if isinstance(subnet_id, str) else None,
            ),
        )
        for mapping in raw.get("BlockDeviceMappings") or []:
            if not isinstance(mapping, Mapping):
                continue
            ebs = mapping.get("Ebs") or {}
            volume_id = ebs.get("VolumeId")
            attached_volumes.append(
                {
                    "device_name": mapping.get("DeviceName"),
                    "volume_id": volume_id,
                    "status": ebs.get("Status"),
                    "attach_time": _parse_dt(ebs.get("AttachTime")),
                    "delete_on_termination": ebs.get("DeleteOnTermination"),
                }
            )
            relationships.extend(
                compact_edges(
                    edge(
                        relationship_type="attached_to",
                        account_id=account_id,
                        region=region,
                        service=self.service,
                        resource_type="volume",
                        resource_id=volume_id if isinstance(volume_id, str) else None,
                    )
                )
            )

        network_interfaces: list[dict[str, Any]] = []
        for eni in raw.get("NetworkInterfaces") or []:
            if not isinstance(eni, Mapping):
                continue
            eni_id = eni.get("NetworkInterfaceId")
            network_interfaces.append(
                {
                    "network_interface_id": eni_id,
                    "subnet_id": eni.get("SubnetId"),
                    "vpc_id": eni.get("VpcId"),
                    "private_ip_address": eni.get("PrivateIpAddress"),
                    "public_ip": (eni.get("Association") or {}).get("PublicIp"),
                    "groups": [
                        g.get("GroupId")
                        for g in (eni.get("Groups") or [])
                        if isinstance(g, Mapping)
                    ],
                }
            )
            relationships.extend(
                compact_edges(
                    edge(
                        relationship_type="uses",
                        account_id=account_id,
                        region=region,
                        service=self.service,
                        resource_type="network-interface",
                        resource_id=eni_id if isinstance(eni_id, str) else None,
                    )
                )
            )
            for group in eni.get("Groups") or []:
                if isinstance(group, Mapping):
                    relationships.extend(
                        compact_edges(
                            edge(
                                relationship_type="uses",
                                account_id=account_id,
                                region=region,
                                service=self.service,
                                resource_type="security-group",
                                resource_id=group.get("GroupId"),
                            )
                        )
                    )

        security_groups = [
            {"group_id": g.get("GroupId"), "group_name": g.get("GroupName")}
            for g in (raw.get("SecurityGroups") or [])
            if isinstance(g, Mapping)
        ]

        discovered = raw.get("LaunchTime")
        discovered_at = (
            discovered
            if isinstance(discovered, datetime)
            else datetime.now(UTC)
        )
        if isinstance(discovered_at, datetime) and discovered_at.tzinfo is None:
            discovered_at = discovered_at.replace(tzinfo=UTC)

        return NormalizedResource(
            account_id=account_id,
            region=region,
            service=self.service,
            resource_type=self.resource_type,
            resource_id=resource_id,
            tags=tags,
            observed_state=self._base_state(
                account_id=account_id,
                region=region,
                resource_id=resource_id,
                name=name,
                status=state,
                instance_type=raw.get("InstanceType"),
                state=state,
                state_code=(raw.get("State") or {}).get("Code"),
                launch_time=_parse_dt(raw.get("LaunchTime")),
                platform=raw.get("Platform") or raw.get("PlatformDetails"),
                architecture=raw.get("Architecture"),
                vpc_id=vpc_id,
                subnet_id=subnet_id,
                private_ip_address=raw.get("PrivateIpAddress"),
                public_ip_address=raw.get("PublicIpAddress"),
                private_dns_name=raw.get("PrivateDnsName"),
                public_dns_name=raw.get("PublicDnsName"),
                key_name=raw.get("KeyName"),
                iam_instance_profile=(raw.get("IamInstanceProfile") or {}).get("Arn"),
                security_groups=security_groups,
                attached_volumes=attached_volumes,
                network_interfaces=network_interfaces,
                discovery_timestamp=datetime.now(UTC).isoformat(),
            ),
            relationships=relationships,
            discovered_at=discovered_at,
        )


class EbsVolumeCollector(AwsDescribeCollector):
    """Collect EBS volumes and attachment relationships."""

    name = "compute.ebs_volume"
    resource_type = "volume"
    describe_method = "describe_volumes"
    result_key = "Volumes"
    source = "aws.ec2"

    def __init__(
        self,
        session_manager: AWSSessionManager,
        *,
        ec2_service_name: str,
        batch_size: int = 200,
        **kwargs: Any,
    ):
        super().__init__(session_manager, service_name=ec2_service_name, **kwargs)
        self.source = "aws.ec2"
        self._batch_size = batch_size

    def normalize(
        self, raw: Mapping[str, Any], *, account_id: str, region: str
    ) -> NormalizedResource:
        resource_id = str(raw["VolumeId"])
        tags = tags_to_dict(raw.get("Tags"))
        name = tag_name(tags)
        status = raw.get("State")
        attachments: list[dict[str, Any]] = []
        relationships = []
        for attachment in raw.get("Attachments") or []:
            if not isinstance(attachment, Mapping):
                continue
            instance_id = attachment.get("InstanceId")
            attachments.append(
                {
                    "instance_id": instance_id,
                    "device": attachment.get("Device"),
                    "state": attachment.get("State"),
                    "attach_time": _parse_dt(attachment.get("AttachTime")),
                    "delete_on_termination": attachment.get("DeleteOnTermination"),
                }
            )
            relationships.extend(
                compact_edges(
                    edge(
                        relationship_type="attached_to",
                        account_id=account_id,
                        region=region,
                        service=self.service,
                        resource_type="instance",
                        resource_id=instance_id if isinstance(instance_id, str) else None,
                    )
                )
            )

        created = raw.get("CreateTime")
        discovered_at = created if isinstance(created, datetime) else datetime.now(UTC)
        if isinstance(discovered_at, datetime) and discovered_at.tzinfo is None:
            discovered_at = discovered_at.replace(tzinfo=UTC)

        return NormalizedResource(
            account_id=account_id,
            region=region,
            service=self.service,
            resource_type=self.resource_type,
            resource_id=resource_id,
            tags=tags,
            observed_state=self._base_state(
                account_id=account_id,
                region=region,
                resource_id=resource_id,
                name=name,
                status=status,
                state=status,
                size_gib=raw.get("Size"),
                volume_type=raw.get("VolumeType"),
                iops=raw.get("Iops"),
                throughput=raw.get("Throughput"),
                encrypted=raw.get("Encrypted"),
                availability_zone=raw.get("AvailabilityZone"),
                snapshot_id=raw.get("SnapshotId"),
                attachments=attachments,
                create_time=_parse_dt(raw.get("CreateTime")),
                discovery_timestamp=datetime.now(UTC).isoformat(),
            ),
            relationships=relationships,
            discovered_at=discovered_at,
        )

    def describe_volumes_batched(
        self,
        *,
        account_id: str,
        region: str,
        volume_ids: list[str],
    ) -> list[NormalizedResource]:
        """Batched describe for specific volume IDs (e.g. from instance attachments)."""
        if not volume_ids:
            return []
        client = self._get_client(region)
        items: list[NormalizedResource] = []
        for batch in chunked(volume_ids, self._batch_size):
            response = self._invoke(
                client,
                self.describe_method,
                {"VolumeIds": batch},
                region=region,
            )
            for raw in response.get(self.result_key) or []:
                if isinstance(raw, Mapping):
                    items.append(
                        self.normalize(raw, account_id=account_id, region=region)
                    )
        return items


class ElasticIpCollector(AwsDescribeCollector):
    """Collect Elastic IPs / public IPv4 addresses."""

    name = "compute.elastic_ip"
    resource_type = "elastic-ip"
    describe_method = "describe_addresses"
    result_key = "Addresses"
    source = "aws.ec2"

    def __init__(self, session_manager: AWSSessionManager, *, ec2_service_name: str, **kwargs: Any):
        # describe_addresses is not paginated; ignore page_size for the call.
        super().__init__(session_manager, service_name=ec2_service_name, **kwargs)
        self.source = "aws.ec2"

    def fetch_page(
        self,
        *,
        account_id: str,
        region: str,
        next_token: str | None = None,
    ) -> ResourcePage:
        if next_token:
            return ResourcePage(items=[], next_token=None)
        client = self._get_client(region)
        response = self._invoke(client, self.describe_method, {}, region=region)
        items = [
            self.normalize(raw, account_id=account_id, region=region)
            for raw in (response.get(self.result_key) or [])
            if isinstance(raw, Mapping)
        ]
        return ResourcePage(items=items, next_token=None)

    def normalize(
        self, raw: Mapping[str, Any], *, account_id: str, region: str
    ) -> NormalizedResource:
        allocation_id = raw.get("AllocationId")
        public_ip = raw.get("PublicIp")
        resource_id = str(allocation_id or public_ip)
        tags = tags_to_dict(raw.get("Tags"))
        name = tag_name(tags)
        association_id = raw.get("AssociationId")
        instance_id = raw.get("InstanceId")
        eni_id = raw.get("NetworkInterfaceId")
        status = "associated" if association_id else "available"

        relationships = compact_edges(
            edge(
                relationship_type="associated_with",
                account_id=account_id,
                region=region,
                service=self.service,
                resource_type="instance",
                resource_id=instance_id if isinstance(instance_id, str) else None,
            ),
            edge(
                relationship_type="associated_with",
                account_id=account_id,
                region=region,
                service=self.service,
                resource_type="network-interface",
                resource_id=eni_id if isinstance(eni_id, str) else None,
            ),
        )
        return NormalizedResource(
            account_id=account_id,
            region=region,
            service=self.service,
            resource_type=self.resource_type,
            resource_id=resource_id,
            tags=tags,
            observed_state=self._base_state(
                account_id=account_id,
                region=region,
                resource_id=resource_id,
                name=name,
                status=status,
                public_ip=public_ip,
                allocation_id=allocation_id,
                association_id=association_id,
                domain=raw.get("Domain"),
                instance_id=instance_id,
                network_interface_id=eni_id,
                private_ip_address=raw.get("PrivateIpAddress"),
                network_border_group=raw.get("NetworkBorderGroup"),
                public_ipv4_pool=raw.get("PublicIpv4Pool"),
                discovery_timestamp=datetime.now(UTC).isoformat(),
            ),
            relationships=relationships,
        )


def create_compute_collectors(
    session_manager: AWSSessionManager,
    *,
    ec2_service_name: str,
    page_size: int = 100,
    max_retries: int = 5,
) -> list[PaginatedCollector]:
    common = {
        "ec2_service_name": ec2_service_name,
        "page_size": page_size,
        "max_retries": max_retries,
    }
    return [
        Ec2InstanceCollector(session_manager, **common),
        EbsVolumeCollector(session_manager, **common),
        ElasticIpCollector(session_manager, **common),
    ]
