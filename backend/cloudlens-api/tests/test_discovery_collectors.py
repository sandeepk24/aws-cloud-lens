"""Tests for architecture-discovery collectors, collector changes and the registry."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml
from botocore.exceptions import ClientError

from app.accounts.models import AccountsConfig, AwsApiConfig
from app.collectors.apigateway import (
    ApiGatewayRestApiCollector,
    ApiGatewayV2ApiCollector,
    lambda_arn_from_integration_uri,
)
from app.collectors.cloudfront import CloudFrontDistributionCollector
from app.collectors.ecs import EcsTaskCollector
from app.collectors.elbv2 import ListenerRuleCollector, TargetHealthCollector
from app.collectors.lambda_functions import LambdaFunctionCollector
from app.collectors.normalized import ANY_RESOURCE_TYPE, NormalizedResource
from app.collectors.rds import RdsDbSubnetGroupCollector, RdsInstanceCollector
from app.collectors.registry import COLLECTOR_SPECS, build_collector
from app.collectors.route53 import Route53RecordCollector, normalize_dns_name
from app.collectors.wafv2 import Wafv2CloudFrontWebAclCollector, Wafv2RegionalWebAclCollector

ACCOUNT = "111122223333"
REGION = "test-region-a"
GLOBAL = "test-global-region"
LB_ARN = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:loadbalancer/app/web/abc"
LISTENER_ARN = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:listener/app/web/abc/111"
TG_A = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:targetgroup/a/1"
TG_B = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:targetgroup/b/2"
FN_ARN = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:api"
CLUSTER_ARN = f"arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/prod"
ROOT = Path(__file__).resolve().parents[3]


def _common(client: Any) -> dict[str, Any]:
    return {"client_factory": lambda _r: client, "sleep": lambda _s: None}


def _denied(op: str) -> ClientError:
    return ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, op)


def _edges(item: NormalizedResource) -> set[tuple[str, str, str]]:
    return {(e.relationship_type, e.to_resource_type, e.to_resource_id) for e in item.relationships}


def _assert_evidence(items: list[NormalizedResource]) -> None:
    for item in items:
        for e in item.relationships:
            assert e.evidence, (item.resource_type, e.relationship_type)
            assert e.confidence == "verified"


def _api_config(**overrides: Any) -> AwsApiConfig:
    base = dict(
        sts_service_name="sts",
        ec2_service_name="ec2",
        elbv2_service_name="elbv2",
        ecs_service_name="ecs",
        eks_service_name="eks",
        rds_service_name="rds",
        s3_service_name="s3",
        wafv2_service_name="wafv2",
        route53_service_name="route53",
        logs_service_name="logs",
        global_control_region=GLOBAL,
        s3_default_region=REGION,
        lambda_service_name="lambda",
        cloudfront_service_name="cloudfront",
        apigateway_service_name="apigateway",
        apigatewayv2_service_name="apigatewayv2",
    )
    base.update(overrides)
    return AwsApiConfig(**base)


# --------------------------------------------------------------------------- #
# ELBv2 listener rules
# --------------------------------------------------------------------------- #
def test_listener_rules_paginate_all_levels_and_link_target_groups() -> None:
    client = MagicMock()
    client.describe_load_balancers.return_value = {
        "LoadBalancers": [{"LoadBalancerArn": LB_ARN}],
        "NextMarker": "lb-next",
    }
    client.describe_listeners.side_effect = [
        {"Listeners": [{"ListenerArn": LISTENER_ARN}], "NextMarker": "l2"},
        {"Listeners": []},
    ]
    client.describe_rules.side_effect = [
        {
            "Rules": [
                {
                    "RuleArn": "rule-1",
                    "Priority": "10",
                    "IsDefault": False,
                    "Conditions": [
                        {"Field": "path-pattern", "PathPatternConfig": {"Values": ["/api/*"]}}
                    ],
                    "Actions": [
                        {
                            "Type": "forward",
                            "ForwardConfig": {
                                "TargetGroups": [
                                    {"TargetGroupArn": TG_A, "Weight": 80},
                                    {"TargetGroupArn": TG_B, "Weight": 20},
                                ]
                            },
                        }
                    ],
                }
            ],
            "NextMarker": "r2",
        },
        {
            "Rules": [
                {
                    "RuleArn": "rule-default",
                    "Priority": "default",
                    "IsDefault": True,
                    "Actions": [{"Type": "forward", "TargetGroupArn": TG_A}],
                }
            ]
        },
    ]
    page = ListenerRuleCollector(
        MagicMock(), elbv2_service_name="elbv2", **_common(client)
    ).fetch_page(account_id=ACCOUNT, region=REGION)
    assert page.next_token == "lb-next"
    assert [i.resource_id for i in page.items] == ["rule-1", "rule-default"]
    rule = page.items[0]
    assert rule.observed_state["conditions"] == [{"field": "path-pattern", "values": ["/api/*"]}]
    assert rule.observed_state["actions"][0]["weights"] == {TG_A: 80, TG_B: 20}
    assert _edges(rule) == {
        ("belongs_to", "listener", LISTENER_ARN),
        ("forwards_to", "target-group", TG_A),
        ("forwards_to", "target-group", TG_B),
    }
    assert page.items[1].observed_state["is_default"] is True
    assert client.describe_rules.call_args_list[1].kwargs["Marker"] == "r2"
    _assert_evidence(page.items)


def test_listener_rules_soft_failure_keeps_other_listeners() -> None:
    client = MagicMock()
    client.describe_load_balancers.return_value = {"LoadBalancers": [{"LoadBalancerArn": LB_ARN}]}
    client.describe_listeners.return_value = {
        "Listeners": [{"ListenerArn": "l-denied"}, {"ListenerArn": LISTENER_ARN}]
    }

    def rules(**kw: Any) -> Any:
        if kw["ListenerArn"] == "l-denied":
            raise _denied("DescribeRules")
        return {"Rules": [{"RuleArn": "ok", "Actions": []}]}

    client.describe_rules.side_effect = rules
    page = ListenerRuleCollector(
        MagicMock(), elbv2_service_name="elbv2", **_common(client)
    ).fetch_page(account_id=ACCOUNT, region=REGION)
    assert [i.resource_id for i in page.items] == ["ok"]


# --------------------------------------------------------------------------- #
# Target health
# --------------------------------------------------------------------------- #
def test_target_health_classifies_targets_and_never_links_ip_targets() -> None:
    client = MagicMock()
    client.describe_target_groups.return_value = {
        "TargetGroups": [
            {"TargetGroupArn": TG_A, "TargetType": "ip", "VpcId": "vpc-1"},
            {"TargetGroupArn": TG_B, "TargetType": "lambda"},
            {"TargetGroupArn": "tg-inst", "TargetType": "instance", "VpcId": "vpc-1"},
            {"TargetGroupArn": "tg-denied", "TargetType": "instance"},
        ]
    }

    def health(**kw: Any) -> Any:
        tg = kw["TargetGroupArn"]
        if tg == "tg-denied":
            raise _denied("DescribeTargetHealth")
        target = {
            TG_A: {"Id": "10.0.1.5", "Port": 8080},
            TG_B: {"Id": FN_ARN},
            "tg-inst": {"Id": "i-123", "Port": 80},
        }[tg]
        return {
            "TargetHealthDescriptions": [{"Target": target, "TargetHealth": {"State": "healthy"}}]
        }

    client.describe_target_health.side_effect = health
    items = (
        TargetHealthCollector(
            MagicMock(), elbv2_service_name="elbv2", ec2_service_name="ec2", **_common(client)
        )
        .fetch_page(account_id=ACCOUNT, region=REGION)
        .items
    )
    by_type = {i.observed_state["target_type"]: i for i in items}
    assert set(by_type) == {"ip", "lambda", "instance"}
    ip = by_type["ip"]
    assert ip.observed_state["target_id"] == "10.0.1.5"
    assert ip.observed_state["target_group_vpc_id"] == "vpc-1"
    assert _edges(ip) == {("belongs_to", "target-group", TG_A)}
    assert ("targets", ANY_RESOURCE_TYPE, FN_ARN) in _edges(by_type["lambda"])
    assert ("targets", "instance", "i-123") in _edges(by_type["instance"])
    _assert_evidence(items)


# --------------------------------------------------------------------------- #
# ECS tasks
# --------------------------------------------------------------------------- #
def test_ecs_tasks_paginate_and_link_eni_subnet_service() -> None:
    client = MagicMock()
    client.list_clusters.return_value = {"clusterArns": [CLUSTER_ARN], "nextToken": "c2"}
    client.list_tasks.side_effect = [
        {"taskArns": ["t1"], "nextToken": "n"},
        {"taskArns": ["t2"]},
    ]
    task = {
        "taskArn": f"arn:aws:ecs:{REGION}:{ACCOUNT}:task/prod/t1",
        "clusterArn": CLUSTER_ARN,
        "taskDefinitionArn": "td:1",
        "group": "service:web",
        "launchType": "FARGATE",
        "lastStatus": "RUNNING",
        "attachments": [
            {
                "type": "ElasticNetworkInterface",
                "details": [
                    {"name": "networkInterfaceId", "value": "eni-1"},
                    {"name": "subnetId", "value": "subnet-1"},
                    {"name": "privateIPv4Address", "value": "10.0.1.5"},
                ],
            }
        ],
    }
    client.describe_tasks.return_value = {"tasks": [task]}
    page = EcsTaskCollector(
        MagicMock(), ecs_service_name="ecs", ec2_service_name="ec2", **_common(client)
    ).fetch_page(account_id=ACCOUNT, region=REGION)
    assert page.next_token == "c2"
    assert client.describe_tasks.call_args.kwargs["tasks"] == ["t1", "t2"]
    item = page.items[0]
    assert item.observed_state["private_ips"] == ["10.0.1.5"]
    assert item.observed_state["service_name"] == "web"
    assert _edges(item) == {
        ("belongs_to", "ecs-cluster", CLUSTER_ARN),
        ("uses", "ecs-task-definition", "td:1"),
        ("uses", "network-interface", "eni-1"),
        ("located_in", "subnet", "subnet-1"),
        ("member_of", "ecs-service", f"{CLUSTER_ARN}/web"),
    }
    _assert_evidence(page.items)


def test_ecs_task_without_service_group_has_no_service_edge() -> None:
    client = MagicMock()
    client.list_clusters.return_value = {"clusterArns": [CLUSTER_ARN]}
    client.list_tasks.return_value = {"taskArns": ["t1"]}
    client.describe_tasks.return_value = {
        "tasks": [{"taskArn": "arn:t1", "clusterArn": CLUSTER_ARN, "group": "family:batch"}]
    }
    item = (
        EcsTaskCollector(
            MagicMock(), ecs_service_name="ecs", ec2_service_name="ec2", **_common(client)
        )
        .fetch_page(account_id=ACCOUNT, region=REGION)
        .items[0]
    )
    assert "member_of" not in {e.relationship_type for e in item.relationships}


# --------------------------------------------------------------------------- #
# Lambda
# --------------------------------------------------------------------------- #
def test_lambda_functions_pagination_and_vpc_edges() -> None:
    client = MagicMock()
    client.list_functions.return_value = {
        "Functions": [
            {
                "FunctionName": "api",
                "FunctionArn": FN_ARN,
                "Runtime": "python3.12",
                "VpcConfig": {
                    "VpcId": "vpc-1",
                    "SubnetIds": ["subnet-1"],
                    "SecurityGroupIds": ["sg-1"],
                },
                "Environment": {"Variables": {"SECRET": "x"}},
            },
            {"FunctionName": "plain", "FunctionArn": FN_ARN + "2", "VpcConfig": {}},
        ],
        "NextMarker": "m2",
    }
    page = LambdaFunctionCollector(
        MagicMock(),
        lambda_service_name="lambda",
        ec2_service_name="ec2",
        page_size=200,
        **_common(client),
    ).fetch_page(account_id=ACCOUNT, region=REGION, next_token="m1")
    assert client.list_functions.call_args.kwargs == {"MaxItems": 50, "Marker": "m1"}
    assert page.next_token == "m2"
    vpc_fn, plain = page.items
    assert _edges(vpc_fn) == {
        ("belongs_to", "vpc", "vpc-1"),
        ("associated_with", "subnet", "subnet-1"),
        ("uses", "security-group", "sg-1"),
    }
    assert plain.relationships == []
    assert "SECRET" not in str(vpc_fn.observed_state)
    _assert_evidence(page.items)


# --------------------------------------------------------------------------- #
# RDS subnet groups
# --------------------------------------------------------------------------- #
def test_rds_subnet_group_and_instance_link() -> None:
    client = MagicMock()
    client.describe_db_subnet_groups.return_value = {
        "DBSubnetGroups": [
            {
                "DBSubnetGroupName": "db-private",
                "DBSubnetGroupArn": "arn:sg",
                "VpcId": "vpc-1",
                "SubnetGroupStatus": "Complete",
                "Subnets": [{"SubnetIdentifier": "subnet-1"}, {"SubnetIdentifier": "subnet-2"}],
            }
        ]
    }
    group = (
        RdsDbSubnetGroupCollector(
            MagicMock(), rds_service_name="rds", ec2_service_name="ec2", **_common(client)
        )
        .fetch_page(account_id=ACCOUNT, region=REGION)
        .items[0]
    )
    assert _edges(group) == {
        ("belongs_to", "vpc", "vpc-1"),
        ("contains", "subnet", "subnet-1"),
        ("contains", "subnet", "subnet-2"),
    }
    instance = RdsInstanceCollector(
        MagicMock(), rds_service_name="rds", ec2_service_name="ec2", **_common(MagicMock())
    ).normalize(
        {
            "DBInstanceIdentifier": "db1",
            "DBInstanceArn": "arn:db1",
            "DBSubnetGroup": {"DBSubnetGroupName": "db-private", "VpcId": "vpc-1", "Subnets": []},
        },
        account_id=ACCOUNT,
        region=REGION,
    )
    assert ("uses", "rds-db-subnet-group", "db-private") in _edges(instance)
    assert instance.observed_state["db_subnet_group_name"] == "db-private"
    _assert_evidence([group])


# --------------------------------------------------------------------------- #
# CloudFront
# --------------------------------------------------------------------------- #
def test_cloudfront_is_global_paginates_and_records_origins() -> None:
    client = MagicMock()
    client.list_distributions.return_value = {
        "DistributionList": {
            "IsTruncated": True,
            "NextMarker": "d2",
            "Items": [
                {
                    "Id": "E1",
                    "ARN": f"arn:aws:cloudfront::{ACCOUNT}:distribution/E1",
                    "DomainName": "d111.cloudfront.net",
                    "Aliases": {"Items": ["www.example.com"]},
                    "Status": "Deployed",
                    "Enabled": True,
                    "Origins": {
                        "Items": [
                            {
                                "Id": "alb",
                                "DomainName": "web-1.elb.amazonaws.com",
                                "CustomOriginConfig": {},
                            },
                            {
                                "Id": "s3",
                                "DomainName": "assets.s3.amazonaws.com",
                                "S3OriginConfig": {},
                            },
                        ]
                    },
                    "DefaultCacheBehavior": {"TargetOriginId": "alb"},
                    "WebACLId": f"arn:aws:wafv2:us-east-1:{ACCOUNT}:global/webacl/cf/1",
                },
                {"Id": "E2", "Origins": {"Items": []}, "WebACLId": ""},
            ],
        }
    }
    collector = CloudFrontDistributionCollector(
        MagicMock(),
        cloudfront_service_name="cloudfront",
        global_control_region=GLOBAL,
        **_common(client),
    )
    assert collector.fetch_page(account_id=ACCOUNT, region=REGION).items == []
    page = collector.fetch_page(account_id=ACCOUNT, region=GLOBAL)
    assert client.list_distributions.call_args.kwargs["MaxItems"] == "100"
    assert page.next_token == "d2"
    dist = page.items[0]
    assert dist.region == GLOBAL
    assert [o["origin_type"] for o in dist.observed_state["origins"]] == ["custom", "s3"]
    assert dist.observed_state["default_origin_id"] == "alb"
    assert [e.relationship_type for e in dist.relationships] == ["uses"]
    assert dist.relationships[0].to_resource_type == ANY_RESOURCE_TYPE
    assert page.items[1].relationships == []
    _assert_evidence(page.items)


# --------------------------------------------------------------------------- #
# API Gateway
# --------------------------------------------------------------------------- #
def test_lambda_arn_parsing() -> None:
    uri = f"arn:aws:apigateway:{REGION}:lambda:path/2015-03-31/functions/{FN_ARN}/invocations"
    assert lambda_arn_from_integration_uri(uri) == FN_ARN
    assert lambda_arn_from_integration_uri(FN_ARN) == FN_ARN
    assert lambda_arn_from_integration_uri("https://example.com/api") is None
    assert lambda_arn_from_integration_uri(None) is None


def test_rest_api_paginates_resources_and_links_lambda() -> None:
    client = MagicMock()
    client.get_rest_apis.return_value = {
        "items": [{"id": "abc", "name": "orders"}],
        "position": "p2",
    }
    uri = f"arn:aws:apigateway:{REGION}:lambda:path/2015-03-31/functions/{FN_ARN}/invocations"
    client.get_resources.side_effect = [
        {
            "items": [
                {
                    "path": "/a",
                    "resourceMethods": {
                        "GET": {"methodIntegration": {"type": "AWS_PROXY", "uri": uri}}
                    },
                }
            ],
            "position": "r2",
        },
        {
            "items": [
                {
                    "path": "/b",
                    "resourceMethods": {
                        "POST": {
                            "methodIntegration": {
                                "type": "HTTP",
                                "uri": "https://legacy.example.com",
                            }
                        }
                    },
                }
            ]
        },
    ]
    page = ApiGatewayRestApiCollector(
        MagicMock(), apigateway_service_name="apigateway", **_common(client)
    ).fetch_page(account_id=ACCOUNT, region=REGION)
    assert page.next_token == "p2"
    api = page.items[0]
    assert api.arn == f"arn:aws:apigateway:{REGION}::/restapis/abc"
    assert api.observed_state["endpoint_domains"] == [f"abc.execute-api.{REGION}.amazonaws.com"]
    assert len(api.observed_state["integrations"]) == 2
    assert _edges(api) == {("integrates_with", ANY_RESOURCE_TYPE, FN_ARN)}
    assert client.get_resources.call_args_list[1].kwargs["position"] == "r2"
    _assert_evidence(page.items)


def test_rest_api_denied_resources_marks_integrations_uncollected() -> None:
    client = MagicMock()
    client.get_rest_apis.return_value = {"items": [{"id": "abc"}]}
    client.get_resources.side_effect = _denied("GetResources")
    api = (
        ApiGatewayRestApiCollector(
            MagicMock(), apigateway_service_name="apigateway", **_common(client)
        )
        .fetch_page(account_id=ACCOUNT, region=REGION)
        .items[0]
    )
    assert api.observed_state["integrations_collected"] is False
    assert api.relationships == []


def test_http_api_links_lambda_and_listener_only() -> None:
    client = MagicMock()
    client.get_apis.return_value = {
        "Items": [
            {
                "ApiId": "h1",
                "Name": "http",
                "ProtocolType": "HTTP",
                "ApiEndpoint": f"https://h1.execute-api.{REGION}.amazonaws.com",
            }
        ],
        "NextToken": "t2",
    }
    client.get_integrations.return_value = {
        "Items": [
            {"IntegrationType": "AWS_PROXY", "IntegrationUri": FN_ARN},
            {
                "IntegrationType": "HTTP_PROXY",
                "IntegrationUri": LISTENER_ARN,
                "ConnectionType": "VPC_LINK",
                "ConnectionId": "vl-1",
            },
            {"IntegrationType": "HTTP_PROXY", "IntegrationUri": "https://example.com"},
        ]
    }
    page = ApiGatewayV2ApiCollector(
        MagicMock(), apigatewayv2_service_name="apigatewayv2", **_common(client)
    ).fetch_page(account_id=ACCOUNT, region=REGION)
    assert page.next_token == "t2"
    api = page.items[0]
    assert api.observed_state["endpoint_domains"] == [f"h1.execute-api.{REGION}.amazonaws.com"]
    assert _edges(api) == {
        ("integrates_with", ANY_RESOURCE_TYPE, FN_ARN),
        ("integrates_with", ANY_RESOURCE_TYPE, LISTENER_ARN),
    }
    _assert_evidence(page.items)


# --------------------------------------------------------------------------- #
# Route 53 records
# --------------------------------------------------------------------------- #
def test_route53_records_alias_and_cname_only_with_pagination() -> None:
    client = MagicMock()
    client.list_hosted_zones.return_value = {
        "HostedZones": [{"Id": "/hostedzone/Z1"}],
        "IsTruncated": True,
        "NextMarker": "z2",
    }
    client.list_resource_record_sets.side_effect = [
        {
            "ResourceRecordSets": [
                {
                    "Name": "www.example.com.",
                    "Type": "A",
                    "AliasTarget": {
                        "DNSName": "dualstack.web-1.elb.amazonaws.com.",
                        "HostedZoneId": "ZELB",
                        "EvaluateTargetHealth": True,
                    },
                },
                {"Name": "example.com.", "Type": "TXT", "ResourceRecords": [{"Value": "secret"}]},
            ],
            "IsTruncated": True,
            "NextRecordName": "z.example.com.",
            "NextRecordType": "CNAME",
        },
        {
            "ResourceRecordSets": [
                {
                    "Name": "z.example.com.",
                    "Type": "CNAME",
                    "ResourceRecords": [{"Value": "d111.cloudfront.net"}],
                },
                {
                    "Name": "plain.example.com.",
                    "Type": "A",
                    "ResourceRecords": [{"Value": "1.2.3.4"}],
                },
            ],
            "IsTruncated": False,
        },
    ]
    collector = Route53RecordCollector(
        MagicMock(), route53_service_name="route53", global_control_region=GLOBAL, **_common(client)
    )
    assert collector.fetch_page(account_id=ACCOUNT, region=REGION).items == []
    page = collector.fetch_page(account_id=ACCOUNT, region=GLOBAL)
    assert page.next_token == "z2"
    assert [i.observed_state["record_type"] for i in page.items] == ["A", "CNAME"]
    alias = page.items[0].observed_state["alias_target"]
    assert alias["dns_name_normalized"] == "web-1.elb.amazonaws.com"
    assert page.items[1].observed_state["cname_values_normalized"] == ["d111.cloudfront.net"]
    second = client.list_resource_record_sets.call_args_list[1].kwargs
    assert second["StartRecordName"] == "z.example.com." and second["StartRecordType"] == "CNAME"
    assert "secret" not in str([i.observed_state for i in page.items])
    assert _edges(page.items[0]) == {("belongs_to", "route53-hosted-zone", "/hostedzone/Z1")}
    _assert_evidence(page.items)


def test_normalize_dns_name() -> None:
    assert normalize_dns_name("DualStack.My-LB.elb.amazonaws.com.") == "my-lb.elb.amazonaws.com"
    assert normalize_dns_name("") is None


# --------------------------------------------------------------------------- #
# WAFv2 association edges
# --------------------------------------------------------------------------- #
def _waf_client(resource_arns: dict[str, list[str]]) -> MagicMock:
    client = MagicMock()
    client.list_web_acls.return_value = {
        "WebACLs": [
            {
                "Name": "acl",
                "Id": "id",
                "ARN": f"arn:aws:wafv2:{REGION}:{ACCOUNT}:regional/webacl/acl/id",
            }
        ]
    }
    client.get_web_acl.return_value = {"WebACL": {"Rules": []}}
    client.list_resources_for_web_acl.side_effect = lambda **kw: {
        "ResourceArns": resource_arns.get(kw.get("ResourceType"), [])
    }
    return client


def test_wafv2_regional_protects_albs_and_api_stages() -> None:
    stage = f"arn:aws:apigateway:{REGION}::/restapis/abc/stages/prod"
    client = _waf_client({"APPLICATION_LOAD_BALANCER": [LB_ARN], "API_GATEWAY": [stage, LB_ARN]})
    acl = (
        Wafv2RegionalWebAclCollector(
            MagicMock(), wafv2_service_name="wafv2", global_control_region=GLOBAL, **_common(client)
        )
        .fetch_page(account_id=ACCOUNT, region=REGION)
        .items[0]
    )
    types = {c.kwargs["ResourceType"] for c in client.list_resources_for_web_acl.call_args_list}
    assert types == {"APPLICATION_LOAD_BALANCER", "API_GATEWAY"}
    assert acl.observed_state["associated_resource_arns"] == [LB_ARN, stage]
    assert _edges(acl) == {
        ("protects", ANY_RESOURCE_TYPE, LB_ARN),
        ("protects", ANY_RESOURCE_TYPE, stage),
    }
    _assert_evidence([acl])


def test_wafv2_cloudfront_scope_does_not_list_associations() -> None:
    client = _waf_client({})
    client.list_web_acls.return_value = {
        "WebACLs": [
            {"Name": "cf", "Id": "1", "ARN": f"arn:aws:wafv2:{GLOBAL}:{ACCOUNT}:global/webacl/cf/1"}
        ]
    }
    acl = (
        Wafv2CloudFrontWebAclCollector(
            MagicMock(), wafv2_service_name="wafv2", global_control_region=GLOBAL, **_common(client)
        )
        .fetch_page(account_id=ACCOUNT, region=GLOBAL)
        .items[0]
    )
    client.list_resources_for_web_acl.assert_not_called()
    assert acl.relationships == []


# --------------------------------------------------------------------------- #
# Registry and configuration
# --------------------------------------------------------------------------- #
def test_registry_covers_every_example_collector_and_builds_it() -> None:
    raw = yaml.safe_load((ROOT / "config/accounts.example.yaml").read_text())
    names = {n for p in raw["projects"] for a in p["accounts"] for n in a["collectors"]["enabled"]}
    assert names <= set(COLLECTOR_SPECS)
    api = _api_config()
    for name, spec in COLLECTOR_SPECS.items():
        collector = build_collector(
            name,
            MagicMock(),
            api,
            page_size=100,
            max_retries=2,
            client_factory=lambda _r: MagicMock(),
            sleep=None,
        )
        assert collector.name == name
        assert set(spec.required_service_fields) <= set(AwsApiConfig.model_fields)
    assert {n for n, s in COLLECTOR_SPECS.items() if s.is_global} == {
        "cloudfront.distribution",
        "wafv2.web_acl_cloudfront",
        "route53.hosted_zone",
        "route53.record",
    }


def test_registry_errors_are_explicit() -> None:
    with pytest.raises(ValueError, match="Unknown collector"):
        build_collector("nope.thing", MagicMock(), _api_config())
    with pytest.raises(ValueError, match="lambda_service_name"):
        build_collector("lambda.function", MagicMock(), _api_config(lambda_service_name=None))


def test_logs_page_size_clamped_to_api_limit() -> None:
    collector = build_collector("logs.log_group", MagicMock(), _api_config(), page_size=500)
    assert collector._page_size == 50  # type: ignore[attr-defined]


def test_existing_config_without_new_fields_still_validates() -> None:
    raw = yaml.safe_load((Path(__file__).parent / "fixtures/accounts.valid.yaml").read_text())
    cfg = AccountsConfig.model_validate(raw)
    assert cfg.aws_api.lambda_service_name is None
    assert all(a.kubernetes.clusters == [] for _, a in cfg.iter_accounts())


def test_kubernetes_block_and_example_yaml_validate() -> None:
    raw = yaml.safe_load((ROOT / "config/accounts.example.yaml").read_text())
    raw["projects"][0]["accounts"][0]["kubernetes"] = {
        "clusters": [{"eks_cluster_name": "prod", "region": REGION, "kubeconfig_context": "ctx"}]
    }
    cfg = AccountsConfig.model_validate(raw)
    clusters = cfg.projects[0].accounts[0].kubernetes.clusters
    assert clusters[0].eks_cluster_name == "prod" and clusters[0].kubeconfig_path is None
    assert cfg.aws_api.cloudfront_service_name == "cloudfront"


def test_example_yaml_matches_json_schema() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    import json

    schema = json.loads((ROOT / "config/schemas/accounts.schema.json").read_text())
    raw = yaml.safe_load((ROOT / "config/accounts.example.yaml").read_text())
    jsonschema.validate(raw, schema)
