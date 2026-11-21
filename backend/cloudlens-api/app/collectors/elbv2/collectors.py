"""ELBv2 collectors: load balancers, listeners, target groups, target health."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from app.collectors.aws_api import (
    AwsDescribeCollector,
    compact_edges,
    edge,
    tag_name,
    tags_to_dict,
)
from app.collectors.base import PaginatedCollector
from app.collectors.normalized import (
    ANY_RESOURCE_TYPE,
    NormalizedResource,
    ResourceEdge,
    ResourcePage,
)
from app.connectors.aws.session import AWSSessionManager


def _parse_dt(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=UTC)
        return dt.isoformat()
    return str(value)


def _classify_target(target_id: Any, target_group_type: Any) -> str | None:
    """Target type from the target group's TargetType, else from the Id shape."""
    if target_group_type in {"instance", "ip", "lambda", "alb"}:
        return str(target_group_type)
    if not isinstance(target_id, str):
        return None
    if target_id.startswith("i-"):
        return "instance"
    if target_id.startswith("arn:") and ":function:" in target_id:
        return "lambda"
    if target_id.startswith("arn:") and ":loadbalancer/app/" in target_id:
        return "alb"
    if target_id[:1].isdigit() or ":" in target_id:
        return "ip"
    return None


def _resource_id_from_arn(arn: str) -> str:
    # Prefer full ARN as identity when available; callers set arn field.
    return arn


class LoadBalancerCollector(AwsDescribeCollector):
    """Collect ALB/NLB load balancers including scheme and VPC mapping."""

    name = "elbv2.load_balancer"
    resource_type = "load-balancer"
    describe_method = "describe_load_balancers"
    result_key = "LoadBalancers"
    token_param = "Marker"
    token_response_key = "NextMarker"
    page_size_param = "PageSize"
    source = "aws.elbv2"

    def __init__(
        self,
        session_manager: AWSSessionManager,
        *,
        elbv2_service_name: str,
        ec2_service_name: str,
        **kwargs: Any,
    ):
        super().__init__(session_manager, service_name=elbv2_service_name, **kwargs)
        self.source = "aws.elbv2"
        self._ec2_service_name = ec2_service_name.strip()

    def normalize(
        self, raw: Mapping[str, Any], *, account_id: str, region: str
    ) -> NormalizedResource:
        arn = str(raw["LoadBalancerArn"])
        resource_id = _resource_id_from_arn(arn)
        tags = tags_to_dict(raw.get("Tags"))
        name = raw.get("LoadBalancerName") or tag_name(tags)
        vpc_id = raw.get("VpcId")
        state = (raw.get("State") or {}).get("Code")
        net = self._ec2_service_name
        relationships = compact_edges(
            edge(
                relationship_type="belongs_to",
                account_id=account_id,
                region=region,
                service=net,
                resource_type="vpc",
                resource_id=vpc_id if isinstance(vpc_id, str) else None,
            )
        )
        azs = [
            {
                "zone_name": az.get("ZoneName"),
                "subnet_id": az.get("SubnetId"),
            }
            for az in (raw.get("AvailabilityZones") or [])
            if isinstance(az, Mapping)
        ]
        for az in azs:
            relationships.extend(
                compact_edges(
                    edge(
                        relationship_type="associated_with",
                        account_id=account_id,
                        region=region,
                        service=net,
                        resource_type="subnet",
                        resource_id=az.get("subnet_id"),
                    )
                )
            )
        security_groups = [str(g) for g in (raw.get("SecurityGroups") or []) if g]
        for sg in security_groups:
            relationships.extend(
                compact_edges(
                    edge(
                        relationship_type="uses",
                        account_id=account_id,
                        region=region,
                        service=net,
                        resource_type="security-group",
                        resource_id=sg,
                    )
                )
            )

        created = raw.get("CreatedTime")
        discovered_at = created if isinstance(created, datetime) else datetime.now(UTC)
        if isinstance(discovered_at, datetime) and discovered_at.tzinfo is None:
            discovered_at = discovered_at.replace(tzinfo=UTC)

        return NormalizedResource(
            account_id=account_id,
            region=region,
            service=self.service,
            resource_type=self.resource_type,
            resource_id=resource_id,
            arn=arn,
            tags=tags,
            observed_state=self._base_state(
                account_id=account_id,
                region=region,
                resource_id=resource_id,
                name=name if isinstance(name, str) else None,
                status=state,
                load_balancer_arn=arn,
                dns_name=raw.get("DNSName"),
                canonical_hosted_zone_id=raw.get("CanonicalHostedZoneId"),
                scheme=raw.get("Scheme"),
                type=raw.get("Type"),
                ip_address_type=raw.get("IpAddressType"),
                vpc_id=vpc_id,
                state=state,
                availability_zones=azs,
                security_groups=security_groups,
                created_time=_parse_dt(raw.get("CreatedTime")),
                discovery_timestamp=datetime.now(UTC).isoformat(),
            ),
            relationships=relationships,
            discovered_at=discovered_at,
        )


