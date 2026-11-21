"""Unit tests for AWS network collectors (mocked Boto3)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from app.collectors.network import (
    EniCollector,
    InternetGatewayCollector,
    NatGatewayCollector,
    NetworkAclCollector,
    RouteTableCollector,
    SecurityGroupCollector,
    SubnetCollector,
    VpcCollector,
    VpcEndpointCollector,
    create_network_collectors,
    tags_to_dict,
)
from app.collectors.normalized import CollectorError
from app.connectors.aws.client import AWSConnector
from app.connectors.aws.session import AWSSessionManager
from app.accounts.models import ProfileAuth


ACCOUNT = "111122223333"
REGION = "test-region-a"
EC2_SERVICE = "ec2"


def _client_error(code: str, message: str = "throttled", status: int = 400) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": message},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "DescribeVpcs",
    )


def _session_manager() -> AWSSessionManager:
    connector = AWSConnector(
        project_alias="demo",
        account_alias="acct",
        account_id=ACCOUNT,
        regions=(REGION,),
        enabled_collectors=("network.vpc",),
        auth=ProfileAuth(type="profile", profile="test-profile"),
    )
    return AWSSessionManager(
        connector,
        sts_service_name="sts",
        session_factory=MagicMock(),
    )


def _vpc_collector(client: MagicMock, **kwargs: Any) -> VpcCollector:
    return VpcCollector(
        _session_manager(),
        ec2_service_name=EC2_SERVICE,
        client_factory=lambda _region: client,
        sleep=lambda _s: None,
        **kwargs,
    )


def test_tags_to_dict() -> None:
    assert tags_to_dict([{"Key": "Name", "Value": "web"}, {"Key": "Env", "Value": "dev"}]) == {
        "Name": "web",
        "Env": "dev",
    }
    assert tags_to_dict(None) == {}


def test_vpc_collector_pagination_and_fields() -> None:
    client = MagicMock()
    client.describe_vpcs.side_effect = [
        {
            "Vpcs": [
                {
                    "VpcId": "vpc-1",
                    "CidrBlock": "10.0.0.0/16",
                    "IsDefault": False,
                    "State": "available",
                    "Tags": [{"Key": "Name", "Value": "main"}],
                    "CidrBlockAssociationSet": [
                        {
                            "AssociationId": "vpc-cidr-assoc-1",
                            "CidrBlock": "10.0.0.0/16",
                            "CidrBlockState": {"State": "associated"},
                        }
                    ],
                }
            ],
            "NextToken": "token-2",
        },
        {
            "Vpcs": [
                {
                    "VpcId": "vpc-2",
                    "CidrBlock": "10.1.0.0/16",
                    "State": "available",
                    "Tags": [],
                }
            ]
        },
    ]
    collector = _vpc_collector(client, page_size=50)

    page1 = collector.fetch_page(account_id=ACCOUNT, region=REGION)
    assert page1.next_token == "token-2"
    assert len(page1.items) == 1
    vpc = page1.items[0]
    assert vpc.resource_id == "vpc-1"
    assert vpc.resource_type == "vpc"
    assert vpc.service == EC2_SERVICE
    assert vpc.account_id == ACCOUNT
    assert vpc.region == REGION
    assert vpc.tags["Name"] == "main"
    assert vpc.observed_state["cidr_block"] == "10.0.0.0/16"
    assert vpc.observed_state["name"] == "main"
    assert vpc.observed_state["account_id"] == ACCOUNT
    assert vpc.observed_state["region"] == REGION

    page2 = collector.fetch_page(
        account_id=ACCOUNT, region=REGION, next_token="token-2"
    )
    assert page2.next_token is None
    assert [i.resource_id for i in page2.items] == ["vpc-2"]

    assert client.describe_vpcs.call_args_list[0].kwargs == {"MaxResults": 50}
    assert client.describe_vpcs.call_args_list[1].kwargs == {
        "MaxResults": 50,
        "NextToken": "token-2",
    }


def test_subnet_belongs_to_vpc_relationship() -> None:
    client = MagicMock()
    client.describe_subnets.return_value = {
        "Subnets": [
            {
                "SubnetId": "subnet-1",
                "VpcId": "vpc-1",
                "CidrBlock": "10.0.1.0/24",
                "AvailabilityZone": "test-region-a-1a",
                "Tags": [{"Key": "Name", "Value": "public-a"}],
            }
        ]
    }
    collector = SubnetCollector(
        _session_manager(),
        ec2_service_name=EC2_SERVICE,
        client_factory=lambda _r: client,
        sleep=lambda _s: None,
    )
    page = collector.fetch_page(account_id=ACCOUNT, region=REGION)
    item = page.items[0]
    assert item.observed_state["cidr_block"] == "10.0.1.0/24"
    assert len(item.relationships) == 1
    rel = item.relationships[0]
    assert rel.relationship_type == "belongs_to"
    assert rel.to_resource_type == "vpc"
    assert rel.to_resource_id == "vpc-1"
    assert rel.to_account_id == ACCOUNT
    assert rel.to_region == REGION


def test_route_table_associations_and_routes() -> None:
    client = MagicMock()
    client.describe_route_tables.return_value = {
        "RouteTables": [
            {
                "RouteTableId": "rtb-1",
                "VpcId": "vpc-1",
                "Associations": [
                    {
                        "RouteTableAssociationId": "rtbassoc-1",
                        "SubnetId": "subnet-1",
                        "Main": False,
                    },
                    {"RouteTableAssociationId": "rtbassoc-main", "Main": True},
                ],
                "Routes": [
                    {
                        "DestinationCidrBlock": "0.0.0.0/0",
                        "GatewayId": "igw-1",
                        "State": "active",
                    }
                ],
                "Tags": [],
            }
        ]
    }
    collector = RouteTableCollector(
        _session_manager(),
        ec2_service_name=EC2_SERVICE,
        client_factory=lambda _r: client,
        sleep=lambda _s: None,
    )
    item = collector.fetch_page(account_id=ACCOUNT, region=REGION).items[0]
    types = {(r.relationship_type, r.to_resource_type, r.to_resource_id) for r in item.relationships}
    assert ("belongs_to", "vpc", "vpc-1") in types
    assert ("associated_with", "subnet", "subnet-1") in types
    # Main association without subnet must not invent an edge.
    assert all(r.to_resource_id for r in item.relationships)
    assert item.observed_state["routes"][0]["destination_cidr_block"] == "0.0.0.0/0"


def test_internet_gateway_attachment() -> None:
    client = MagicMock()
    client.describe_internet_gateways.return_value = {
        "InternetGateways": [
            {
                "InternetGatewayId": "igw-1",
                "Attachments": [{"VpcId": "vpc-1", "State": "available"}],
                "Tags": [{"Key": "Name", "Value": "igw"}],
            }
        ]
    }
    collector = InternetGatewayCollector(
        _session_manager(),
        ec2_service_name=EC2_SERVICE,
        client_factory=lambda _r: client,
        sleep=lambda _s: None,
    )
    item = collector.fetch_page(account_id=ACCOUNT, region=REGION).items[0]
    assert item.relationships[0].relationship_type == "attached_to"
    assert item.relationships[0].to_resource_id == "vpc-1"


def test_nat_gateway_relationships() -> None:
    client = MagicMock()
    client.describe_nat_gateways.return_value = {
        "NatGateways": [
            {
                "NatGatewayId": "nat-1",
                "VpcId": "vpc-1",
                "SubnetId": "subnet-1",
                "State": "available",
                "NatGatewayAddresses": [
                    {
                        "AllocationId": "eipalloc-1",
                        "NetworkInterfaceId": "eni-1",
                        "PrivateIp": "10.0.1.10",
                        "PublicIp": "203.0.113.10",
                    }
                ],
                "Tags": [],
            }
        ]
    }
    collector = NatGatewayCollector(
        _session_manager(),
        ec2_service_name=EC2_SERVICE,
        client_factory=lambda _r: client,
        sleep=lambda _s: None,
    )
    item = collector.fetch_page(account_id=ACCOUNT, region=REGION).items[0]
    targets = {(r.relationship_type, r.to_resource_id) for r in item.relationships}
    assert ("belongs_to", "vpc-1") in targets
    assert ("located_in", "subnet-1") in targets
    assert ("uses", "eni-1") in targets


def test_vpc_endpoint_associations() -> None:
    client = MagicMock()
    client.describe_vpc_endpoints.return_value = {
        "VpcEndpoints": [
            {
                "VpcEndpointId": "vpce-1",
                "VpcId": "vpc-1",
                "ServiceName": "com.amazonaws.test-region-a.s3",
                "VpcEndpointType": "Gateway",
                "State": "available",
                "SubnetIds": ["subnet-1"],
                "RouteTableIds": ["rtb-1"],
                "Groups": [{"GroupId": "sg-1"}],
                "Tags": [],
            }
        ]
    }
    collector = VpcEndpointCollector(
        _session_manager(),
        ec2_service_name=EC2_SERVICE,
        client_factory=lambda _r: client,
        sleep=lambda _s: None,
    )
    item = collector.fetch_page(account_id=ACCOUNT, region=REGION).items[0]
    by_type = {r.to_resource_type: r.to_resource_id for r in item.relationships}
    assert by_type["vpc"] == "vpc-1"
    assert by_type["subnet"] == "subnet-1"
    assert by_type["route-table"] == "rtb-1"
    assert by_type["security-group"] == "sg-1"


def test_security_group_and_network_acl() -> None:
    sg_client = MagicMock()
    sg_client.describe_security_groups.return_value = {
        "SecurityGroups": [
            {
                "GroupId": "sg-1",
                "GroupName": "web",
                "Description": "web sg",
                "VpcId": "vpc-1",
                "IpPermissions": [
                    {
                        "IpProtocol": "tcp",
                        "FromPort": 443,
                        "ToPort": 443,
                        "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                    }
                ],
                "IpPermissionsEgress": [],
                "Tags": [],
            }
        ]
    }
    sg = SecurityGroupCollector(
        _session_manager(),
        ec2_service_name=EC2_SERVICE,
        client_factory=lambda _r: sg_client,
        sleep=lambda _s: None,
    ).fetch_page(account_id=ACCOUNT, region=REGION).items[0]
    assert sg.observed_state["name"] == "web"
    assert sg.relationships[0].to_resource_id == "vpc-1"
    assert sg.observed_state["ip_permissions"][0]["from_port"] == 443

    nacl_client = MagicMock()
    nacl_client.describe_network_acls.return_value = {
        "NetworkAcls": [
            {
                "NetworkAclId": "acl-1",
                "VpcId": "vpc-1",
                "IsDefault": True,
                "Associations": [
                    {"NetworkAclAssociationId": "aclassoc-1", "SubnetId": "subnet-1"}
                ],
                "Entries": [
                    {
                        "RuleNumber": 100,
                        "Protocol": "-1",
                        "RuleAction": "allow",
                        "Egress": False,
                        "CidrBlock": "0.0.0.0/0",
                    }
                ],
                "Tags": [],
            }
        ]
    }
    nacl = NetworkAclCollector(
        _session_manager(),
        ec2_service_name=EC2_SERVICE,
        client_factory=lambda _r: nacl_client,
        sleep=lambda _s: None,
    ).fetch_page(account_id=ACCOUNT, region=REGION).items[0]
    types = {r.relationship_type: r.to_resource_id for r in nacl.relationships}
    assert types["belongs_to"] == "vpc-1"
    assert types["associated_with"] == "subnet-1"
    assert nacl.observed_state["entries"][0]["cidr_block"] == "0.0.0.0/0"


def test_eni_relationships() -> None:
    client = MagicMock()
    client.describe_network_interfaces.return_value = {
        "NetworkInterfaces": [
            {
                "NetworkInterfaceId": "eni-1",
                "VpcId": "vpc-1",
                "SubnetId": "subnet-1",
                "Status": "in-use",
                "PrivateIpAddress": "10.0.1.20",
                "Groups": [{"GroupId": "sg-1", "GroupName": "web"}],
                "Attachment": {
                    "AttachmentId": "eni-attach-1",
                    "InstanceId": "i-1",
                    "Status": "attached",
                },
                "TagSet": [{"Key": "Name", "Value": "web-eni"}],
            }
        ]
    }
    item = EniCollector(
        _session_manager(),
        ec2_service_name=EC2_SERVICE,
        client_factory=lambda _r: client,
        sleep=lambda _s: None,
    ).fetch_page(account_id=ACCOUNT, region=REGION).items[0]
    assert item.tags["Name"] == "web-eni"
    targets = {(r.relationship_type, r.to_resource_id) for r in item.relationships}
    assert ("belongs_to", "vpc-1") in targets
    assert ("located_in", "subnet-1") in targets
    assert ("uses", "sg-1") in targets


def test_throttling_retries_then_succeeds() -> None:
    client = MagicMock()
    client.describe_vpcs.side_effect = [
        _client_error("RequestLimitExceeded"),
        _client_error("Throttling"),
        {"Vpcs": [{"VpcId": "vpc-ok", "CidrBlock": "10.0.0.0/16", "Tags": []}]},
    ]
    sleeps: list[float] = []
    collector = VpcCollector(
        _session_manager(),
        ec2_service_name=EC2_SERVICE,
        client_factory=lambda _r: client,
        max_retries=5,
        sleep=sleeps.append,
    )
    # Force zero jitter by patching rng through call_with_backoff — sleep still called.
    page = collector.fetch_page(account_id=ACCOUNT, region=REGION)
    assert page.items[0].resource_id == "vpc-ok"
    assert client.describe_vpcs.call_count == 3
    assert len(sleeps) == 2


def test_non_retryable_error_raises_collector_error() -> None:
    client = MagicMock()
    client.describe_vpcs.side_effect = _client_error("UnauthorizedOperation", "nope", 403)
    collector = _vpc_collector(client, max_retries=2)
    with pytest.raises(CollectorError) as exc_info:
        collector.fetch_page(account_id=ACCOUNT, region=REGION)
    assert exc_info.value.retryable is False
    assert exc_info.value.error_code == "UnauthorizedOperation"


def test_skips_unverified_empty_relationship_targets() -> None:
    client = MagicMock()
    client.describe_subnets.return_value = {
        "Subnets": [
            {
                "SubnetId": "subnet-orphan",
                "VpcId": "",
                "CidrBlock": "10.0.9.0/24",
                "Tags": [],
            }
        ]
    }
    item = SubnetCollector(
        _session_manager(),
        ec2_service_name=EC2_SERVICE,
        client_factory=lambda _r: client,
        sleep=lambda _s: None,
    ).fetch_page(account_id=ACCOUNT, region=REGION).items[0]
    assert item.relationships == []


def test_create_network_collectors_set() -> None:
    collectors = create_network_collectors(
        _session_manager(), ec2_service_name=EC2_SERVICE
    )
    names = [c.name for c in collectors]
    assert names == [
        "network.vpc",
        "network.subnet",
        "network.route_table",
        "network.internet_gateway",
        "network.nat_gateway",
        "network.vpc_endpoint",
        "network.security_group",
        "network.network_acl",
        "network.eni",
    ]
    assert all(c.service == EC2_SERVICE for c in collectors)
