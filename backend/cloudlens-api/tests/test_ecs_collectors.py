"""Unit tests for ECS collectors."""

from __future__ import annotations

from unittest.mock import MagicMock

from botocore.exceptions import ClientError

from app.accounts.models import ProfileAuth
from app.collectors.ecs import (
    EcsClusterCollector,
    EcsServiceCollector,
    EcsTaskDefinitionCollector,
    create_ecs_collectors,
)
from app.connectors.aws.client import AWSConnector
from app.connectors.aws.session import AWSSessionManager

ACCOUNT = "111122223333"
REGION = "test-region-a"
ECS = "ecs"
CLUSTER_ARN = f"arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/app"
SERVICE_ARN = f"arn:aws:ecs:{REGION}:{ACCOUNT}:service/app/web"
TD_ARN = f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/web:3"
TG_ARN = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:targetgroup/web/abc"


def _mgr() -> AWSSessionManager:
    return AWSSessionManager(
        AWSConnector(
            project_alias="demo",
            account_alias="acct",
            account_id=ACCOUNT,
            regions=(REGION,),
            enabled_collectors=("ecs.cluster",),
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
        "DescribeClusters",
    )


def test_ecs_cluster_pagination_and_batch_describe() -> None:
    client = MagicMock()
    client.list_clusters.return_value = {
        "clusterArns": [CLUSTER_ARN, f"{CLUSTER_ARN}-b"],
        "nextToken": "t2",
    }
    client.describe_clusters.return_value = {
        "clusters": [
            {
                "clusterArn": CLUSTER_ARN,
                "clusterName": "app",
                "status": "ACTIVE",
                "activeServicesCount": 2,
                "runningTasksCount": 5,
                "pendingTasksCount": 1,
                "capacityProviders": ["FARGATE", "FARGATE_SPOT"],
                "defaultCapacityProviderStrategy": [
                    {"capacityProvider": "FARGATE", "weight": 1, "base": 0}
                ],
                "tags": [{"key": "Env", "value": "dev"}],
            }
        ],
        "failures": [
            {"arn": f"{CLUSTER_ARN}-b", "reason": "MISSING"}
        ],
    }
    collector = EcsClusterCollector(
        _mgr(),
        ecs_service_name=ECS,
        client_factory=lambda _r: client,
        sleep=lambda _s: None,
        describe_batch_size=100,
    )
    page = collector.fetch_page(account_id=ACCOUNT, region=REGION)
    assert page.next_token == "t2"
    by_id = {i.resource_id: i for i in page.items}
    assert by_id[CLUSTER_ARN].observed_state["status"] == "ACTIVE"
    assert by_id[CLUSTER_ARN].observed_state["running_tasks_count"] == 5
    assert by_id[CLUSTER_ARN].observed_state["capacity_providers"] == [
        "FARGATE",
        "FARGATE_SPOT",
    ]
    assert by_id[f"{CLUSTER_ARN}-b"].observed_state["access_failure"] is True
    client.describe_clusters.assert_called()
    assert len(client.describe_clusters.call_args.kwargs["clusters"]) == 2


def test_ecs_service_launch_type_networking_target_groups() -> None:
    client = MagicMock()
    client.list_clusters.return_value = {"clusterArns": [CLUSTER_ARN]}
    client.list_services.return_value = {"serviceArns": [SERVICE_ARN]}
    client.describe_services.return_value = {
        "services": [
            {
                "serviceArn": SERVICE_ARN,
                "serviceName": "web",
                "clusterArn": CLUSTER_ARN,
                "status": "ACTIVE",
                "taskDefinition": TD_ARN,
                "launchType": "FARGATE",
                "desiredCount": 3,
                "runningCount": 2,
                "pendingCount": 1,
                "capacityProviderStrategy": [
                    {"capacityProvider": "FARGATE", "weight": 1}
                ],
                "networkConfiguration": {
                    "awsvpcConfiguration": {
                        "subnets": ["subnet-1"],
                        "securityGroups": ["sg-1"],
                        "assignPublicIp": "DISABLED",
                    }
                },
                "loadBalancers": [
                    {
                        "targetGroupArn": TG_ARN,
                        "containerName": "web",
                        "containerPort": 8080,
                    }
                ],
                "tags": [],
            }
        ],
        "failures": [],
    }
    item = EcsServiceCollector(
        _mgr(),
        ecs_service_name=ECS,
        elbv2_service_name="elbv2",
        ec2_service_name="ec2",
        client_factory=lambda _r: client,
        sleep=lambda _s: None,
    ).fetch_page(account_id=ACCOUNT, region=REGION).items[0]
    assert item.observed_state["launch_type"] == "FARGATE"
    assert item.observed_state["desired_count"] == 3
    assert item.observed_state["running_count"] == 2
    assert item.observed_state["pending_count"] == 1
    assert item.observed_state["network_configuration"]["subnets"] == ["subnet-1"]
    rels = {(r.relationship_type, r.to_resource_type) for r in item.relationships}
    assert ("belongs_to", "ecs-cluster") in rels
    assert ("uses", "ecs-task-definition") in rels
    assert ("associated_with", "subnet") in rels
    assert ("forwards_to", "target-group") in rels
    # Batch limit: describe_services called with at most 10.
    assert len(client.describe_services.call_args.kwargs["services"]) <= 10


def test_ecs_service_access_denied_on_one_cluster_continues() -> None:
    client = MagicMock()
    client.list_clusters.return_value = {
        "clusterArns": [CLUSTER_ARN, f"{CLUSTER_ARN}-2"]
    }

    def list_services(**kwargs):
        if kwargs["cluster"] == CLUSTER_ARN:
            raise _client_error("AccessDeniedException")
        return {"serviceArns": [SERVICE_ARN]}

    client.list_services.side_effect = list_services
    client.describe_services.return_value = {
        "services": [
            {
                "serviceArn": SERVICE_ARN,
                "serviceName": "web",
                "status": "ACTIVE",
                "desiredCount": 1,
                "runningCount": 1,
                "pendingCount": 0,
                "taskDefinition": TD_ARN,
                "launchType": "EC2",
                "networkConfiguration": {},
                "loadBalancers": [],
            }
        ],
        "failures": [],
    }
    items = EcsServiceCollector(
        _mgr(),
        ecs_service_name=ECS,
        elbv2_service_name="elbv2",
        ec2_service_name="ec2",
        client_factory=lambda _r: client,
        sleep=lambda _s: None,
        max_retries=1,
    ).fetch_page(account_id=ACCOUNT, region=REGION).items
    assert len(items) == 1
    assert items[0].resource_id == SERVICE_ARN


def test_ecs_task_definition_soft_failure() -> None:
    client = MagicMock()
    client.list_task_definitions.return_value = {
        "taskDefinitionArns": [TD_ARN, f"{TD_ARN[:-1]}4"]
    }

    def describe_task_definition(**kwargs):
        if kwargs["taskDefinition"] == TD_ARN:
            return {
                "taskDefinition": {
                    "taskDefinitionArn": TD_ARN,
                    "family": "web",
                    "revision": 3,
                    "status": "ACTIVE",
                    "networkMode": "awsvpc",
                    "requiresCompatibilities": ["FARGATE"],
                    "cpu": "256",
                    "memory": "512",
                    "containerDefinitions": [
                        {"name": "web", "image": "web:latest", "essential": True}
                    ],
                }
            }
        raise _client_error("AccessDeniedException")

    client.describe_task_definition.side_effect = describe_task_definition
    items = EcsTaskDefinitionCollector(
        _mgr(),
        ecs_service_name=ECS,
        client_factory=lambda _r: client,
        sleep=lambda _s: None,
        max_retries=1,
    ).fetch_page(account_id=ACCOUNT, region=REGION).items
    assert len(items) == 2
    ok = next(i for i in items if i.resource_id == TD_ARN)
    assert ok.observed_state["family"] == "web"
    assert ok.observed_state["network_mode"] == "awsvpc"
    failed = next(i for i in items if i.observed_state.get("access_failure"))
    assert failed.observed_state["status"] == "access_failed"


def test_create_ecs_collectors() -> None:
    names = [
        c.name
        for c in create_ecs_collectors(
            _mgr(),
            ecs_service_name=ECS,
            elbv2_service_name="elbv2",
            ec2_service_name="ec2",
        )
    ]
    assert names == ["ecs.cluster", "ecs.service", "ecs.task_definition"]
