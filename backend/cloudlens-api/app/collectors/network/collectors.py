"""Concrete AWS network collectors (VPC family)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.collectors.base import PaginatedCollector
from app.collectors.network.common import (
    Ec2NetworkCollector,
    edge,
    tag_name,
    tags_to_dict,
)
from app.collectors.normalized import NormalizedResource, ResourceEdge
from app.connectors.aws.session import AWSSessionManager


class VpcCollector(Ec2NetworkCollector):
    name = "network.vpc"
    resource_type = "vpc"
    describe_method = "describe_vpcs"
    result_key = "Vpcs"

    def normalize(
        self, raw: Mapping[str, Any], *, account_id: str, region: str
    ) -> NormalizedResource:
        resource_id = str(raw["VpcId"])
        tags = tags_to_dict(raw.get("Tags"))
        name = tag_name(tags)
        cidr_associations = [
            {
                "cidr": assoc.get("CidrBlock"),
                "state": (assoc.get("CidrBlockState") or {}).get("State"),
                "association_id": assoc.get("AssociationId"),
            }
            for assoc in raw.get("CidrBlockAssociationSet") or []
        ]
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
                cidr_block=raw.get("CidrBlock"),
                cidr_block_association_set=cidr_associations,
                is_default=raw.get("IsDefault"),
                state=raw.get("State"),
                dhcp_options_id=raw.get("DhcpOptionsId"),
                instance_tenancy=raw.get("InstanceTenancy"),
            ),
        )


class SubnetCollector(Ec2NetworkCollector):
    name = "network.subnet"
    resource_type = "subnet"
    describe_method = "describe_subnets"
    result_key = "Subnets"

    def normalize(
        self, raw: Mapping[str, Any], *, account_id: str, region: str
    ) -> NormalizedResource:
        resource_id = str(raw["SubnetId"])
        tags = tags_to_dict(raw.get("Tags"))
        name = tag_name(tags)
        vpc_id = raw.get("VpcId")
        relationships = _compact(
            edge(
                relationship_type="belongs_to",
                account_id=account_id,
                region=region,
                service=self.service,
                resource_type="vpc",
                resource_id=vpc_id if isinstance(vpc_id, str) else None,
            )
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
                vpc_id=vpc_id,
                cidr_block=raw.get("CidrBlock"),
                availability_zone=raw.get("AvailabilityZone"),
                availability_zone_id=raw.get("AvailabilityZoneId"),
                map_public_ip_on_launch=raw.get("MapPublicIpOnLaunch"),
                state=raw.get("State"),
                available_ip_address_count=raw.get("AvailableIpAddressCount"),
            ),
            relationships=relationships,
        )


class RouteTableCollector(Ec2NetworkCollector):
    name = "network.route_table"
    resource_type = "route-table"
    describe_method = "describe_route_tables"
    result_key = "RouteTables"

    def normalize(
        self, raw: Mapping[str, Any], *, account_id: str, region: str
    ) -> NormalizedResource:
        resource_id = str(raw["RouteTableId"])
        tags = tags_to_dict(raw.get("Tags"))
        name = tag_name(tags)
        vpc_id = raw.get("VpcId")
        relationships: list[ResourceEdge] = _compact(
            edge(
                relationship_type="belongs_to",
                account_id=account_id,
                region=region,
                service=self.service,
                resource_type="vpc",
                resource_id=vpc_id if isinstance(vpc_id, str) else None,
            )
        )
        associations: list[dict[str, Any]] = []
        for assoc in raw.get("Associations") or []:
            associations.append(
                {
                    "association_id": assoc.get("RouteTableAssociationId"),
                    "subnet_id": assoc.get("SubnetId"),
                    "gateway_id": assoc.get("GatewayId"),
                    "main": assoc.get("Main"),
                    "state": (assoc.get("AssociationState") or {}).get("State"),
                }
            )
            relationships.extend(
                _compact(
                    edge(
                        relationship_type="associated_with",
                        account_id=account_id,
                        region=region,
                        service=self.service,
                        resource_type="subnet",
                        resource_id=assoc.get("SubnetId"),
                    )
                )
            )
        routes = [
            {
                "destination_cidr_block": route.get("DestinationCidrBlock"),
                "destination_ipv6_cidr_block": route.get("DestinationIpv6CidrBlock"),
                "gateway_id": route.get("GatewayId"),
                "nat_gateway_id": route.get("NatGatewayId"),
                "transit_gateway_id": route.get("TransitGatewayId"),
                "vpc_endpoint_id": route.get("VpcEndpointId"),
                "egress_only_internet_gateway_id": route.get(
                    "EgressOnlyInternetGatewayId"
                ),
                "network_interface_id": route.get("NetworkInterfaceId"),
                "instance_id": route.get("InstanceId"),
                "state": route.get("State"),
                "origin": route.get("Origin"),
            }
            for route in raw.get("Routes") or []
        ]
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
                vpc_id=vpc_id,
                associations=associations,
                routes=routes,
            ),
            relationships=relationships,
        )


class InternetGatewayCollector(Ec2NetworkCollector):
    name = "network.internet_gateway"
    resource_type = "internet-gateway"
    describe_method = "describe_internet_gateways"
    result_key = "InternetGateways"

    def normalize(
        self, raw: Mapping[str, Any], *, account_id: str, region: str
    ) -> NormalizedResource:
        resource_id = str(raw["InternetGatewayId"])
        tags = tags_to_dict(raw.get("Tags"))
        name = tag_name(tags)
        attachments: list[dict[str, Any]] = []
        relationships: list[ResourceEdge] = []
        for attachment in raw.get("Attachments") or []:
            attachments.append(
                {"vpc_id": attachment.get("VpcId"), "state": attachment.get("State")}
            )
            relationships.extend(
                _compact(
                    edge(
                        relationship_type="attached_to",
                        account_id=account_id,
                        region=region,
                        service=self.service,
                        resource_type="vpc",
                        resource_id=attachment.get("VpcId"),
                    )
                )
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
                attachments=attachments,
            ),
            relationships=relationships,
        )


class NatGatewayCollector(Ec2NetworkCollector):
    name = "network.nat_gateway"
    resource_type = "nat-gateway"
    describe_method = "describe_nat_gateways"
    result_key = "NatGateways"

    def normalize(
        self, raw: Mapping[str, Any], *, account_id: str, region: str
    ) -> NormalizedResource:
        resource_id = str(raw["NatGatewayId"])
        tags = tags_to_dict(raw.get("Tags"))
        name = tag_name(tags)
        vpc_id = raw.get("VpcId")
        subnet_id = raw.get("SubnetId")
        relationships = _compact(
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
        addresses = [
            {
                "allocation_id": addr.get("AllocationId"),
                "network_interface_id": addr.get("NetworkInterfaceId"),
                "private_ip": addr.get("PrivateIp"),
                "public_ip": addr.get("PublicIp"),
            }
            for addr in raw.get("NatGatewayAddresses") or []
        ]
        for addr in addresses:
            relationships.extend(
                _compact(
                    edge(
                        relationship_type="uses",
                        account_id=account_id,
                        region=region,
                        service=self.service,
                        resource_type="network-interface",
                        resource_id=addr.get("network_interface_id"),
                    )
                )
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
                vpc_id=vpc_id,
                subnet_id=subnet_id,
                state=raw.get("State"),
                connectivity_type=raw.get("ConnectivityType"),
                addresses=addresses,
            ),
            relationships=relationships,
        )


class VpcEndpointCollector(Ec2NetworkCollector):
    name = "network.vpc_endpoint"
    resource_type = "vpc-endpoint"
    describe_method = "describe_vpc_endpoints"
    result_key = "VpcEndpoints"

    def normalize(
        self, raw: Mapping[str, Any], *, account_id: str, region: str
    ) -> NormalizedResource:
        resource_id = str(raw["VpcEndpointId"])
        tags = tags_to_dict(raw.get("Tags"))
        name = tag_name(tags)
        vpc_id = raw.get("VpcId")
        relationships = _compact(
            edge(
                relationship_type="belongs_to",
                account_id=account_id,
                region=region,
                service=self.service,
                resource_type="vpc",
                resource_id=vpc_id if isinstance(vpc_id, str) else None,
            )
        )
        subnet_ids = [str(s) for s in (raw.get("SubnetIds") or []) if s]
        for subnet_id in subnet_ids:
            relationships.extend(
                _compact(
                    edge(
                        relationship_type="associated_with",
                        account_id=account_id,
                        region=region,
                        service=self.service,
                        resource_type="subnet",
                        resource_id=subnet_id,
                    )
                )
            )
        route_table_ids = [str(r) for r in (raw.get("RouteTableIds") or []) if r]
        for route_table_id in route_table_ids:
            relationships.extend(
                _compact(
                    edge(
                        relationship_type="associated_with",
                        account_id=account_id,
                        region=region,
                        service=self.service,
                        resource_type="route-table",
                        resource_id=route_table_id,
                    )
                )
            )
        group_ids = [
            str(g.get("GroupId"))
            for g in (raw.get("Groups") or [])
            if isinstance(g, Mapping) and g.get("GroupId")
        ]
        for group_id in group_ids:
            relationships.extend(
                _compact(
                    edge(
                        relationship_type="uses",
                        account_id=account_id,
                        region=region,
                        service=self.service,
                        resource_type="security-group",
                        resource_id=group_id,
                    )
                )
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
                vpc_id=vpc_id,
                service_name=raw.get("ServiceName"),
                vpc_endpoint_type=raw.get("VpcEndpointType"),
                state=raw.get("State"),
                subnet_ids=subnet_ids,
                route_table_ids=route_table_ids,
                security_group_ids=group_ids,
                private_dns_enabled=raw.get("PrivateDnsEnabled"),
            ),
            relationships=relationships,
        )


class SecurityGroupCollector(Ec2NetworkCollector):
    name = "network.security_group"
    resource_type = "security-group"
    describe_method = "describe_security_groups"
    result_key = "SecurityGroups"

    def normalize(
        self, raw: Mapping[str, Any], *, account_id: str, region: str
    ) -> NormalizedResource:
        resource_id = str(raw["GroupId"])
        tags = tags_to_dict(raw.get("Tags"))
        name = tag_name(tags) or (
            str(raw["GroupName"]) if raw.get("GroupName") else None
        )
        vpc_id = raw.get("VpcId")
        relationships = _compact(
            edge(
                relationship_type="belongs_to",
                account_id=account_id,
                region=region,
                service=self.service,
                resource_type="vpc",
                resource_id=vpc_id if isinstance(vpc_id, str) else None,
            )
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
                group_name=raw.get("GroupName"),
                description=raw.get("Description"),
                vpc_id=vpc_id,
                ip_permissions=_sanitize_permissions(raw.get("IpPermissions")),
                ip_permissions_egress=_sanitize_permissions(
                    raw.get("IpPermissionsEgress")
                ),
            ),
            relationships=relationships,
        )


class NetworkAclCollector(Ec2NetworkCollector):
    name = "network.network_acl"
    resource_type = "network-acl"
    describe_method = "describe_network_acls"
    result_key = "NetworkAcls"

    def normalize(
        self, raw: Mapping[str, Any], *, account_id: str, region: str
    ) -> NormalizedResource:
        resource_id = str(raw["NetworkAclId"])
        tags = tags_to_dict(raw.get("Tags"))
        name = tag_name(tags)
        vpc_id = raw.get("VpcId")
        relationships = _compact(
            edge(
                relationship_type="belongs_to",
                account_id=account_id,
                region=region,
                service=self.service,
                resource_type="vpc",
                resource_id=vpc_id if isinstance(vpc_id, str) else None,
            )
        )
        associations: list[dict[str, Any]] = []
        for assoc in raw.get("Associations") or []:
            associations.append(
                {
                    "network_acl_association_id": assoc.get("NetworkAclAssociationId"),
                    "subnet_id": assoc.get("SubnetId"),
                }
            )
            relationships.extend(
                _compact(
                    edge(
                        relationship_type="associated_with",
                        account_id=account_id,
                        region=region,
                        service=self.service,
                        resource_type="subnet",
                        resource_id=assoc.get("SubnetId"),
                    )
                )
            )
        entries = [
            {
                "rule_number": entry.get("RuleNumber"),
                "protocol": entry.get("Protocol"),
                "rule_action": entry.get("RuleAction"),
                "egress": entry.get("Egress"),
                "cidr_block": entry.get("CidrBlock"),
                "ipv6_cidr_block": entry.get("Ipv6CidrBlock"),
            }
            for entry in raw.get("Entries") or []
        ]
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
                vpc_id=vpc_id,
                is_default=raw.get("IsDefault"),
                associations=associations,
                entries=entries,
            ),
            relationships=relationships,
        )


class EniCollector(Ec2NetworkCollector):
    name = "network.eni"
    resource_type = "network-interface"
    describe_method = "describe_network_interfaces"
    result_key = "NetworkInterfaces"

    def normalize(
        self, raw: Mapping[str, Any], *, account_id: str, region: str
    ) -> NormalizedResource:
        resource_id = str(raw["NetworkInterfaceId"])
        tags = tags_to_dict(raw.get("TagSet") or raw.get("Tags"))
        name = tag_name(tags)
        vpc_id = raw.get("VpcId")
        subnet_id = raw.get("SubnetId")
        relationships = _compact(
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
        group_ids: list[str] = []
        for group in raw.get("Groups") or []:
            if isinstance(group, Mapping) and group.get("GroupId"):
                group_id = str(group["GroupId"])
                group_ids.append(group_id)
                relationships.extend(
                    _compact(
                        edge(
                            relationship_type="uses",
                            account_id=account_id,
                            region=region,
                            service=self.service,
                            resource_type="security-group",
                            resource_id=group_id,
                        )
                    )
                )
        private_ips = [
            {
                "private_ip": ip.get("PrivateIpAddress"),
                "primary": ip.get("Primary"),
                "association_public_ip": (ip.get("Association") or {}).get("PublicIp"),
            }
            for ip in raw.get("PrivateIpAddresses") or []
        ]
        attachment = raw.get("Attachment") or {}
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
                vpc_id=vpc_id,
                subnet_id=subnet_id,
                availability_zone=raw.get("AvailabilityZone"),
                description=raw.get("Description"),
                status=raw.get("Status"),
                interface_type=raw.get("InterfaceType"),
                mac_address=raw.get("MacAddress"),
                private_ip_address=raw.get("PrivateIpAddress"),
                private_ip_addresses=private_ips,
                security_group_ids=group_ids,
                attachment={
                    "attachment_id": attachment.get("AttachmentId"),
                    "instance_id": attachment.get("InstanceId"),
                    "device_index": attachment.get("DeviceIndex"),
                    "status": attachment.get("Status"),
                },
            ),
            relationships=relationships,
        )


def create_network_collectors(
    session_manager: AWSSessionManager,
    *,
    ec2_service_name: str,
    page_size: int = 100,
    max_retries: int = 5,
) -> list[PaginatedCollector]:
    """Instantiate the full set of VPC-family network collectors."""
    common = {
        "ec2_service_name": ec2_service_name,
        "page_size": page_size,
        "max_retries": max_retries,
    }
    return [
        VpcCollector(session_manager, **common),
        SubnetCollector(session_manager, **common),
        RouteTableCollector(session_manager, **common),
        InternetGatewayCollector(session_manager, **common),
        NatGatewayCollector(session_manager, **common),
        VpcEndpointCollector(session_manager, **common),
        SecurityGroupCollector(session_manager, **common),
        NetworkAclCollector(session_manager, **common),
        EniCollector(session_manager, **common),
    ]


def _compact(*edges: ResourceEdge | None) -> list[ResourceEdge]:
    return [edge for edge in edges if edge is not None]


def _sanitize_permissions(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    cleaned: list[dict[str, Any]] = []
    for perm in raw:
        if not isinstance(perm, Mapping):
            continue
        cleaned.append(
            {
                "ip_protocol": perm.get("IpProtocol"),
                "from_port": perm.get("FromPort"),
                "to_port": perm.get("ToPort"),
                "ip_ranges": [
                    {"cidr_ip": r.get("CidrIp"), "description": r.get("Description")}
                    for r in (perm.get("IpRanges") or [])
                    if isinstance(r, Mapping)
                ],
                "ipv6_ranges": [
                    {
                        "cidr_ipv6": r.get("CidrIpv6"),
                        "description": r.get("Description"),
                    }
                    for r in (perm.get("Ipv6Ranges") or [])
                    if isinstance(r, Mapping)
                ],
                "user_id_group_pairs": [
                    {
                        "group_id": r.get("GroupId"),
                        "user_id": r.get("UserId"),
                        "description": r.get("Description"),
                    }
                    for r in (perm.get("UserIdGroupPairs") or [])
                    if isinstance(r, Mapping)
                ],
            }
        )
    return cleaned
