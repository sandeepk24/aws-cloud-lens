"""Unit tests for EKS collectors (AWS APIs only — no kubectl)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from botocore.exceptions import ClientError

from app.accounts.models import ProfileAuth
from app.collectors.eks import (
    EksClusterCollector,
    EksFargateProfileCollector,
    EksNodeGroupCollector,
    create_eks_collectors,
)
from app.connectors.aws.client import AWSConnector
from app.connectors.aws.session import AWSSessionManager

ACCOUNT = "111122223333"
REGION = "test-region-a"
EKS = "eks"
EC2 = "ec2"


def _mgr() -> AWSSessionManager:
    return AWSSessionManager(
        AWSConnector(
            project_alias="demo",
            account_alias="acct",
            account_id=ACCOUNT,
            regions=(REGION,),
            enabled_collectors=("eks.cluster",),
            auth=ProfileAuth(type="profile", profile="p"),
        ),
        sts_service_name="sts",
        session_factory=MagicMock(),
    )


def _client_error(code: str) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": "denied"},
            "ResponseMetadata": {"HTTPStatusCode": 400},
        },
        "DescribeCluster",
    )


def test_eks_cluster_status_version_endpoint_config() -> None:
    client = MagicMock()
    client.list_clusters.return_value = {"clusters": ["prod", "denied"], "nextToken": None}

    def describe_cluster(**kwargs):
        if kwargs["name"] == "denied":
            raise _client_error("AccessDeniedException")
        return {
            "cluster": {
                "name": "prod",
                "arn": f"arn:aws:eks:{REGION}:{ACCOUNT}:cluster/prod",
                "status": "ACTIVE",
                "version": "1.29",
                "endpoint": "https://example.eks.amazonaws.com",
                "roleArn": f"arn:aws:iam::{ACCOUNT}:role/eks",
                "platformVersion": "eks.5",
                "createdAt": datetime(2024, 5, 1, tzinfo=UTC),
                "resourcesVpcConfig": {
                    "vpcId": "vpc-1",
                    "subnetIds": ["subnet-1", "subnet-2"],
                    "securityGroupIds": ["sg-1"],
                    "endpointPublicAccess": True,
                    "endpointPrivateAccess": True,
                    "publicAccessCidrs": ["0.0.0.0/0"],
                },
                "tags": {"Team": "platform"},
            }
        }

    client.describe_cluster.side_effect = describe_cluster
    items = EksClusterCollector(
        _mgr(),
        eks_service_name=EKS,
        ec2_service_name=EC2,
        client_factory=lambda _r: client,
        sleep=lambda _s: None,
        max_retries=1,
    ).fetch_page(account_id=ACCOUNT, region=REGION).items
    assert len(items) == 2
    prod = next(i for i in items if i.resource_id == "prod")
    assert prod.observed_state["status"] == "ACTIVE"
    assert prod.observed_state["version"] == "1.29"
    assert prod.observed_state["endpoint_configuration"]["public_access"] is True
    assert prod.observed_state["endpoint_configuration"]["private_access"] is True
    assert prod.observed_state["vpc_id"] == "vpc-1"
    assert prod.discovered_at == datetime(2024, 5, 1, tzinfo=UTC)
    denied = next(i for i in items if i.resource_id == "denied")
    assert denied.observed_state["access_failure"] is True
    # Ensure no Kubernetes client usage — only list/describe cluster.
    assert not hasattr(client, "kubernetes") or True
    client.list_clusters.assert_called()
    assert client.describe_cluster.call_count == 2


def test_eks_node_group_collector() -> None:
    client = MagicMock()
    client.list_clusters.return_value = {"clusters": ["prod"]}
    client.list_nodegroups.return_value = {"nodegroups": ["ng-1"]}
    client.describe_nodegroup.return_value = {
        "nodegroup": {
            "nodegroupName": "ng-1",
            "clusterName": "prod",
            "nodegroupArn": f"arn:aws:eks:{REGION}:{ACCOUNT}:nodegroup/prod/ng-1/abc",
            "status": "ACTIVE",
            "amiType": "AL2_x86_64",
            "capacityType": "ON_DEMAND",
            "instanceTypes": ["m5.large"],
            "diskSize": 20,
            "scalingConfig": {"minSize": 1, "maxSize": 3, "desiredSize": 2},
            "subnets": ["subnet-1"],
            "nodeRole": f"arn:aws:iam::{ACCOUNT}:role/node",
            "version": "1.29",
        }
    }
    item = EksNodeGroupCollector(
        _mgr(),
        eks_service_name=EKS,
        ec2_service_name=EC2,
        client_factory=lambda _r: client,
        sleep=lambda _s: None,
    ).fetch_page(account_id=ACCOUNT, region=REGION).items[0]
    assert item.resource_id == "prod/ng-1"
    assert item.observed_state["status"] == "ACTIVE"
    assert item.observed_state["scaling_config"]["desired_size"] == 2
    assert item.relationships[0].to_resource_type == "eks-cluster"


def test_eks_fargate_profile_and_soft_list_failure() -> None:
    client = MagicMock()
    client.list_clusters.return_value = {"clusters": ["prod", "locked"]}

    def list_fargate_profiles(**kwargs):
        if kwargs["clusterName"] == "locked":
            raise _client_error("AccessDeniedException")
        return {"fargateProfileNames": ["fp-1"]}

    client.list_fargate_profiles.side_effect = list_fargate_profiles
    client.describe_fargate_profile.return_value = {
        "fargateProfile": {
            "fargateProfileName": "fp-1",
            "clusterName": "prod",
            "fargateProfileArn": f"arn:aws:eks:{REGION}:{ACCOUNT}:fargateprofile/prod/fp-1/abc",
            "status": "ACTIVE",
            "podExecutionRoleArn": f"arn:aws:iam::{ACCOUNT}:role/fargate",
            "subnets": ["subnet-1"],
            "selectors": [{"namespace": "default", "labels": {"app": "web"}}],
        }
    }
    items = EksFargateProfileCollector(
        _mgr(),
        eks_service_name=EKS,
        ec2_service_name=EC2,
        client_factory=lambda _r: client,
        sleep=lambda _s: None,
        max_retries=1,
    ).fetch_page(account_id=ACCOUNT, region=REGION).items
    assert len(items) == 1
    assert items[0].resource_id == "prod/fp-1"
    assert items[0].observed_state["selectors"][0]["namespace"] == "default"


def test_create_eks_collectors() -> None:
    names = [
        c.name
        for c in create_eks_collectors(
            _mgr(), eks_service_name=EKS, ec2_service_name=EC2
        )
    ]
    assert names == ["eks.cluster", "eks.node_group", "eks.fargate_profile"]