class ListenerCollector(AwsDescribeCollector):
    """Collect listeners by paging load balancers and batching DescribeListeners."""

    name = "elbv2.listener"
    resource_type = "listener"
    describe_method = "describe_listeners"
    result_key = "Listeners"
    token_param = "Marker"
    token_response_key = "NextMarker"
    page_size_param = "PageSize"
    source = "aws.elbv2"

    def __init__(
        self,
        session_manager: AWSSessionManager,
        *,
        elbv2_service_name: str,
        ec2_service_name: str,
        **kwargs: Any,
    ):
        super().__init__(session_manager, service_name=elbv2_service_name, **kwargs)
        self.source = "aws.elbv2"
        self._ec2_service_name = ec2_service_name.strip()

    def fetch_page(
        self,
        *,
        account_id: str,
        region: str,
        next_token: str | None = None,
    ) -> ResourcePage:
        client = self._get_client(region)
        lb_params: dict[str, Any] = {self.page_size_param: self._page_size}
        if next_token:
            lb_params[self.token_param] = next_token
        lb_response = self._invoke(
            client, "describe_load_balancers", lb_params, region=region
        )
        items: list[NormalizedResource] = []
        for lb in lb_response.get("LoadBalancers") or []:
            if not isinstance(lb, Mapping) or not lb.get("LoadBalancerArn"):
                continue
            lb_arn = str(lb["LoadBalancerArn"])
            listener_marker: str | None = None
            while True:
                params: dict[str, Any] = {"LoadBalancerArn": lb_arn}
                if listener_marker:
                    params["Marker"] = listener_marker
                response = self._invoke(
                    client, self.describe_method, params, region=region
                )
                for raw in response.get(self.result_key) or []:
                    if isinstance(raw, Mapping):
                        items.append(
                            self.normalize(
                                raw,
                                account_id=account_id,
                                region=region,
                                load_balancer_arn=lb_arn,
                            )
                        )
                listener_marker = response.get("NextMarker")
                if not listener_marker:
                    break
        token = lb_response.get("NextMarker")
        return ResourcePage(items=items, next_token=str(token) if token else None)

    def normalize(
        self,
        raw: Mapping[str, Any],
        *,
        account_id: str,
        region: str,
        load_balancer_arn: str | None = None,
    ) -> NormalizedResource:
        arn = str(raw["ListenerArn"])
        lb_arn = load_balancer_arn or raw.get("LoadBalancerArn")
        default_actions = [
            {
                "type": action.get("Type"),
                "target_group_arn": action.get("TargetGroupArn"),
                "order": action.get("Order"),
            }
            for action in (raw.get("DefaultActions") or [])
            if isinstance(action, Mapping)
        ]
        relationships = compact_edges(
            edge(
                relationship_type="belongs_to",
                account_id=account_id,
                region=region,
                service=self.service,
                resource_type="load-balancer",
                resource_id=str(lb_arn) if lb_arn else None,
            )
        )
        for action in default_actions:
            relationships.extend(
                compact_edges(
                    edge(
                        relationship_type="forwards_to",
                        account_id=account_id,
                        region=region,
                        service=self.service,
                        resource_type="target-group",
                        resource_id=action.get("target_group_arn"),
                    )
                )
            )
        return NormalizedResource(
            account_id=account_id,
            region=region,
            service=self.service,
            resource_type=self.resource_type,
            resource_id=arn,
            arn=arn,
            observed_state=self._base_state(
                account_id=account_id,
                region=region,
                resource_id=arn,
                status="active",
                listener_arn=arn,
                load_balancer_arn=lb_arn,
                port=raw.get("Port"),
                protocol=raw.get("Protocol"),
                ssl_policy=raw.get("SslPolicy"),
                default_actions=default_actions,
                discovery_timestamp=datetime.now(UTC).isoformat(),
            ),
            relationships=relationships,
        )


