"""Unit tests for EC2 / EBS / Elastic IP collectors."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from app.accounts.models import ProfileAuth
from app.collectors.compute import (
    EbsVolumeCollector,
    Ec2InstanceCollector,
    ElasticIpCollector,
    create_compute_collectors,
)
from app.connectors.aws.client import AWSConnector
from app.connectors.aws.session import AWSSessionManager

ACCOUNT = "111122223333"
REGION = "test-region-a"
EC2 = "ec2"


def _mgr() -> AWSSessionManager:
    return AWSSessionManager(
        AWSConnector(
            project_alias="demo",
            account_alias="acct",
            account_id=ACCOUNT,
            regions=(REGION,),
            enabled_collectors=("compute.ec2_instance",),
            auth=ProfileAuth(type="profile", profile="p"),
        ),
        sts_service_name="sts",
        session_factory=MagicMock(),
    )


def test_ec2_instance_pagination_state_volumes_networking() -> None:
    client = MagicMock()
    client.describe_instances.side_effect = [
        {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-1",
                            "InstanceType": "t3.micro",
                            "State": {"Name": "running", "Code": 16},
                            "LaunchTime": datetime(2024, 6, 1, tzinfo=UTC),
                            "VpcId": "vpc-1",
                            "SubnetId": "subnet-1",
                            "PrivateIpAddress": "10.0.1.10",
                            "PublicIpAddress": "203.0.113.10",
                            "SecurityGroups": [{"GroupId": "sg-1", "GroupName": "web"}],
                            "BlockDeviceMappings": [
                                {
                                    "DeviceName": "/dev/xvda",
                                    "Ebs": {
                                        "VolumeId": "vol-1",
                                        "Status": "attached",
                                        "AttachTime": datetime(2024, 6, 1, tzinfo=UTC),
                                        "DeleteOnTermination": True,
                                    },
                                }
                            ],
                            "NetworkInterfaces": [
                                {
                                    "NetworkInterfaceId": "eni-1",
                                    "SubnetId": "subnet-1",
                                    "VpcId": "vpc-1",
                                    "PrivateIpAddress": "10.0.1.10",
                                    "Association": {"PublicIp": "203.0.113.10"},
                                    "Groups": [{"GroupId": "sg-1"}],
                                }
                            ],
                            "Tags": [{"Key": "Name", "Value": "web-1"}],
                        }
                    ]
                }
            ],
            "NextToken": "t2",
        },
        {"Reservations": []},
    ]
    collector = Ec2InstanceCollector(
        _mgr(),
        ec2_service_name=EC2,
        client_factory=lambda _r: client,
        sleep=lambda _s: None,
    )
    page = collector.fetch_page(account_id=ACCOUNT, region=REGION)
    assert page.next_token == "t2"
    item = page.items[0]
    assert item.resource_id == "i-1"
    assert item.observed_state["status"] == "running"
    assert item.observed_state["instance_type"] == "t3.micro"
    assert item.observed_state["attached_volumes"][0]["volume_id"] == "vol-1"
    assert item.observed_state["network_interfaces"][0]["network_interface_id"] == "eni-1"
    assert item.discovered_at == datetime(2024, 6, 1, tzinfo=UTC)
    assert "discovery_timestamp" in item.observed_state
    rels = {(r.relationship_type, r.to_resource_type, r.to_resource_id) for r in item.relationships}
    assert ("belongs_to", "vpc", "vpc-1") in rels
    assert ("located_in", "subnet", "subnet-1") in rels
    assert ("attached_to", "volume", "vol-1") in rels
    assert ("uses", "network-interface", "eni-1") in rels


def test_ebs_volume_and_batched_describe() -> None:
    client = MagicMock()
    client.describe_volumes.side_effect = [
        {
            "Volumes": [
                {
                    "VolumeId": "vol-1",
                    "Size": 20,
                    "VolumeType": "gp3",
                    "State": "in-use",
                    "AvailabilityZone": "test-region-a-1a",
                    "CreateTime": datetime(2024, 1, 1, tzinfo=UTC),
                    "Attachments": [
                        {
                            "InstanceId": "i-1",
                            "Device": "/dev/xvda",
                            "State": "attached",
                        }
                    ],
                    "Tags": [{"Key": "Name", "Value": "root"}],
                }
            ],
            "NextToken": None,
        },
        {
            "Volumes": [
                {
                    "VolumeId": "vol-2",
                    "Size": 100,
                    "State": "available",
                    "CreateTime": datetime(2024, 2, 1, tzinfo=UTC),
                    "Attachments": [],
                    "Tags": [],
                }
            ]
        },
    ]
    collector = EbsVolumeCollector(
        _mgr(),
        ec2_service_name=EC2,
        client_factory=lambda _r: client,
        sleep=lambda _s: None,
        batch_size=1,
    )
    page = collector.fetch_page(account_id=ACCOUNT, region=REGION)
    item = page.items[0]
    assert item.observed_state["status"] == "in-use"
    assert item.relationships[0].to_resource_id == "i-1"
    assert item.discovered_at == datetime(2024, 1, 1, tzinfo=UTC)

    batched = collector.describe_volumes_batched(
        account_id=ACCOUNT, region=REGION, volume_ids=["vol-2"]
    )
    assert batched[0].resource_id == "vol-2"
    assert client.describe_volumes.call_args_list[-1].kwargs == {"VolumeIds": ["vol-2"]}


def test_elastic_ip_collector() -> None:
    client = MagicMock()
    client.describe_addresses.return_value = {
        "Addresses": [
            {
                "AllocationId": "eipalloc-1",
                "PublicIp": "203.0.113.5",
                "Domain": "vpc",
                "AssociationId": "eipassoc-1",
                "InstanceId": "i-1",
                "NetworkInterfaceId": "eni-1",
                "Tags": [{"Key": "Name", "Value": "public"}],
            }
        ]
    }
    collector = ElasticIpCollector(
        _mgr(),
        ec2_service_name=EC2,
        client_factory=lambda _r: client,
        sleep=lambda _s: None,
    )
    page = collector.fetch_page(account_id=ACCOUNT, region=REGION)
    item = page.items[0]
    assert item.resource_id == "eipalloc-1"
    assert item.observed_state["status"] == "associated"
    assert item.observed_state["public_ip"] == "203.0.113.5"
    assert page.next_token is None
    # Second page token yields empty (non-paginated API).
    assert collector.fetch_page(
        account_id=ACCOUNT, region=REGION, next_token="x"
    ).items == []


def test_create_compute_collectors() -> None:
    names = [c.name for c in create_compute_collectors(_mgr(), ec2_service_name=EC2)]
    assert names == [
        "compute.ec2_instance",
        "compute.ebs_volume",
        "compute.elastic_ip",
    ]
