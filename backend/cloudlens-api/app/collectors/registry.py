"""Name -> collector construction for every AWS collector (accounts YAML names).

``build_collector`` is the single place that maps a configured collector name
to a class plus its config-driven service names. Kubernetes workload discovery
is not here: it needs a cluster read client, not a boto3 session
(see ``app.collectors.kubernetes.create_kubernetes_collectors``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.accounts.models import AwsApiConfig
from app.collectors.apigateway import ApiGatewayRestApiCollector, ApiGatewayV2ApiCollector
from app.collectors.base import PaginatedCollector
from app.collectors.cloudfront import CloudFrontDistributionCollector
from app.collectors.compute import EbsVolumeCollector, Ec2InstanceCollector, ElasticIpCollector
from app.collectors.ecs import (
    EcsClusterCollector,
    EcsServiceCollector,
    EcsTaskCollector,
    EcsTaskDefinitionCollector,
)
from app.collectors.eks import (
    EksClusterCollector,
    EksFargateProfileCollector,
    EksNodeGroupCollector,
)
from app.collectors.elbv2 import (
    ListenerCollector,
    ListenerRuleCollector,
    LoadBalancerCollector,
    TargetGroupCollector,
    TargetHealthCollector,
)
from app.collectors.lambda_functions import LambdaFunctionCollector
from app.collectors.logs import CloudWatchLogGroupCollector
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
)
from app.collectors.rds import (
    RdsClusterCollector,
    RdsDbSubnetGroupCollector,
    RdsInstanceCollector,
)
from app.collectors.route53 import Route53HostedZoneCollector, Route53RecordCollector
from app.collectors.s3 import S3BucketCollector
from app.collectors.wafv2 import Wafv2CloudFrontWebAclCollector, Wafv2RegionalWebAclCollector
from app.connectors.aws.session import AWSSessionManager

Builder = Callable[[AWSSessionManager, AwsApiConfig, dict[str, Any]], PaginatedCollector]


@dataclass(frozen=True, slots=True)
class CollectorSpec:
    """How to build one collector and what configuration it needs.

    ``is_global``: the collector only returns data for the configured
    ``global_control_region`` (run it once per account in that region).
    """

    name: str
    is_global: bool
    required_service_fields: tuple[str, ...]
    factory: Builder

    def build(
        self, session_manager: AWSSessionManager, aws_api: AwsApiConfig, **common: Any
    ) -> PaginatedCollector:
        return self.factory(session_manager, aws_api, common)


def _svc(aws_api: AwsApiConfig, field: str) -> str:
    value = getattr(aws_api, field, None)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"aws_api.{field} is not configured")
    return value


def _ec2(cls: type) -> Builder:
    return lambda sm, api, c: cls(sm, ec2_service_name=_svc(api, "ec2_service_name"), **c)


def _elb(cls: type) -> Builder:
    return lambda sm, api, c: cls(
        sm,
        elbv2_service_name=_svc(api, "elbv2_service_name"),
        ec2_service_name=_svc(api, "ec2_service_name"),
        **c,
    )


def _eks(cls: type) -> Builder:
    return lambda sm, api, c: cls(
        sm,
        eks_service_name=_svc(api, "eks_service_name"),
        ec2_service_name=_svc(api, "ec2_service_name"),
        **c,
    )


def _rds(cls: type) -> Builder:
    return lambda sm, api, c: cls(
        sm,
        rds_service_name=_svc(api, "rds_service_name"),
        ec2_service_name=_svc(api, "ec2_service_name"),
        **c,
    )


def _waf(cls: type) -> Builder:
    return lambda sm, api, c: cls(
        sm,
        wafv2_service_name=_svc(api, "wafv2_service_name"),
        global_control_region=api.global_control_region,
        **c,
    )


def _r53(cls: type) -> Builder:
    return lambda sm, api, c: cls(
        sm,
        route53_service_name=_svc(api, "route53_service_name"),
        global_control_region=api.global_control_region,
        **c,
    )


_EC2 = ("ec2_service_name",)
_ELB = ("elbv2_service_name", "ec2_service_name")
_EKS = ("eks_service_name", "ec2_service_name")
_RDS = ("rds_service_name", "ec2_service_name")
_ECS = ("ecs_service_name", "ec2_service_name")


def _spec(
    name: str, required: tuple[str, ...], factory: Builder, *, is_global: bool = False
) -> CollectorSpec:
    return CollectorSpec(
        name=name, is_global=is_global, required_service_fields=required, factory=factory
    )


COLLECTOR_SPECS: dict[str, CollectorSpec] = {
    s.name: s
    for s in [
        # network
        _spec("network.vpc", _EC2, _ec2(VpcCollector)),
        _spec("network.subnet", _EC2, _ec2(SubnetCollector)),
        _spec("network.route_table", _EC2, _ec2(RouteTableCollector)),
        _spec("network.internet_gateway", _EC2, _ec2(InternetGatewayCollector)),
        _spec("network.nat_gateway", _EC2, _ec2(NatGatewayCollector)),
        _spec("network.vpc_endpoint", _EC2, _ec2(VpcEndpointCollector)),
        _spec("network.security_group", _EC2, _ec2(SecurityGroupCollector)),
        _spec("network.network_acl", _EC2, _ec2(NetworkAclCollector)),
        _spec("network.eni", _EC2, _ec2(EniCollector)),
        # compute
        _spec("compute.ec2_instance", _EC2, _ec2(Ec2InstanceCollector)),
        _spec("compute.ebs_volume", _EC2, _ec2(EbsVolumeCollector)),
        _spec("compute.elastic_ip", _EC2, _ec2(ElasticIpCollector)),
        # elbv2
        _spec("elbv2.load_balancer", _ELB, _elb(LoadBalancerCollector)),
        _spec("elbv2.listener", _ELB, _elb(ListenerCollector)),
        _spec(
            "elbv2.listener_rule",
            ("elbv2_service_name",),
            lambda sm, api, c: ListenerRuleCollector(
                sm, elbv2_service_name=_svc(api, "elbv2_service_name"), **c
            ),
        ),
        _spec("elbv2.target_group", _ELB, _elb(TargetGroupCollector)),
        _spec("elbv2.target_health", _ELB, _elb(TargetHealthCollector)),
        # ecs
        _spec(
            "ecs.cluster",
            ("ecs_service_name",),
            lambda sm, api, c: EcsClusterCollector(
                sm, ecs_service_name=_svc(api, "ecs_service_name"), **c
            ),
        ),
        _spec(
            "ecs.service",
            ("ecs_service_name", "elbv2_service_name", "ec2_service_name"),
            lambda sm, api, c: EcsServiceCollector(
                sm,
                ecs_service_name=_svc(api, "ecs_service_name"),
                elbv2_service_name=_svc(api, "elbv2_service_name"),
                ec2_service_name=_svc(api, "ec2_service_name"),
                **c,
            ),
        ),
        _spec(
            "ecs.task_definition",
            ("ecs_service_name",),
            lambda sm, api, c: EcsTaskDefinitionCollector(
                sm, ecs_service_name=_svc(api, "ecs_service_name"), **c
            ),
        ),
        _spec(
            "ecs.task",
            _ECS,
            lambda sm, api, c: EcsTaskCollector(
                sm,
                ecs_service_name=_svc(api, "ecs_service_name"),
                ec2_service_name=_svc(api, "ec2_service_name"),
                **c,
            ),
        ),
        # eks
        _spec("eks.cluster", _EKS, _eks(EksClusterCollector)),
        _spec("eks.node_group", _EKS, _eks(EksNodeGroupCollector)),
        _spec("eks.fargate_profile", _EKS, _eks(EksFargateProfileCollector)),
        # rds
        _spec("rds.db_instance", _RDS, _rds(RdsInstanceCollector)),
        _spec("rds.db_cluster", _RDS, _rds(RdsClusterCollector)),
        _spec("rds.db_subnet_group", _RDS, _rds(RdsDbSubnetGroupCollector)),
        # serverless / edge
        _spec(
            "lambda.function",
            ("lambda_service_name", "ec2_service_name"),
            lambda sm, api, c: LambdaFunctionCollector(
                sm,
                lambda_service_name=_svc(api, "lambda_service_name"),
                ec2_service_name=_svc(api, "ec2_service_name"),
                **c,
            ),
        ),
        _spec(
            "apigateway.rest_api",
            ("apigateway_service_name",),
            lambda sm, api, c: ApiGatewayRestApiCollector(
                sm, apigateway_service_name=_svc(api, "apigateway_service_name"), **c
            ),
        ),
        _spec(
            "apigatewayv2.api",
            ("apigatewayv2_service_name",),
            lambda sm, api, c: ApiGatewayV2ApiCollector(
                sm, apigatewayv2_service_name=_svc(api, "apigatewayv2_service_name"), **c
            ),
        ),
        _spec(
            "cloudfront.distribution",
            ("cloudfront_service_name",),
            lambda sm, api, c: CloudFrontDistributionCollector(
                sm,
                cloudfront_service_name=_svc(api, "cloudfront_service_name"),
                global_control_region=api.global_control_region,
                **c,
            ),
            is_global=True,
        ),
        _spec(
            "s3.bucket",
            ("s3_service_name",),
            lambda sm, api, c: S3BucketCollector(
                sm,
                s3_service_name=_svc(api, "s3_service_name"),
                s3_default_region=api.s3_default_region,
                s3_location_constraint_map=api.s3_location_constraint_map,
                **c,
            ),
        ),
        _spec(
            "wafv2.web_acl_regional", ("wafv2_service_name",), _waf(Wafv2RegionalWebAclCollector)
        ),
        _spec(
            "wafv2.web_acl_cloudfront",
            ("wafv2_service_name",),
            _waf(Wafv2CloudFrontWebAclCollector),
            is_global=True,
        ),
        _spec(
            "route53.hosted_zone",
            ("route53_service_name",),
            _r53(Route53HostedZoneCollector),
            is_global=True,
        ),
        _spec(
            "route53.record",
            ("route53_service_name",),
            _r53(Route53RecordCollector),
            is_global=True,
        ),
        _spec(
            "logs.log_group",
            ("logs_service_name",),
            # DescribeLogGroups accepts limit <= 50.
            lambda sm, api, c: CloudWatchLogGroupCollector(
                sm,
                logs_service_name=_svc(api, "logs_service_name"),
                **{**c, "page_size": min(int(c.get("page_size", 50)), 50)},
            ),
        ),
    ]
}


def build_collector(
    name: str,
    session_manager: AWSSessionManager,
    aws_api: AwsApiConfig,
    **common: Any,
) -> PaginatedCollector:
    """Build a configured collector; ``common`` may hold page_size, max_retries,
    client_factory and sleep (``None`` values are dropped)."""
    spec = COLLECTOR_SPECS.get(name)
    if spec is None:
        raise ValueError(
            f"Unknown collector '{name}'. Known collectors: {', '.join(sorted(COLLECTOR_SPECS))}"
        )
    missing = [
        f for f in spec.required_service_fields if not (getattr(aws_api, f, None) or "").strip()
    ]
    if missing:
        raise ValueError(
            f"Collector '{name}' requires aws_api.{', aws_api.'.join(missing)} "
            "in the accounts configuration"
        )
    return spec.build(
        session_manager, aws_api, **{k: v for k, v in common.items() if v is not None}
    )
