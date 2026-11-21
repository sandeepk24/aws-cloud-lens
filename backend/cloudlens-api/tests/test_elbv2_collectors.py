"""Unit tests for ELBv2 collectors."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from app.accounts.models import ProfileAuth
from app.collectors.elbv2 import (
    ListenerCollector,
    LoadBalancerCollector,
    TargetGroupCollector,
    TargetHealthCollector,
    create_elbv2_collectors,
)
from app.connectors.aws.client import AWSConnector
from app.connectors.aws.session import AWSSessionManager

ACCOUNT = "111122223333"
REGION = "test-region-a"
ELB = "elbv2"
EC2 = "ec2"
LB_ARN = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:loadbalancer/app/web/abc"
TG_ARN = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:targetgroup/web/xyz"
LISTENER_ARN = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:listener/app/web/abc/111"


def _mgr() -> AWSSessionManager:
    return AWSSessionManager(
        AWSConnector(
            project_alias="demo",
            account_alias="acct",
            account_id=ACCOUNT,
            regions=(REGION,),
            enabled_collectors=("elbv2.load_balancer",),
            auth=ProfileAuth(type="profile", profile="p"),
        ),
        sts_service_name="sts",
        session_factory=MagicMock(),
    )


def test_load_balancer_scheme_and_vpc_mapping() -> None:
    client = MagicMock()
    client.describe_load_balancers.return_value = {
        "LoadBalancers": [
            {
                "LoadBalancerArn": LB_ARN,
                "LoadBalancerName": "web",
                "DNSName": "web.example",
                "Scheme": "internet-facing",
                "Type": "application",
                "VpcId": "vpc-1",
                "State": {"Code": "active"},
                "CreatedTime": datetime(2024, 3, 1, tzinfo=UTC),
                "AvailabilityZones": [
                    {"ZoneName": f"{REGION}a", "SubnetId": "subnet-1"}
                ],
                "SecurityGroups": ["sg-1"],
            }
        ]
    }
    item = LoadBalancerCollector(
        _mgr(),
        elbv2_service_name=ELB,
        ec2_service_name=EC2,
        client_factory=lambda _r: client,
        sleep=lambda _s: None,
    ).fetch_page(account_id=ACCOUNT, region=REGION).items[0]
    assert item.arn == LB_ARN
    assert item.observed_state["scheme"] == "internet-facing"
    assert item.observed_state["type"] == "application"
    assert item.observed_state["status"] == "active"
    assert item.observed_state["vpc_id"] == "vpc-1"
    assert item.discovered_at == datetime(2024, 3, 1, tzinfo=UTC)
    services = {r.to_service for r in item.relationships}
    assert EC2 in services


def test_listener_batched_per_load_balancer_page() -> None:
    client = MagicMock()
    client.describe_load_balancers.return_value = {
        "LoadBalancers": [{"LoadBalancerArn": LB_ARN}],
        "NextMarker": None,
    }
    client.describe_listeners.return_value = {
        "Listeners": [
            {
                "ListenerArn": LISTENER_ARN,
                "LoadBalancerArn": LB_ARN,
                "Port": 443,
                "Protocol": "HTTPS",
                "DefaultActions": [
                    {"Type": "forward", "TargetGroupArn": TG_ARN}
                ],
            }
        ]
    }
    item = ListenerCollector(
        _mgr(),
        elbv2_service_name=ELB,
        ec2_service_name=EC2,
        client_factory=lambda _r: client,
        sleep=lambda _s: None,
    ).fetch_page(account_id=ACCOUNT, region=REGION).items[0]
    assert item.observed_state["port"] == 443
    assert item.relationships[0].to_resource_id == LB_ARN
    assert any(r.to_resource_id == TG_ARN for r in item.relationships)
    client.describe_listeners.assert_called_with(LoadBalancerArn=LB_ARN)


def test_target_group_and_health() -> None:
    client = MagicMock()
    client.describe_target_groups.return_value = {
        "TargetGroups": [
            {
                "TargetGroupArn": TG_ARN,
                "TargetGroupName": "web",
                "Protocol": "HTTP",
                "Port": 80,
                "VpcId": "vpc-1",
                "TargetType": "instance",
                "HealthCheckPath": "/health",
                "LoadBalancerArns": [LB_ARN],
            }
        ]
    }
    tg = TargetGroupCollector(
        _mgr(),
        elbv2_service_name=ELB,
        ec2_service_name=EC2,
        client_factory=lambda _r: client,
        sleep=lambda _s: None,
    ).fetch_page(account_id=ACCOUNT, region=REGION).items[0]
    assert tg.observed_state["vpc_id"] == "vpc-1"
    assert tg.observed_state["health_check_path"] == "/health"

    client.describe_target_health.return_value = {
        "TargetHealthDescriptions": [
            {
                "Target": {"Id": "i-1", "Port": 80},
                "TargetHealth": {
                    "State": "healthy",
                    "Reason": None,
                    "Description": "ok",
                },
            }
        ]
    }
    health = TargetHealthCollector(
        _mgr(),
        elbv2_service_name=ELB,
        ec2_service_name=EC2,
        client_factory=lambda _r: client,
        sleep=lambda _s: None,
    ).fetch_page(account_id=ACCOUNT, region=REGION).items[0]
    assert health.observed_state["status"] == "healthy"
    assert health.observed_state["target_id"] == "i-1"
    assert any(
        r.relationship_type == "targets" and r.to_resource_id == "i-1"
        for r in health.relationships
    )


def test_create_elbv2_collectors() -> None:
    names = [
        c.name
        for c in create_elbv2_collectors(
            _mgr(), elbv2_service_name=ELB, ec2_service_name=EC2
        )
    ]
    assert names == [
        "elbv2.load_balancer",
        "elbv2.listener",
        "elbv2.target_group",
        "elbv2.target_health",
    ]