class TargetGroupCollector(AwsDescribeCollector):
    """Collect ELBv2 target groups including VPC mapping."""

    name = "elbv2.target_group"
    resource_type = "target-group"
    describe_method = "describe_target_groups"
    result_key = "TargetGroups"
    token_param = "Marker"
    token_response_key = "NextMarker"
    page_size_param = "PageSize"
    source = "aws.elbv2"

    def __init__(
        self,
        session_manager: AWSSessionManager,
        *,
        elbv2_service_name: str,
        ec2_service_name: str,
        **kwargs: Any,
    ):
        super().__init__(session_manager, service_name=elbv2_service_name, **kwargs)
        self.source = "aws.elbv2"
        self._ec2_service_name = ec2_service_name.strip()

    def normalize(
        self, raw: Mapping[str, Any], *, account_id: str, region: str
    ) -> NormalizedResource:
        arn = str(raw["TargetGroupArn"])
        vpc_id = raw.get("VpcId")
        lb_arns = [str(a) for a in (raw.get("LoadBalancerArns") or []) if a]
        relationships = compact_edges(
            edge(
                relationship_type="belongs_to",
                account_id=account_id,
                region=region,
                service=self._ec2_service_name,
                resource_type="vpc",
                resource_id=vpc_id if isinstance(vpc_id, str) else None,
            )
        )
        for lb_arn in lb_arns:
            relationships.extend(
                compact_edges(
                    edge(
                        relationship_type="associated_with",
                        account_id=account_id,
                        region=region,
                        service=self.service,
                        resource_type="load-balancer",
                        resource_id=lb_arn,
                    )
                )
            )
        return NormalizedResource(
            account_id=account_id,
            region=region,
            service=self.service,
            resource_type=self.resource_type,
            resource_id=arn,
            arn=arn,
            observed_state=self._base_state(
                account_id=account_id,
                region=region,
                resource_id=arn,
                name=raw.get("TargetGroupName"),
                status="active",
                target_group_arn=arn,
                protocol=raw.get("Protocol"),
                port=raw.get("Port"),
                vpc_id=vpc_id,
                target_type=raw.get("TargetType"),
                health_check_protocol=raw.get("HealthCheckProtocol"),
                health_check_path=raw.get("HealthCheckPath"),
                health_check_enabled=raw.get("HealthCheckEnabled"),
                matcher=(raw.get("Matcher") or {}).get("HttpCode"),
                load_balancer_arns=lb_arns,
                discovery_timestamp=datetime.now(UTC).isoformat(),
            ),
            relationships=relationships,
        )


