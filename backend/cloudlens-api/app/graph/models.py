"""Architecture graph API contract (schema_version "1").

Every node is a persisted inventory resource (``kind="resource"``) or a target
that an AWS API record *references* but CloudLens did not collect
(``kind="external"``, e.g. a CloudFront custom origin domain or an IP target).
No node or edge is created without a source API record behind it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Confidence = Literal["verified", "inferred", "unknown"]
ViewKind = Literal["account", "vpc", "application", "service"]
NodeKind = Literal["resource", "external"]
GroupKind = Literal["account", "global", "region", "vpc", "subnet"]
EdgeCategory = Literal[
    "flow",  # request path: DNS -> CDN -> LB -> listener -> rule -> TG -> target
    "membership",  # listener of LB, task of service, nodegroup of cluster
    "network",  # placement: resource in subnet / VPC, ENI attachment
    "security",  # security-group / WAF association (never proof of traffic)
    "dependency",  # uses task definition, subnet group, volume, ...
]


class Evidence(BaseModel):
    """Where an edge came from. Always points at a stored API observation."""

    source_api: str | None = Field(
        default=None, description="AWS API and field, e.g. 'DescribeInstances:SubnetId'"
    )
    collector: str | None = None
    collector_version: str | None = None
    from_resource: str = Field(description="Node id of the resource whose record holds it")
    observed_at: datetime | None = Field(default=None, description="last_seen_at of that record")
    detail: str | None = Field(default=None, description="Human-readable matching rule")


class GraphNode(BaseModel):
    id: str = Field(description="Stable id: '<account>:<region>:<resource_type>:<identity_key>'")
    kind: NodeKind = "resource"
    resource_type: str
    service: str
    label: str
    account_id: str
    region: str
    resource_id: str
    arn: str | None = None
    status: str | None = None
    vpc_id: str | None = None
    subnet_id: str | None = Field(
        default=None, description="Set only when the resource is placed in exactly one subnet"
    )
    subnet_ids: list[str] = Field(default_factory=list)
    is_stale: bool = False
    last_seen_at: datetime | None = None
    source: str | None = None
    tags: dict[str, str] = Field(default_factory=dict)
    context_only: bool = Field(
        default=False, description="Included only to give context to the requested view"
    )
    parent_group: str | None = Field(default=None, description="Innermost GraphGroup id")


class GraphEdge(BaseModel):
    id: str
    source: str
    target: str
    relationship_type: str
    category: EdgeCategory
    confidence: Confidence
    evidence: list[Evidence] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)


class GraphGroup(BaseModel):
    """Containment box. VPCs and subnets are rendered as groups, not nodes;
    ``node_id`` points at the collected VPC/subnet resource for detail lookup
    (``None`` when only referenced, never collected)."""

    id: str
    kind: GroupKind
    label: str
    parent_id: str | None = None
    node_id: str | None = None


class CollectorCoverage(BaseModel):
    account_id: str
    region: str | None
    collector: str
    status: str
    finished_at: datetime | None = None
    resources_seen: int = 0
    error_count: int = 0


class MissingPermission(BaseModel):
    account_id: str
    region: str | None
    collector: str
    iam_action: str | None
    error_code: str | None
    message: str


class UnresolvedReference(BaseModel):
    from_node: str
    relationship_type: str
    target_type: str
    target_hint: str
    reason: Literal["not_collected", "ambiguous", "out_of_scope"]
    evidence: str | None = None


class Coverage(BaseModel):
    collectors: list[CollectorCoverage] = Field(default_factory=list)
    missing_permissions: list[MissingPermission] = Field(default_factory=list)
    unresolved_references: list[UnresolvedReference] = Field(default_factory=list)
    stale_resources: int = 0
    oldest_observation: datetime | None = None
    newest_observation: datetime | None = None


class GraphScope(BaseModel):
    project_alias: str
    account_ids: list[str]
    regions: list[str] = Field(default_factory=list)
    view: ViewKind = "account"
    vpc_id: str | None = None
    service: str | None = None
    seed: str | None = None
    application_tag: str | None = None
    include_inferred: bool = True
    include_stale: bool = False
    detail: Literal["standard", "full"] = Field(
        default="standard",
        description="'standard' hides route tables, NACLs, volumes, task definitions, log groups",
    )


class GraphStats(BaseModel):
    node_count: int
    edge_count: int
    verified_edges: int
    inferred_edges: int
    unknown_edges: int
    by_resource_type: dict[str, int]
    truncated: bool = False
    total_nodes_before_limit: int = 0


class GraphDocument(BaseModel):
    schema_version: Literal["1"] = "1"
    generated_at: datetime
    scope: GraphScope
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    groups: list[GraphGroup]
    stats: GraphStats
    coverage: Coverage
    assumptions: list[str]


class NodeDetail(BaseModel):
    node: GraphNode
    observed_state: dict[str, Any]
    inbound: list[GraphEdge]
    outbound: list[GraphEdge]


class VpcSummary(BaseModel):
    account_id: str
    region: str
    vpc_id: str
    label: str
    resource_count: int


class EntryPoint(BaseModel):
    node_id: str
    label: str
    resource_type: str
    account_id: str
    region: str


class AccountSummary(BaseModel):
    account_alias: str
    account_id: str
    regions: list[str]


class ProjectSummary(BaseModel):
    project_alias: str
    accounts: list[AccountSummary]
    vpcs: list[VpcSummary] = Field(default_factory=list)
    services: dict[str, int] = Field(default_factory=dict)
    entry_points: list[EntryPoint] = Field(default_factory=list)
    application_tags: dict[str, list[str]] = Field(default_factory=dict)
