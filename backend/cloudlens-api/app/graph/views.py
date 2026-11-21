"""View selection, grouping and truncation over a ``BuiltGraph``.

Views never add edges. They select a subset of nodes and the edges between
them. Application traversal deliberately does not pass *through* shared
infrastructure ("hub" types such as VPCs, subnets, security groups, clusters),
so two workloads are never connected just because they share one.
"""

from __future__ import annotations

from collections import Counter, deque
from collections.abc import Iterable

from app.graph.builder import (
    COLLAPSED_RESOURCE_TYPES,
    GLOBAL_RESOURCE_TYPES,
    GROUP_RESOURCE_TYPES,
    BuiltGraph,
)
from app.graph.models import GraphEdge, GraphGroup, GraphNode, GraphScope, GraphStats

HUB_RESOURCE_TYPES = frozenset(
    {
        "vpc",
        "subnet",
        "security-group",
        "route-table",
        "network-acl",
        "internet-gateway",
        "nat-gateway",
        "rds-db-subnet-group",
        "ecs-cluster",
        "eks-cluster",
        "ecs-task-definition",
        "route53-hosted-zone",
        "wafv2-web-acl",
        "logs-log-group",
    }
)
TRAVERSAL_CATEGORIES = frozenset({"flow", "membership", "dependency", "network"})
STANDARD_HIDDEN_TYPES = frozenset(
    {"route-table", "network-acl", "volume", "ecs-task-definition", "logs-log-group"}
)
ENTRY_POINT_TYPES = (
    "route53-record",
    "cloudfront-distribution",
    "apigateway-rest-api",
    "apigatewayv2-api",
    "load-balancer",
    "k8s-ingress",
)
# Lower = kept first when a view must be truncated.
_TYPE_PRIORITY = {t: i for i, t in enumerate(ENTRY_POINT_TYPES)} | {
    "listener": 10,
    "listener-rule": 11,
    "target-group": 12,
    "ecs-service": 13,
    "lambda-function": 14,
    "eks-nodegroup": 15,
    "instance": 16,
    "ecs-task": 17,
    "rds-db-cluster": 18,
    "rds-db-instance": 19,
}
APPLICATION_MAX_DEPTH = 8


def _visible(model: GraphNode, scope: GraphScope) -> bool:
    if model.resource_type in GROUP_RESOURCE_TYPES | COLLAPSED_RESOURCE_TYPES:
        return False
    if model.is_stale and not scope.include_stale:
        return False
    if scope.detail == "standard" and model.resource_type in STANDARD_HIDDEN_TYPES:
        return False
    if scope.regions and model.region not in scope.regions and model.kind == "resource":
        return model.resource_type in GLOBAL_RESOURCE_TYPES
    return True


def _edge_ok(edge: GraphEdge, scope: GraphScope) -> bool:
    return scope.include_inferred or edge.confidence == "verified"


def select_nodes(built: BuiltGraph, scope: GraphScope) -> tuple[set[str], set[str]]:
    """Return (primary node ids, context-only node ids) for the scope's view."""
    g = built.graph
    visible = {n for n in g.nodes if _visible(g.nodes[n]["model"], scope)}

    if scope.view == "account":
        return visible, set()

    if scope.view == "vpc":
        primary = {n for n in visible if g.nodes[n]["model"].vpc_id == scope.vpc_id}
        return primary, _one_hop_context(built, primary, visible, scope)

    if scope.view == "service":
        wanted = (scope.service or "").lower()
        primary = {
            n
            for n in visible
            if wanted
            and (
                g.nodes[n]["model"].service.lower() == wanted
                or g.nodes[n]["model"].resource_type.lower().startswith(wanted)
            )
        }
        return primary, _one_hop_context(built, primary, visible, scope)

    # application view
    seeds: set[str] = set()
    if scope.seed and scope.seed in g:
        seeds.add(scope.seed)
    if scope.application_tag and "=" in scope.application_tag:
        key, value = scope.application_tag.split("=", 1)
        seeds |= {n for n in visible if g.nodes[n]["model"].tags.get(key) == value}
    primary = _traverse(built, seeds & visible | (seeds if scope.seed else set()), visible, scope)
    return primary, _one_hop_context(built, primary, visible, scope, hubs_only=True)


def _neighbors(built: BuiltGraph, nid: str, scope: GraphScope) -> Iterable[tuple[str, GraphEdge]]:
    g = built.graph
    for _, v, d in g.out_edges(nid, data=True):
        yield v, d["model"]
    for u, _, d in g.in_edges(nid, data=True):
        yield u, d["model"]


def _traverse(built: BuiltGraph, seeds: set[str], visible: set[str], scope: GraphScope) -> set[str]:
    g = built.graph
    seen = set(seeds)
    queue = deque((s, 0) for s in seeds)
    while queue:
        nid, depth = queue.popleft()
        if depth >= APPLICATION_MAX_DEPTH:
            continue
        if nid not in seeds and g.nodes[nid]["model"].resource_type in HUB_RESOURCE_TYPES:
            continue  # include hubs, never expand through them
        for other, edge in _neighbors(built, nid, scope):
            if other in seen or other not in visible:
                continue
            if edge.category not in TRAVERSAL_CATEGORIES or not _edge_ok(edge, scope):
                continue
            if g.nodes[other]["model"].resource_type in HUB_RESOURCE_TYPES:
                continue  # hubs are added as context afterwards
            seen.add(other)
            queue.append((other, depth + 1))
    return seen