class TargetHealthCollector(AwsDescribeCollector):
    """Collect target health by paging target groups and batching health calls."""

    name = "elbv2.target_health"
    resource_type = "target-health"
    describe_method = "describe_target_health"
    result_key = "TargetHealthDescriptions"
    token_param = "Marker"
    token_response_key = "NextMarker"
    page_size_param = "PageSize"
    source = "aws.elbv2"

    def __init__(
        self,
        session_manager: AWSSessionManager,
        *,
        elbv2_service_name: str,
        ec2_service_name: str,
        **kwargs: Any,
    ):
        super().__init__(session_manager, service_name=elbv2_service_name, **kwargs)
        self.source = "aws.elbv2"
        self._ec2_service_name = ec2_service_name.strip()

    def fetch_page(
        self,
        *,
        account_id: str,
        region: str,
        next_token: str | None = None,
    ) -> ResourcePage:
        client = self._get_client(region)
        tg_params: dict[str, Any] = {self.page_size_param: self._page_size}
        if next_token:
            tg_params[self.token_param] = next_token
        tg_response = self._invoke(
            client, "describe_target_groups", tg_params, region=region
        )
        items: list[NormalizedResource] = []
        for tg in tg_response.get("TargetGroups") or []:
            if not isinstance(tg, Mapping) or not tg.get("TargetGroupArn"):
                continue
            tg_arn = str(tg["TargetGroupArn"])
            # Soft: one denied/deleted target group must not abort the page.
            response = self._invoke_soft(
                client,
                self.describe_method,
                {"TargetGroupArn": tg_arn},
                region=region,
                context=f"target_group={tg_arn}",
            )
            if response is None:
                continue
            for raw in response.get(self.result_key) or []:
                if isinstance(raw, Mapping):
                    items.append(
                        self.normalize(
                            raw,
                            account_id=account_id,
                            region=region,
                            target_group_arn=tg_arn,
                            target_group=tg,
                        )
                    )
        token = tg_response.get("NextMarker")
        return ResourcePage(items=items, next_token=str(token) if token else None)

    def normalize(
        self,
        raw: Mapping[str, Any],
        *,
        account_id: str,
        region: str,
        target_group_arn: str | None = None,
        target_group: Mapping[str, Any] | None = None,
    ) -> NormalizedResource:
        target = raw.get("Target") or {}
        target_id = target.get("Id")
        port = target.get("Port")
        availability_zone = target.get("AvailabilityZone")
        health = raw.get("TargetHealth") or {}
        status = health.get("State")
        tg_arn = target_group_arn or ""
        # Canonical ID: target-group + target id + port
        resource_id = f"{tg_arn}:{target_id}:{port}"
        tg = target_group if isinstance(target_group, Mapping) else {}
        target_type = _classify_target(target_id, tg.get("TargetType"))
        relationships = compact_edges(
            edge(
                relationship_type="belongs_to",
                account_id=account_id,
                region=region,
                service=self.service,
                resource_type="target-group",
                resource_id=tg_arn or None,
            ),
        )
        for rel in relationships:
            rel.evidence = "DescribeTargetHealth:TargetGroupArn"
        if target_type == "instance" and isinstance(target_id, str):
            relationships.append(
                ResourceEdge(
                    relationship_type="targets",
                    to_account_id=account_id,
                    to_region=region,
                    to_service=self._ec2_service_name,
                    to_resource_type="instance",
                    to_resource_id=target_id,
                    evidence="DescribeTargetHealth:Target.Id",
                )
            )
        elif target_type in {"lambda", "alb"} and isinstance(target_id, str):
            # Target.Id is the function / load balancer ARN.
            relationships.append(
                ResourceEdge(
                    relationship_type="targets",
                    to_account_id=account_id,
                    to_region=region,
                    to_service="*",
                    to_resource_type=ANY_RESOURCE_TYPE,
                    to_resource_id=target_id,
                    to_arn=target_id,
                    evidence="DescribeTargetHealth:Target.Id",
                )
            )
        # IP targets: no edge. The registered IP is recorded; any match to an
        # ENI is an inference made (and labelled) by the graph builder.

        return NormalizedResource(
            account_id=account_id,
            region=region,
            service=self.service,
            resource_type=self.resource_type,
            resource_id=resource_id,
            observed_state=self._base_state(
                account_id=account_id,
                region=region,
                resource_id=resource_id,
                status=status,
                target_group_arn=tg_arn,
                target_id=target_id,
                target_type=target_type,
                target_port=port,
                target_availability_zone=availability_zone,
                target_group_vpc_id=tg.get("VpcId"),
                port=port,
                availability_zone=availability_zone,
                health_state=status,
                health_reason=health.get("Reason"),
                health_description=health.get("Description"),
                discovery_timestamp=datetime.now(UTC).isoformat(),
            ),
            relationships=relationships,
        )


