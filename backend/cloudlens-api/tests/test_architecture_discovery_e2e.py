"""End-to-end architecture discovery against a mocked AWS account.

Real collectors + discovery runner + orchestrator + SQLite + graph builder.
Only the AWS clients are fake: each returns canned API responses shaped like
the real Boto3 responses. Covers the five reference architectures plus
missing permissions, identity mismatch and read-only enforcement.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from botocore.exceptions import ClientError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.accounts.models import AccountsConfig
from app.connectors.aws.errors import AWSAccountMismatchError
from app.connectors.aws.session import CallerIdentity
from app.db.base import Base
from app.db.models import Resource
from app.discovery.runner import discover_account
from app.graph.export import to_mermaid, to_svg
from app.graph.models import GraphDocument, GraphScope
from app.graph.service import build_document

A, OTHER = "111122223333", "999988887777"
R = "us-east-1"
ELB = f"arn:aws:elasticloadbalancing:{R}:{A}"
LB1, LB2 = f"{ELB}:loadbalancer/app/fargate/1", f"{ELB}:loadbalancer/app/ec2web/2"
LB1_DNS = "fargate-alb-1.us-east-1.elb.amazonaws.com"
L1, L2 = f"{ELB}:listener/app/fargate/1/11", f"{ELB}:listener/app/ec2web/2/22"
RULE1 = f"{ELB}:listener-rule/app/fargate/1/11/111"
TG1, TG2, TG3 = (f"{ELB}:targetgroup/{n}" for n in ("fargate/1", "ec2web/2", "denied/3"))
C1 = f"arn:aws:ecs:{R}:{A}:cluster/web"
S1 = f"arn:aws:ecs:{R}:{A}:service/web/web"
T1 = f"arn:aws:ecs:{R}:{A}:task/web/abc"
TD = f"arn:aws:ecs:{R}:{A}:task-definition/web:3"
EKS = f"arn:aws:eks:{R}:{A}:cluster/prod"
NG = f"arn:aws:eks:{R}:{A}:nodegroup/prod/ng1/x"
RDS = f"arn:aws:rds:{R}:{A}:db:orders-db"
DBSG = f"arn:aws:rds:{R}:{A}:subgrp:db-subnets"
CF = f"arn:aws:cloudfront::{A}:distribution/E1"

COLLECTORS = [
    "network.vpc",
    "network.subnet",
    "network.security_group",
    "network.eni",
    "compute.ec2_instance",
    "elbv2.load_balancer",
    "elbv2.listener",
    "elbv2.listener_rule",
    "elbv2.target_group",
    "elbv2.target_health",
    "ecs.cluster",
    "ecs.service",
    "ecs.task",
    "eks.cluster",
    "eks.node_group",
    "rds.db_instance",
    "rds.db_subnet_group",
    "cloudfront.distribution",
    "lambda.function",
]


def _denied(op: str) -> Callable[..., Any]:
    def _raise(**_: Any) -> Any:
        raise ClientError(
            {
                "Error": {"Code": "AccessDenied", "Message": "not authorized"},
                "ResponseMetadata": {"HTTPStatusCode": 403},
            },
            op,
        )

    return _raise


def _lb(arn: str, name: str, dns: str) -> dict[str, Any]:
    return {
        "LoadBalancerArn": arn,
        "LoadBalancerName": name,
        "DNSName": dns,
        "VpcId": "vpc-app",
        "Type": "application",
        "Scheme": "internet-facing",
        "State": {"Code": "active"},
        "AvailabilityZones": [{"ZoneName": f"{R}a", "SubnetId": "subnet-pub-a"}],
        "SecurityGroups": ["sg-alb"],
    }


def _eni(eni: str, ip: str, subnet: str, instance: str | None = None) -> dict[str, Any]:
    return {
        "NetworkInterfaceId": eni,
        "VpcId": "vpc-app",
        "SubnetId": subnet,
        "PrivateIpAddress": ip,
        "PrivateIpAddresses": [{"PrivateIpAddress": ip, "Primary": True}],
        "Groups": [{"GroupId": "sg-app"}],
        "Status": "in-use",
        "InterfaceType": "interface",
        "Attachment": {"InstanceId": instance} if instance else {},
    }


WORLD: dict[str, dict[str, Callable[..., Any]]] = {
    "ec2": {
        "describe_vpcs": lambda **_: {
            "Vpcs": [
                {
                    "VpcId": "vpc-app",
                    "CidrBlock": "10.0.0.0/16",
                    "Tags": [{"Key": "Name", "Value": "app"}],
                }
            ]
        },
        "describe_subnets": lambda **_: {
            "Subnets": [
                {"SubnetId": s, "VpcId": "vpc-app", "CidrBlock": c, "AvailabilityZone": f"{R}a"}
                for s, c in [
                    ("subnet-pub-a", "10.0.0.0/24"),
                    ("subnet-app-a", "10.0.2.0/24"),
                    ("subnet-db-a", "10.0.4.0/24"),
                    ("subnet-db-b", "10.0.5.0/24"),
                ]
            ]
        },
        "describe_security_groups": lambda **_: {
            "SecurityGroups": [
                {
                    "GroupId": g,
                    "GroupName": g,
                    "VpcId": "vpc-app",
                    "IpPermissions": [],
                    "IpPermissionsEgress": [],
                }
                for g in ("sg-alb", "sg-app", "sg-db")
            ]
        },
        "describe_network_interfaces": lambda **_: {
            "NetworkInterfaces": [
                _eni("eni-t1", "10.0.2.10", "subnet-app-a"),
                _eni("eni-web1", "10.0.2.20", "subnet-app-a", "i-web1"),
            ]
        },
        "describe_instances": lambda **_: {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-web1",
                            "VpcId": "vpc-app",
                            "SubnetId": "subnet-app-a",
                            "State": {"Name": "running", "Code": 16},
                            "PrivateIpAddress": "10.0.2.20",
                            "SecurityGroups": [{"GroupId": "sg-app"}],
                            "NetworkInterfaces": [
                                {
                                    "NetworkInterfaceId": "eni-web1",
                                    "Groups": [{"GroupId": "sg-app"}],
                                }
                            ],
                        }
                    ]
                }
            ]
        },
    },
    "elbv2": {
        "describe_load_balancers": lambda **_: {
            "LoadBalancers": [
                _lb(LB1, "fargate", LB1_DNS),
                _lb(LB2, "ec2web", "ec2web-2.us-east-1.elb.amazonaws.com"),
            ]
        },
        "describe_listeners": lambda LoadBalancerArn, **_: {
            "Listeners": [
                {
                    "ListenerArn": L1 if LoadBalancerArn == LB1 else L2,
                    "LoadBalancerArn": LoadBalancerArn,
                    "Port": 443,
                    "Protocol": "HTTPS",
                    "DefaultActions": [
                        {
                            "Type": "forward",
                            "TargetGroupArn": TG1 if LoadBalancerArn == LB1 else TG2,
                        }
                    ],
                }
            ]
        },
        "describe_rules": lambda ListenerArn, **_: {
            "Rules": [
                {
                    "RuleArn": RULE1,
                    "Priority": "10",
                    "IsDefault": False,
                    "Conditions": [{"Field": "path-pattern", "Values": ["/api/*"]}],
                    "Actions": [{"Type": "forward", "TargetGroupArn": TG1}],
                }
            ]
            if ListenerArn == L1
            else []
        },
        "describe_target_groups": lambda **_: {
            "TargetGroups": [
                {
                    "TargetGroupArn": TG1,
                    "TargetGroupName": "fargate",
                    "Protocol": "HTTP",
                    "Port": 8080,
                    "VpcId": "vpc-app",
                    "TargetType": "ip",
                    "LoadBalancerArns": [LB1],
                },
                {
                    "TargetGroupArn": TG2,
                    "TargetGroupName": "ec2web",
                    "Protocol": "HTTP",
                    "Port": 80,
                    "VpcId": "vpc-app",
                    "TargetType": "instance",
                    "LoadBalancerArns": [LB2],
                },
                {
                    "TargetGroupArn": TG3,
                    "TargetGroupName": "denied",
                    "Protocol": "HTTP",
                    "Port": 80,
                    "VpcId": "vpc-app",
                    "TargetType": "instance",
                    "LoadBalancerArns": [],
                },
            ]
        },
        "describe_target_health": lambda TargetGroupArn, **_: (
            _denied("DescribeTargetHealth")()
            if TargetGroupArn == TG3
            else {
                "TargetHealthDescriptions": [
                    {
                        "Target": {
                            "Id": "10.0.2.10" if TargetGroupArn == TG1 else "i-web1",
                            "Port": 8080 if TargetGroupArn == TG1 else 80,
                        },
                        "TargetHealth": {"State": "healthy"},
                    }
                ]
            }
        ),
    },
    "ecs": {
        "list_clusters": lambda **_: {"clusterArns": [C1]},
        "describe_clusters": lambda **_: {
            "clusters": [{"clusterArn": C1, "clusterName": "web", "status": "ACTIVE"}]
        },
        "list_services": lambda **_: {"serviceArns": [S1]},
        "describe_services": lambda **_: {
            "services": [
                {
                    "serviceArn": S1,
                    "serviceName": "web",
                    "clusterArn": C1,
                    "taskDefinition": TD,
                    "launchType": "FARGATE",
                    "status": "ACTIVE",
                    "networkConfiguration": {
                        "awsvpcConfiguration": {
                            "subnets": ["subnet-app-a"],
                            "securityGroups": ["sg-app"],
                        }
                    },
                    "loadBalancers": [
                        {"targetGroupArn": TG1, "containerName": "web", "containerPort": 8080}
                    ],
                }
            ]
        },
        "list_tasks": lambda **_: {"taskArns": [T1]},
        "describe_tasks": lambda **_: {
            "tasks": [
                {
                    "taskArn": T1,
                    "clusterArn": C1,
                    "taskDefinitionArn": TD,
                    "group": "service:web",
                    "launchType": "FARGATE",
                    "lastStatus": "RUNNING",
                    "desiredStatus": "RUNNING",
                    "attachments": [
                        {
                            "type": "ElasticNetworkInterface",
                            "details": [
                                {"name": "networkInterfaceId", "value": "eni-t1"},
                                {"name": "subnetId", "value": "subnet-app-a"},
                                {"name": "privateIPv4Address", "value": "10.0.2.10"},
                            ],
                        }
                    ],
                }
            ]
        },
    },
    "eks": {
        "list_clusters": lambda **_: {"clusters": ["prod"]},
        "describe_cluster": lambda **_: {
            "cluster": {
                "name": "prod",
                "arn": EKS,
                "status": "ACTIVE",
                "version": "1.30",
                "resourcesVpcConfig": {
                    "vpcId": "vpc-app",
                    "subnetIds": ["subnet-app-a"],
                    "securityGroupIds": ["sg-app"],
                },
            }
        },
        "list_nodegroups": lambda **_: {"nodegroups": ["ng1"]},
        "describe_nodegroup": lambda **_: {
            "nodegroup": {
                "nodegroupName": "ng1",
                "clusterName": "prod",
                "nodegroupArn": NG,
                "status": "ACTIVE",
                "subnets": ["subnet-app-a"],
                "instanceTypes": ["m6i.large"],
                "scalingConfig": {"minSize": 2, "maxSize": 4, "desiredSize": 2},
            }
        },
    },
    "rds": {
        "describe_db_instances": lambda **_: {
            "DBInstances": [
                {
                    "DBInstanceIdentifier": "orders-db",
                    "DBInstanceArn": RDS,
                    "DBInstanceStatus": "available",
                    "Engine": "postgres",
                    "PubliclyAccessible": False,
                    "Endpoint": {
                        "Address": "orders-db.abc.us-east-1.rds.amazonaws.com",
                        "Port": 5432,
                    },
                    "DBSubnetGroup": {
                        "DBSubnetGroupName": "db-subnets",
                        "VpcId": "vpc-app",
                        "Subnets": [
                            {"SubnetIdentifier": "subnet-db-a"},
                            {"SubnetIdentifier": "subnet-db-b"},
                        ],
                    },
                    "VpcSecurityGroups": [{"VpcSecurityGroupId": "sg-db"}],
                }
            ]
        },
        "describe_db_subnet_groups": lambda **_: {
            "DBSubnetGroups": [
                {
                    "DBSubnetGroupName": "db-subnets",
                    "DBSubnetGroupArn": DBSG,
                    "VpcId": "vpc-app",
                    "SubnetGroupStatus": "Complete",
                    "Subnets": [
                        {"SubnetIdentifier": "subnet-db-a"},
                        {"SubnetIdentifier": "subnet-db-b"},
                    ],
                }
            ]
        },
    },
    "cloudfront": {
        "list_distributions": lambda **_: {
            "DistributionList": {
                "IsTruncated": False,
                "Items": [
                    {
                        "Id": "E1",
                        "ARN": CF,
                        "DomainName": "d111.cloudfront.net",
                        "Status": "Deployed",
                        "Enabled": True,
                        "Aliases": {"Quantity": 0},
                        "Origins": {
                            "Quantity": 1,
                            "Items": [
                                {
                                    "Id": "alb",
                                    "DomainName": LB1_DNS,
                                    "OriginPath": "",
                                    "CustomOriginConfig": {"HTTPPort": 80},
                                }
                            ],
                        },
                        "DefaultCacheBehavior": {"TargetOriginId": "alb"},
                        "WebACLId": "",
                    }
                ],
            }
        },
    },
    "lambda": {"list_functions": _denied("ListFunctions")},
}


class _FakeClient:
    def __init__(self, service: str, calls: list[str]) -> None:
        self._service, self._calls = service, calls

    def __getattr__(self, op: str) -> Callable[..., Any]:
        def _call(**kwargs: Any) -> Any:
            self._calls.append(f"{self._service}:{op}")
            handler = WORLD.get(self._service, {}).get(op)
            return handler(**kwargs) if handler else {}

        return _call


class _FakeSessionManager:
    def __init__(self, live_account: str) -> None:
        self.live_account = live_account
        self.calls: list[str] = []

    def validate_identity(self) -> CallerIdentity:
        if self.live_account != A:
            raise AWSAccountMismatchError(
                expected_account_id=A,
                actual_account_id=self.live_account,
                project_alias="shop",
                account_alias="prod",
            )
        return CallerIdentity(account_id=A, arn=f"arn:aws:sts::{A}:assumed-role/ro/x", user_id="x")

    def client(self, service: str, region: str | None = None, **_: Any) -> _FakeClient:
        return _FakeClient(service, self.calls)


def _config() -> AccountsConfig:
    return AccountsConfig.model_validate(
        {
            "version": "1",
            "aws_api": {
                **{
                    f"{s}_service_name": s
                    for s in [
                        "sts",
                        "ec2",
                        "elbv2",
                        "ecs",
                        "eks",
                        "rds",
                        "s3",
                        "wafv2",
                        "route53",
                        "logs",
                        "lambda",
                        "cloudfront",
                        "apigateway",
                        "apigatewayv2",
                    ]
                },
                "global_control_region": R,
                "s3_default_region": R,
            },
            "projects": [
                {
                    "alias": "shop",
                    "accounts": [
                        {
                            "alias": "prod",
                            "account_id": A,
                            "regions": [R],
                            "collectors": {"enabled": COLLECTORS},
                            "auth": {
                                "type": "assume_role",
                                "role_arn": f"arn:aws:iam::{A}:role/CloudLensReadOnly",
                            },
                        }
                    ],
                }
            ],
        }
    )


@pytest.fixture
def factory(tmp_path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'e2e.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, autoflush=False)
    engine.dispose()


@pytest.fixture
def discovered(factory) -> tuple[sessionmaker[Session], _FakeSessionManager, Any]:
    sm = _FakeSessionManager(A)
    report = discover_account(
        _config(), "shop", "prod", factory, session_manager_factory=lambda *_: sm
    )
    return factory, sm, report


def _doc(factory: sessionmaker[Session], **scope: Any) -> GraphDocument:
    with factory() as s:
        return build_document(
            s, _config(), GraphScope(project_alias="shop", account_ids=[], **scope), max_nodes=500
        )


def _edges(doc: GraphDocument) -> set[tuple[str, str, str, str]]:
    by = {n.id: n for n in doc.nodes}
    return {
        (by[e.source].resource_id, e.relationship_type, by[e.target].resource_id, e.confidence)
        for e in doc.edges
    }


def test_discovery_runs_read_only_after_identity_check(discovered) -> None:
    _, sm, report = discovered
    assert report.identity_verified and report.aborted_reason is None
    assert sm.calls, "collectors made no calls"
    assert all(c.split(":", 1)[1].startswith(("describe_", "list_", "get_")) for c in sm.calls)
    assert not report.skipped


def test_alb_to_ecs_fargate(discovered) -> None:
    factory, _, _ = discovered
    edges = _edges(_doc(factory, detail="full"))
    assert (L1, "belongs_to", LB1, "verified") in edges
    assert (RULE1, "belongs_to", L1, "verified") in edges
    assert (RULE1, "forwards_to", TG1, "verified") in edges
    assert (S1, "forwards_to", TG1, "verified") in edges
    assert (T1, "member_of", S1, "verified") in edges
    assert (T1, "uses", "eni-t1", "verified") in edges
    # Registered IP -> ENI is the only inferred hop in the chain.
    assert (TG1, "routes_to", "eni-t1", "inferred") in edges


def test_alb_to_ec2_targets_and_ec2_eni_subnet_vpc(discovered) -> None:
    factory, _, _ = discovered
    doc = _doc(factory, detail="full")
    edges = _edges(doc)
    assert (L2, "forwards_to", TG2, "verified") in edges
    assert (TG2, "routes_to", "i-web1", "verified") in edges
    assert ("i-web1", "uses", "eni-web1", "verified") in edges
    inst = next(n for n in doc.nodes if n.resource_id == "i-web1")
    assert inst.parent_group == f"subnet:{A}:{R}:subnet-app-a"
    subnet = next(g for g in doc.groups if g.id == inst.parent_group)
    assert subnet.parent_id == f"vpc:{A}:{R}:vpc-app" and subnet.node_id


def test_eks_managed_node_groups_without_workload_assumptions(discovered) -> None:
    factory, _, _ = discovered
    doc = _doc(factory, detail="full")
    assert ("prod/ng1", "belongs_to", "prod", "verified") in _edges(doc)
    cluster = next(n for n in doc.nodes if n.resource_type == "eks-cluster")
    assert cluster.arn == EKS
    eks_ids = {n.id for n in doc.nodes if n.service == "eks"}
    # DescribeCluster/DescribeNodegroup expose no workloads: no flow edges from EKS.
    assert not [e for e in doc.edges if e.source in eks_ids and e.category == "flow"]


def test_private_rds_in_application_vpc_without_invented_app_link(discovered) -> None:
    factory, _, _ = discovered
    doc = _doc(factory, detail="full")
    edges = _edges(doc)
    assert ("orders-db", "uses", "db-subnets", "verified") in edges
    rds = next(n for n in doc.nodes if n.resource_type == "rds-db-instance")
    assert rds.vpc_id == "vpc-app" and rds.subnet_id is None  # spans two subnets
    app = {n.id for n in doc.nodes if n.resource_type in {"ecs-task", "ecs-service", "instance"}}
    assert not [
        e for e in doc.edges if {e.source, e.target} & app and rds.id in {e.source, e.target}
    ]
    vpc_view = _doc(factory, view="vpc", vpc_id="vpc-app")
    assert rds.id in {n.id for n in vpc_view.nodes}


def test_cloudfront_to_alb_origin(discovered) -> None:
    factory, _, _ = discovered
    doc = _doc(factory)
    assert ("E1", "origin", LB1, "verified") in _edges(doc)
    cf = next(n for n in doc.nodes if n.resource_type == "cloudfront-distribution")
    assert cf.parent_group == f"global:{A}"
    app = _doc(factory, view="application", seed=cf.id, detail="full")
    ids = {n.resource_id for n in app.nodes}
    assert {"E1", LB1, L1, TG1, "eni-t1", T1, S1} <= ids
    assert LB2 not in ids and "i-web1" not in ids  # same VPC, different app


def test_missing_permissions_and_partial_runs_are_reported(discovered) -> None:
    factory, _, report = discovered
    doc = _doc(factory)
    actions = {m.iam_action for m in doc.coverage.missing_permissions}
    assert "lambda:ListFunctions" in actions  # hard failure
    assert "elasticloadbalancing:DescribeTargetHealth" in actions  # per-item soft failure
    statuses = {(o.collector, o.status) for o in report.outcomes}
    assert ("lambda.function", "failed") in statuses
    assert ("elbv2.target_health", "partial") in statuses
    # TG3's targets were unreadable: no edge is invented for them.
    assert not [e for e in _edges(doc) if e[0] == TG3]


def test_identity_mismatch_aborts_without_collecting(factory) -> None:
    sm = _FakeSessionManager(OTHER)
    report = discover_account(
        _config(), "shop", "prod", factory, session_manager_factory=lambda *_: sm
    )
    assert not report.identity_verified and "mismatch" in (report.aborted_reason or "")
    assert sm.calls == []
    with factory() as s:
        assert s.query(Resource).count() == 0


def test_diagrams_generate_from_discovered_inventory(discovered) -> None:
    factory, _, _ = discovered
    for doc in (
        _doc(factory),
        _doc(factory, view="vpc", vpc_id="vpc-app"),
        _doc(factory, view="service", service="ecs"),
    ):
        mmd, svg = to_mermaid(doc), to_svg(doc)
        assert "flowchart LR" in mmd and svg.startswith("<svg")
        assert doc.coverage.newest_observation is not None