def _one_hop_context(
    built: BuiltGraph,
    primary: set[str],
    visible: set[str],
    scope: GraphScope,
    *,
    hubs_only: bool = False,
) -> set[str]:
    g = built.graph
    context: set[str] = set()
    for nid in primary:
        for other, edge in _neighbors(built, nid, scope):
            if other in primary or other not in visible or not _edge_ok(edge, scope):
                continue
            if edge.category == "security" and not hubs_only:
                continue
            rtype = g.nodes[other]["model"].resource_type
            if hubs_only and rtype not in HUB_RESOURCE_TYPES:
                continue
            context.add(other)
    return context


def truncate(
    built: BuiltGraph, primary: set[str], context: set[str], max_nodes: int
) -> tuple[set[str], set[str], bool, int]:
    total = len(primary) + len(context)
    if total <= max_nodes:
        return primary, context, False, total
    g = built.graph

    def rank(n: str) -> tuple[int, str]:
        return (_TYPE_PRIORITY.get(g.nodes[n]["model"].resource_type, 50), n)

    kept = set(sorted(primary, key=rank)[:max_nodes])
    room = max_nodes - len(kept)
    kept_ctx = set(sorted(context, key=rank)[:room]) if room > 0 else set()
    return kept, kept_ctx, True, total


def build_groups(nodes: list[GraphNode], built: BuiltGraph) -> list[GraphGroup]:
    """account > (global | region > vpc > subnet); sets ``node.parent_group``."""
    groups: dict[str, GraphGroup] = {}
    rows = built.rows

    def collected(acct: str, region: str, rtype: str, rid: str) -> tuple[str | None, str]:
        nid = f"{acct}:{region}:{rtype}:{rid}"
        row = rows.get(nid)
        if row is None:
            return None, rid
        name = (row.tags or {}).get("Name")
        cidr = (row.observed_state or {}).get("cidr_block")
        label = " · ".join(str(x) for x in (name, rid, cidr) if x)
        return nid, label

    for node in nodes:
        acct_id = f"account:{node.account_id}"
        groups.setdefault(
            acct_id, GraphGroup(id=acct_id, kind="account", label=f"Account {node.account_id}")
        )
        if node.resource_type in GLOBAL_RESOURCE_TYPES and not node.vpc_id:
            gid = f"global:{node.account_id}"
            groups.setdefault(
                gid, GraphGroup(id=gid, kind="global", label="Global services", parent_id=acct_id)
            )
            node.parent_group = gid
            continue
        rid = f"region:{node.account_id}:{node.region}"
        groups.setdefault(
            rid, GraphGroup(id=rid, kind="region", label=node.region, parent_id=acct_id)
        )
        parent = rid
        if node.vpc_id:
            vid = f"vpc:{node.account_id}:{node.region}:{node.vpc_id}"
            vpc_node, vpc_label = collected(node.account_id, node.region, "vpc", node.vpc_id)
            groups.setdefault(
                vid,
                GraphGroup(id=vid, kind="vpc", label=vpc_label, parent_id=rid, node_id=vpc_node),
            )
            parent = vid
            if node.subnet_id:
                sid = f"subnet:{node.account_id}:{node.region}:{node.subnet_id}"
                sn_node, sn_label = collected(
                    node.account_id, node.region, "subnet", node.subnet_id
                )
                groups.setdefault(
                    sid,
                    GraphGroup(
                        id=sid, kind="subnet", label=sn_label, parent_id=vid, node_id=sn_node
                    ),
                )
                parent = sid
        node.parent_group = parent
    order = {"account": 0, "global": 1, "region": 1, "vpc": 2, "subnet": 3}
    return sorted(groups.values(), key=lambda g: (order[g.kind], g.id))


def materialize(
    built: BuiltGraph, scope: GraphScope, *, max_nodes: int
) -> tuple[list[GraphNode], list[GraphEdge], list[GraphGroup], GraphStats]:
    primary, context = select_nodes(built, scope)
    primary, context, truncated, total = truncate(built, primary, context, max_nodes)
    keep = primary | context
    g = built.graph
    nodes: list[GraphNode] = []
    for nid in sorted(keep):
        model = g.nodes[nid]["model"].model_copy(deep=True)
        model.context_only = nid in context
        nodes.append(model)
    edges = [
        d["model"]
        for u, v, d in g.edges(data=True)
        if u in keep and v in keep and _edge_ok(d["model"], scope)
    ]
    edges.sort(key=lambda e: e.id)
    groups = build_groups(nodes, built)
    conf = Counter(e.confidence for e in edges)
    stats = GraphStats(
        node_count=len(nodes),
        edge_count=len(edges),
        verified_edges=conf["verified"],
        inferred_edges=conf["inferred"],
        unknown_edges=conf["unknown"],
        by_resource_type=dict(sorted(Counter(n.resource_type for n in nodes).items())),
        truncated=truncated,
        total_nodes_before_limit=total,
    )
    return nodes, edges, groups, stats
