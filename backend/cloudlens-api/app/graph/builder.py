"""Build a normalized NetworkX architecture graph from persisted inventory.

Accuracy rules enforced here:

* Nodes come only from persisted ``Resource`` rows of the accounts in scope,
  or are ``external`` placeholders for identifiers an API record *references*
  but CloudLens did not collect (registered IP targets, CloudFront custom
  origins, Route 53 alias destinations). Nothing else is ever created.
* Edges come from each resource's ``declared_edges`` (read from its API record
  by the collector) or from deterministic matching rules over two stored API
  records (e.g. a CloudFront origin domain equal to a load balancer DNSName).
  Every edge carries evidence pointing at the record(s) it came from.
* Target lookup by ID/ARN/name is refused when ambiguous; it never guesses.
* Shared VPC, subnet or security group membership never produces an edge
  between two workloads.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import networkx as nx

from app.collectors.normalized import ANY_RESOURCE_TYPE
from app.db.models import Resource
from app.graph.models import (
    Confidence,
    EdgeCategory,
    Evidence,
    GraphEdge,
    GraphNode,
    UnresolvedReference,
)

GLOBAL_RESOURCE_TYPES = frozenset(
    {"cloudfront-distribution", "route53-hosted-zone", "route53-record", "s3-bucket"}
)
# Containment types become layout groups, not nodes.
GROUP_RESOURCE_TYPES = frozenset({"vpc", "subnet"})
# Collapsed into TG -> target ``routes_to`` edges.
COLLAPSED_RESOURCE_TYPES = frozenset({"target-health"})

FLOW_RELATIONSHIPS = frozenset(
    {
        "forwards_to",
        "targets",
        "routes_to",
        "origin",
        "alias_to",
        "integrates_with",
        "exposed_by",
        "selects",
        "backs",
    }
)
MEMBERSHIP_RELATIONSHIPS = frozenset({"member_of", "contains", "scheduled_on", "runs"})
SECURITY_TARGET_TYPES = frozenset({"security-group", "network-acl"})
NETWORK_TARGET_TYPES = frozenset(
    {"vpc", "subnet", "route-table", "network-interface", "internet-gateway", "nat-gateway"}
)

_DNS_S3 = re.compile(
    r"^(?P<bucket>[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9])\.s3(?:[.-][a-z0-9-]+)*\.amazonaws\.com$"
)
_APIGW_STAGE_ARN = re.compile(r"^(?P<api>arn:[^:]+:apigateway:[^:]*::/restapis/[^/]+)/stages/.+$")
_DNS_APIGW = re.compile(r"^(?P<id>[a-z0-9]+)\.execute-api\.[a-z0-9-]+\.amazonaws\.com$")


def node_id(account_id: str, region: str, resource_type: str, identity_key: str) -> str:
    return f"{account_id}:{region}:{resource_type}:{identity_key}"


def normalize_dns(name: str | None) -> str | None:
    if not name or not isinstance(name, str):
        return None
    value = name.strip().lower().rstrip(".")
    if value.startswith("dualstack."):
        value = value[len("dualstack.") :]
    return value or None


def categorize(relationship_type: str, from_type: str, to_type: str) -> EdgeCategory:
    if relationship_type in FLOW_RELATIONSHIPS:
        return "flow"
    if relationship_type == "protects" or to_type in SECURITY_TARGET_TYPES:
        return "security"
    if relationship_type in MEMBERSHIP_RELATIONSHIPS:
        return "membership"
    if to_type in NETWORK_TARGET_TYPES or from_type in {"internet-gateway", "elastic-ip"}:
        return "network"
    if relationship_type == "belongs_to":
        return "membership"
    if relationship_type == "associated_with" and to_type == "load-balancer":
        return "membership"
    return "dependency"


@dataclass(slots=True)
class BuiltGraph:
    """NetworkX graph plus side information gathered while building."""

    graph: nx.MultiDiGraph
    rows: dict[str, Resource]
    unresolved: list[UnresolvedReference] = field(default_factory=list)
    account_ids: tuple[str, ...] = ()

    def node(self, nid: str) -> GraphNode:
        return self.graph.nodes[nid]["model"]  # type: ignore[no-any-return]

    def edges(self) -> Iterable[GraphEdge]:
        for _u, _v, data in self.graph.edges(data=True):
            yield data["model"]


class _Index:
    """(account, region, type, key) -> node ids, plus account-wide ARN / DNS maps."""

    def __init__(self) -> None:
        self.by_key: dict[tuple[str, str, str, str], set[str]] = defaultdict(set)
        self.by_arn: dict[tuple[str, str], set[str]] = defaultdict(set)
        self.by_dns: dict[tuple[str, str], set[str]] = defaultdict(set)
        self.s3_by_bucket: dict[tuple[str, str], set[str]] = defaultdict(set)
        self.apigw_by_id: dict[tuple[str, str], set[str]] = defaultdict(set)
        self.eni_by_ip: dict[tuple[str, str, str], set[str]] = defaultdict(set)

    def add(self, nid: str, row: Resource) -> None:
        state = row.observed_state or {}
        acct, region, rtype = row.aws_account_id, row.region, row.resource_type
        keys = {row.identity_key, row.resource_id}
        if row.arn:
            keys.add(row.arn)
            self.by_arn[(acct, row.arn)].add(nid)
        name = state.get("name")
        if isinstance(name, str) and name:
            keys.add(name)
        if rtype == "ecs-service" and state.get("cluster_arn") and name:
            keys.add(f"{state['cluster_arn']}/{name}")
        for key in keys:
            if key:
                self.by_key[(acct, region, rtype, str(key))].add(nid)

        if rtype == "load-balancer":
            self._dns(acct, state.get("dns_name"), nid)
        elif rtype == "cloudfront-distribution":
            self._dns(acct, state.get("domain_name"), nid)
        elif rtype in {"apigateway-rest-api", "apigatewayv2-api"}:
            for domain in state.get("endpoint_domains") or []:
                self._dns(acct, domain, nid)
            api_id = state.get("api_id") or row.resource_id
            if api_id:
                self.apigw_by_id[(acct, str(api_id))].add(nid)
        elif rtype == "s3-bucket":
            self.s3_by_bucket[(acct, row.resource_id)].add(nid)
        elif rtype == "network-interface":
            vpc = state.get("vpc_id")
            ips = {state.get("private_ip_address")}
            for entry in state.get("private_ip_addresses") or []:
                if isinstance(entry, dict):
                    ips.add(entry.get("private_ip"))
            for ip in ips:
                if ip and vpc:
                    self.eni_by_ip[(acct, str(vpc), str(ip))].add(nid)

    def _dns(self, acct: str, name: Any, nid: str) -> None:
        normalized = normalize_dns(name)
        if normalized:
            self.by_dns[(acct, normalized)].add(nid)


class GraphBuilder:
    """Turns ``Resource`` rows for a set of accounts into a ``BuiltGraph``."""

    def __init__(self, rows: Iterable[Resource], *, account_ids: Iterable[str]) -> None:
        self._accounts = tuple(sorted(set(account_ids)))
        # Account isolation: drop anything outside the authorized account set.
        self._rows = [r for r in rows if r.aws_account_id in self._accounts]
        self._graph = nx.MultiDiGraph()
        self._index = _Index()
        self._by_id: dict[str, Resource] = {}
        self._unresolved: list[UnresolvedReference] = []

    # ------------------------------------------------------------------ build
    def build(self) -> BuiltGraph:
        for row in self._rows:
            nid = node_id(row.aws_account_id, row.region, row.resource_type, row.identity_key)
            self._by_id[nid] = row
            self._index.add(nid, row)

        for nid, row in self._by_id.items():
            self._graph.add_node(nid, model=self._node_model(nid, row), row=row)

        for nid, row in self._by_id.items():
            for declared in row.declared_edges or []:
                self._add_declared(nid, row, declared)

        self._apply_placement()
        self._collapse_target_health()
        self._match_cloudfront_origins()
        self._match_route53_records()
        self._match_k8s_ingress()
        return BuiltGraph(
            graph=self._graph,
            rows=self._by_id,
            unresolved=self._unresolved,
            account_ids=self._accounts,
        )

    # ------------------------------------------------------------------ nodes
    @staticmethod
    def _node_model(nid: str, row: Resource) -> GraphNode:
        state = row.observed_state or {}
        tags = {str(k): str(v) for k, v in (row.tags or {}).items()}
        label = tags.get("Name") or state.get("name") or row.resource_id
        subnet_ids: list[str] = []
        if isinstance(state.get("subnet_id"), str):
            subnet_ids.append(state["subnet_id"])
        status = state.get("status") or state.get("state")
        return GraphNode(
            id=nid,
            kind="resource",
            resource_type=row.resource_type,
            service=row.service,
            label=str(label),
            account_id=row.aws_account_id,
            region=row.region,
            resource_id=row.resource_id,
            arn=row.arn,
            status=str(status) if status is not None else None,
            vpc_id=state.get("vpc_id") if isinstance(state.get("vpc_id"), str) else None,
            subnet_ids=subnet_ids,
            is_stale=bool(row.is_stale),
            last_seen_at=row.last_seen_at,
            source=row.source,
            tags=tags,
        )

    def _external(self, *, account_id: str, region: str, kind: str, value: str, label: str) -> str:
        nid = f"{account_id}:{region}:external-{kind}:{value}"
        if nid not in self._graph:
            self._graph.add_node(
                nid,
                model=GraphNode(
                    id=nid,
                    kind="external",
                    resource_type=f"external-{kind}",
                    service="external",
                    label=label,
                    account_id=account_id,
                    region=region,
                    resource_id=value,
                    status="not_collected",
                ),
                row=None,
            )
        return nid

    # ------------------------------------------------------------------ edges
    def _evidence(
        self,
        from_nid: str,
        row: Resource,
        *,
        source_api: str | None,
        collector: str | None = None,
        collector_version: str | None = None,
        detail: str | None = None,
    ) -> Evidence:
        return Evidence(
            source_api=source_api,
            collector=collector,
            collector_version=collector_version or row.collector_version,
            from_resource=from_nid,
            observed_at=row.last_seen_at,
            detail=detail,
        )

    def add_edge(
        self,
        source: str,
        target: str,
        relationship_type: str,
        *,
        confidence: Confidence,
        evidence: list[Evidence],
        attributes: dict[str, Any] | None = None,
    ) -> None:
        if source == target:
            return
        src_type = self._graph.nodes[source]["model"].resource_type
        dst_type = self._graph.nodes[target]["model"].resource_type
        key = f"{source}|{relationship_type}|{target}"
        if self._graph.has_edge(source, target, key=key):
            # Same relationship from a second record: merge evidence, keep the
            # strongest confidence.
            existing: GraphEdge = self._graph.edges[source, target, key]["model"]
            existing.evidence.extend(evidence)
            if confidence == "verified":
                existing.confidence = "verified"
            return
        self._graph.add_edge(
            source,
            target,
            key=key,
            model=GraphEdge(
                id=key,
                source=source,
                target=target,
                relationship_type=relationship_type,
                category=categorize(relationship_type, src_type, dst_type),
                confidence=confidence,
                evidence=evidence,
                attributes=attributes or {},
            ),
        )

    def _resolve(self, declared: dict[str, Any]) -> tuple[set[str], str]:
        acct = str(declared.get("to_account_id") or "")
        if acct not in self._accounts:
            return set(), "out_of_scope"
        rtype = str(declared.get("to_resource_type") or "")
        rid = str(declared.get("to_resource_id") or "")
        arn = declared.get("to_arn")
        if rtype == ANY_RESOURCE_TYPE:
            target_arn = str(arn or rid)
            found = set(self._index.by_arn.get((acct, target_arn), set()))
            stage = _APIGW_STAGE_ARN.match(target_arn)
            if not found and stage:
                # WAF associates REST API *stages*; the API itself is the node.
                found = set(self._index.by_arn.get((acct, stage.group("api")), set()))
        else:
            region = str(declared.get("to_region") or "")
            found = set(self._index.by_key.get((acct, region, rtype, rid), set()))
            if arn:
                found |= self._index.by_key.get((acct, region, rtype, str(arn)), set())
        if len(found) > 1:
            return found, "ambiguous"
        return found, "not_collected" if not found else "ok"

    def _add_declared(self, nid: str, row: Resource, declared: dict[str, Any]) -> None:
        found, status = self._resolve(declared)
        rel = str(declared.get("relationship_type") or "related_to")
        if status != "ok":
            self._unresolved.append(
                UnresolvedReference(
                    from_node=nid,
                    relationship_type=rel,
                    target_type=str(declared.get("to_resource_type")),
                    target_hint=str(declared.get("to_arn") or declared.get("to_resource_id")),
                    reason=status,  # type: ignore[arg-type]
                    evidence=declared.get("evidence"),
                )
            )
            return
        (target,) = found
        confidence: Confidence = (
            "inferred" if declared.get("confidence") == "inferred" else "verified"
        )
        self.add_edge(
            nid,
            target,
            rel,
            confidence=confidence,
            evidence=[
                self._evidence(
                    nid,
                    row,
                    source_api=declared.get("evidence"),
                    collector=declared.get("collector"),
                    collector_version=declared.get("collector_version"),
                )
            ],
        )

    # ------------------------------------------------------------ placement
    def _apply_placement(self) -> None:
        """Derive vpc/subnet placement from API fields and verified network edges."""
        for nid in list(self._graph.nodes):
            model: GraphNode = self._graph.nodes[nid]["model"]
            if model.kind != "resource":
                continue
            subnets = set(model.subnet_ids)
            vpc = model.vpc_id
            for _, target, data in self._graph.out_edges(nid, data=True):
                edge: GraphEdge = data["model"]
                tmodel: GraphNode = self._graph.nodes[target]["model"]
                if edge.confidence != "verified":
                    continue
                if tmodel.resource_type == "subnet" and edge.relationship_type in {
                    "located_in",
                    "associated_with",
                }:
                    subnets.add(tmodel.resource_id)
                    vpc = vpc or self._vpc_of_subnet(target)
                elif tmodel.resource_type == "vpc" and edge.relationship_type in {
                    "belongs_to",
                    "attached_to",
                }:
                    vpc = vpc or tmodel.resource_id
            if model.resource_type == "subnet":
                vpc = vpc or model.vpc_id
                subnets = set()
            if model.resource_type == "vpc":
                vpc, subnets = model.resource_id, set()
            if not vpc and len(subnets) == 1:
                vpc = self._vpc_of_subnet_id(model.account_id, model.region, next(iter(subnets)))
            model.vpc_id = vpc
            model.subnet_ids = sorted(subnets)
            model.subnet_id = model.subnet_ids[0] if len(model.subnet_ids) == 1 else None

    def _vpc_of_subnet(self, subnet_nid: str) -> str | None:
        row = self._by_id.get(subnet_nid)
        if row is None:
            return None
        vpc = (row.observed_state or {}).get("vpc_id")
        return str(vpc) if vpc else None

    def _vpc_of_subnet_id(self, acct: str, region: str, subnet_id: str) -> str | None:
        found = self._index.by_key.get((acct, region, "subnet", subnet_id), set())
        return self._vpc_of_subnet(next(iter(found))) if len(found) == 1 else None

    # ----------------------------------------------------- derived edge rules
    def _collapse_target_health(self) -> None:
        """target-health record => TG --routes_to--> registered target."""
        for nid in [n for n, d in self._graph.nodes(data=True) if d["row"] is not None]:
            row: Resource = self._graph.nodes[nid]["row"]
            if row.resource_type != "target-health":
                continue
            state = row.observed_state or {}
            tg = self._first_out(nid, "belongs_to", "target-group")
            if tg is None:
                continue
            attrs = {
                "port": state.get("target_port"),
                "health_state": state.get("health_state") or state.get("status"),
            }
            targets = [t for t in self._outs(nid, "targets")]
            ev = self._evidence(
                nid, row, source_api="DescribeTargetHealth:TargetHealthDescriptions[].Target.Id"
            )
            if targets:
                for target in targets:
                    self.add_edge(
                        tg,
                        target,
                        "routes_to",
                        confidence="verified",
                        evidence=[ev],
                        attributes=attrs,
                    )
                continue
            target_id = str(state.get("target_id") or "")
            target_type = state.get("target_type")
            if not target_id:
                continue
            if target_type == "ip":
                self._route_ip_target(tg, nid, row, target_id, attrs)
            else:
                ext = self._external(
                    account_id=row.aws_account_id,
                    region=row.region,
                    kind="target",
                    value=target_id,
                    label=f"{target_type or 'target'} {target_id} (not collected)",
                )
                self.add_edge(
                    tg, ext, "routes_to", confidence="verified", evidence=[ev], attributes=attrs
                )

    def _route_ip_target(
        self, tg: str, th_nid: str, row: Resource, ip: str, attrs: dict[str, Any]
    ) -> None:
        tg_model: GraphNode = self._graph.nodes[tg]["model"]
        vpc = tg_model.vpc_id or (self._by_id[tg].observed_state or {}).get("vpc_id")
        enis = (
            self._index.eni_by_ip.get((row.aws_account_id, str(vpc), ip), set()) if vpc else set()
        )
        api_ev = self._evidence(th_nid, row, source_api="DescribeTargetHealth:Target.Id (ip)")
        if len(enis) == 1:
            (eni,) = enis
            eni_row = self._by_id[eni]
            self.add_edge(
                tg,
                eni,
                "routes_to",
                confidence="inferred",
                evidence=[
                    api_ev,
                    self._evidence(
                        eni,
                        eni_row,
                        source_api="DescribeNetworkInterfaces:PrivateIpAddresses",
                        detail=(
                            f"Registered IP {ip} equals this ENI's private IP in {vpc} at "
                            "collection time; IPs can be reused, so this is inferred."
                        ),
                    ),
                ],
                attributes=attrs,
            )
            return
        if len(enis) > 1:
            self._unresolved.append(
                UnresolvedReference(
                    from_node=tg,
                    relationship_type="routes_to",
                    target_type="network-interface",
                    target_hint=ip,
                    reason="ambiguous",
                    evidence=api_ev.source_api,
                )
            )
        ext = self._external(
            account_id=row.aws_account_id,
            region=row.region,
            kind="ip-target",
            value=ip,
            label=f"IP target {ip}",
        )
        self.add_edge(
            tg, ext, "routes_to", confidence="verified", evidence=[api_ev], attributes=attrs
        )

    def _resolve_dns(self, acct: str, dns: str) -> tuple[set[str], str]:
        """Return matching nodes and the rule used, from stored API records only."""
        found = set(self._index.by_dns.get((acct, dns), set()))
        if found:
            return found, "dns-name-equality"
        s3 = _DNS_S3.match(dns)
        if s3:
            return set(
                self._index.s3_by_bucket.get((acct, s3.group("bucket")), set())
            ), "s3-endpoint"
        api = _DNS_APIGW.match(dns)
        if api:
            return set(
                self._index.apigw_by_id.get((acct, api.group("id")), set())
            ), "apigw-endpoint"
        return set(), "none"

    def _match_cloudfront_origins(self) -> None:
        for nid, row in self._rows_of("cloudfront-distribution"):
            for origin in (row.observed_state or {}).get("origins") or []:
                if not isinstance(origin, dict):
                    continue
                domain = normalize_dns(origin.get("domain_name"))
                if not domain:
                    continue
                self._link_dns(
                    nid,
                    row,
                    domain,
                    "origin",
                    source_api="ListDistributions:Origins.Items[].DomainName",
                    attributes={
                        "origin_id": origin.get("id"),
                        "origin_path": origin.get("origin_path"),
                    },
                )

    def _match_route53_records(self) -> None:
        for nid, row in self._rows_of("route53-record"):
            state = row.observed_state or {}
            alias = state.get("alias_target")
            if isinstance(alias, dict) and alias.get("dns_name"):
                domain = normalize_dns(alias.get("dns_name"))
                if domain:
                    self._link_dns(
                        nid,
                        row,
                        domain,
                        "alias_to",
                        source_api="ListResourceRecordSets:AliasTarget.DNSName",
                    )
            for value in state.get("cname_values") or []:
                domain = normalize_dns(value)
                if domain:
                    self._link_dns(
                        nid,
                        row,
                        domain,
                        "alias_to",
                        source_api="ListResourceRecordSets:ResourceRecords[].Value (CNAME)",
                    )

    def _match_k8s_ingress(self) -> None:
        for nid, row in self._rows_of("k8s-ingress"):
            for host in (row.observed_state or {}).get("load_balancer_hostnames") or []:
                domain = normalize_dns(host)
                if not domain:
                    continue
                found, rule = self._resolve_dns(row.aws_account_id, domain)
                if len(found) == 1:
                    (lb,) = found
                    self.add_edge(
                        nid,
                        lb,
                        "exposed_by",
                        confidence="verified",
                        evidence=[
                            self._evidence(
                                nid,
                                row,
                                source_api="Ingress.status.loadBalancer.ingress[].hostname",
                            ),
                            self._evidence(
                                lb,
                                self._by_id[lb],
                                source_api="DescribeLoadBalancers:DNSName",
                                detail=f"Matched by {rule}",
                            ),
                        ],
                    )

    def _link_dns(
        self,
        nid: str,
        row: Resource,
        domain: str,
        rel: str,
        *,
        source_api: str,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        found, rule = self._resolve_dns(row.aws_account_id, domain)
        src_ev = self._evidence(nid, row, source_api=source_api)
        if len(found) == 1:
            (target,) = found
            target_row = self._by_id[target]
            self.add_edge(
                nid,
                target,
                rel,
                confidence="verified",
                evidence=[
                    src_ev,
                    self._evidence(
                        target,
                        target_row,
                        source_api=None,
                        detail=f"Configured name '{domain}' matched this resource by {rule}",
                    ),
                ],
                attributes=attributes,
            )
            return
        if len(found) > 1:
            self._unresolved.append(
                UnresolvedReference(
                    from_node=nid,
                    relationship_type=rel,
                    target_type="dns",
                    target_hint=domain,
                    reason="ambiguous",
                    evidence=source_api,
                )
            )
            return
        ext = self._external(
            account_id=row.aws_account_id,
            region=row.region,
            kind="endpoint",
            value=domain,
            label=domain,
        )
        self.add_edge(
            nid, ext, rel, confidence="verified", evidence=[src_ev], attributes=attributes
        )

    # ---------------------------------------------------------------- helpers
    def _rows_of(self, rtype: str) -> list[tuple[str, Resource]]:
        return [(n, r) for n, r in self._by_id.items() if r.resource_type == rtype]

    def _outs(self, nid: str, rel: str) -> list[str]:
        return [
            v
            for _, v, d in self._graph.out_edges(nid, data=True)
            if d["model"].relationship_type == rel
        ]

    def _first_out(self, nid: str, rel: str, rtype: str) -> str | None:
        for target in self._outs(nid, rel):
            if self._graph.nodes[target]["model"].resource_type == rtype:
                return target
        return None


def observation_range(rows: Iterable[Resource]) -> tuple[datetime | None, datetime | None]:
    stamps = [r.last_seen_at for r in rows if r.last_seen_at is not None]
    return (min(stamps), max(stamps)) if stamps else (None, None)