def create_elbv2_collectors(
    session_manager: AWSSessionManager,
    *,
    elbv2_service_name: str,
    ec2_service_name: str,
    page_size: int = 100,
    max_retries: int = 5,
) -> list[PaginatedCollector]:
    common = {
        "elbv2_service_name": elbv2_service_name,
        "ec2_service_name": ec2_service_name,
        "page_size": page_size,
        "max_retries": max_retries,
    }
    return [
        LoadBalancerCollector(session_manager, **common),
        ListenerCollector(session_manager, **common),
        TargetGroupCollector(session_manager, **common),
        TargetHealthCollector(session_manager, **common),
    ]


def _rule_conditions(raw_conditions: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for cond in raw_conditions or []:
        if not isinstance(cond, Mapping):
            continue
        field = cond.get("Field")
        values = [str(v) for v in (cond.get("Values") or []) if v is not None]
        # Typed configs (HostHeaderConfig, PathPatternConfig, ...) carry Values too.
        for key, cfg in cond.items():
            if key.endswith("Config") and isinstance(cfg, Mapping):
                values.extend(str(v) for v in (cfg.get("Values") or []) if v is not None)
        out.append({"field": field, "values": sorted(set(values))})
    return out


def _action_target_groups(action: Mapping[str, Any]) -> list[tuple[str, Any]]:
    """(target_group_arn, weight) pairs from a forward action, incl. ForwardConfig."""
    pairs: list[tuple[str, Any]] = []
    direct = action.get("TargetGroupArn")
    if direct:
        pairs.append((str(direct), None))
    forward = action.get("ForwardConfig") or {}
    if isinstance(forward, Mapping):
        for group in forward.get("TargetGroups") or []:
            if isinstance(group, Mapping) and group.get("TargetGroupArn"):
                arn = str(group["TargetGroupArn"])
                pairs = [p for p in pairs if p[0] != arn]
                pairs.append((arn, group.get("Weight")))
    return pairs


class ListenerRuleCollector(AwsDescribeCollector):
    """Collect listener rules: LB page -> listeners -> rules (all paginated).

    Records rule conditions and forward targets so the graph can show
    Listener -> Rule -> Target Group. Default rules are included (IsDefault).
    """

    name = "elbv2.listener_rule"
    resource_type = "listener-rule"
    describe_method = "describe_rules"
    result_key = "Rules"
    token_param = "Marker"
    token_response_key = "NextMarker"
    page_size_param = "PageSize"
    source = "aws.elbv2"

    def __init__(
        self,
        session_manager: AWSSessionManager,
        *,
        elbv2_service_name: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(session_manager, service_name=elbv2_service_name, **kwargs)
        self.source = "aws.elbv2"

    def fetch_page(
        self,
        *,
        account_id: str,
        region: str,
        next_token: str | None = None,
    ) -> ResourcePage:
        client = self._get_client(region)
        lb_params: dict[str, Any] = {"PageSize": min(self._page_size, 400)}
        if next_token:
            lb_params["Marker"] = next_token
        lb_response = self._invoke(
            client, "describe_load_balancers", lb_params, region=region
        )
        items: list[NormalizedResource] = []
        for lb in lb_response.get("LoadBalancers") or []:
            if not isinstance(lb, Mapping) or not lb.get("LoadBalancerArn"):
                continue
            lb_arn = str(lb["LoadBalancerArn"])
            for listener_arn in self._listener_arns(client, lb_arn, region=region):
                for raw in self._rules(client, listener_arn, region=region):
                    items.append(
                        self.normalize(
                            raw,
                            account_id=account_id,
                            region=region,
                            listener_arn=listener_arn,
                            load_balancer_arn=lb_arn,
                        )
                    )
        token = lb_response.get("NextMarker")
        return ResourcePage(items=items, next_token=str(token) if token else None)

    def _listener_arns(self, client: Any, lb_arn: str, *, region: str) -> list[str]:
        arns: list[str] = []
        marker: str | None = None
        while True:
            params: dict[str, Any] = {"LoadBalancerArn": lb_arn}
            if marker:
                params["Marker"] = marker
            response = self._invoke_soft(
                client, "describe_listeners", params, region=region,
                context=f"listeners lb={lb_arn}",
            )
            if response is None:
                return arns
            arns.extend(
                str(item["ListenerArn"])
                for item in response.get("Listeners") or []
                if isinstance(item, Mapping) and item.get("ListenerArn")
            )
            marker = response.get("NextMarker")
            if not marker:
                return arns

    def _rules(self, client: Any, listener_arn: str, *, region: str) -> list[Mapping[str, Any]]:
        rules: list[Mapping[str, Any]] = []
        marker: str | None = None
        while True:
            params: dict[str, Any] = {
                "ListenerArn": listener_arn,
                "PageSize": min(self._page_size, 400),
            }
            if marker:
                params["Marker"] = marker
            response = self._invoke_soft(
                client, "describe_rules", params, region=region,
                context=f"rules listener={listener_arn}",
            )
            if response is None:
                return rules
            rules.extend(r for r in response.get("Rules") or [] if isinstance(r, Mapping))
            marker = response.get("NextMarker")
            if not marker:
                return rules

    def normalize(
        self,
        raw: Mapping[str, Any],
        *,
        account_id: str,
        region: str,
        listener_arn: str | None = None,
        load_balancer_arn: str | None = None,
    ) -> NormalizedResource:
        arn = str(raw["RuleArn"])
        actions: list[dict[str, Any]] = []
        relationships: list[ResourceEdge] = []
        if listener_arn:
            relationships.append(
                ResourceEdge(
                    relationship_type="belongs_to",
                    to_account_id=account_id,
                    to_region=region,
                    to_service=self.service,
                    to_resource_type="listener",
                    to_resource_id=listener_arn,
                    to_arn=listener_arn,
                    evidence="DescribeRules:ListenerArn(request)",
                )
            )
        seen_tgs: set[str] = set()
        for action in raw.get("Actions") or []:
            if not isinstance(action, Mapping):
                continue
            pairs = _action_target_groups(action)
            actions.append(
                {
                    "type": action.get("Type"),
                    "order": action.get("Order"),
                    "target_group_arns": [arn_ for arn_, _ in pairs],
                    "weights": {arn_: w for arn_, w in pairs if w is not None},
                }
            )
            for tg_arn, weight in pairs:
                if tg_arn in seen_tgs:
                    continue
                seen_tgs.add(tg_arn)
                relationships.append(
                    ResourceEdge(
                        relationship_type="forwards_to",
                        to_account_id=account_id,
                        to_region=region,
                        to_service=self.service,
                        to_resource_type="target-group",
                        to_resource_id=tg_arn,
                        to_arn=tg_arn,
                        evidence=(
                            "DescribeRules:Actions[].ForwardConfig.TargetGroups[].TargetGroupArn"
                            if weight is not None
                            else "DescribeRules:Actions[].TargetGroupArn"
                        ),
                    )
                )
        priority = raw.get("Priority")
        return NormalizedResource(
            account_id=account_id,
            region=region,
            service=self.service,
            resource_type=self.resource_type,
            resource_id=arn,
            arn=arn,
            observed_state=self._base_state(
                account_id=account_id,
                region=region,
                resource_id=arn,
                name=f"priority {priority}" if priority is not None else None,
                status="active",
                rule_arn=arn,
                listener_arn=listener_arn,
                load_balancer_arn=load_balancer_arn,
                priority=priority,
                is_default=bool(raw.get("IsDefault")),
                conditions=_rule_conditions(raw.get("Conditions")),
                actions=actions,
                discovery_timestamp=datetime.now(UTC).isoformat(),
            ),
            relationships=relationships,
        )
