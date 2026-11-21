import type { GraphDocument, GraphEdge, GraphNode } from "./graphTypes";

const ACCOUNT = "111122223333";
const REGION = "us-east-1";
const LB = `arn:aws:elasticloadbalancing:${REGION}:${ACCOUNT}:loadbalancer/app/web/abc`;
const LISTENER = `arn:aws:elasticloadbalancing:${REGION}:${ACCOUNT}:listener/app/web/abc/111`;
const RULE = `arn:aws:elasticloadbalancing:${REGION}:${ACCOUNT}:listener-rule/app/web/abc/111/222`;
const TG = `arn:aws:elasticloadbalancing:${REGION}:${ACCOUNT}:targetgroup/web/xyz`;
const TASK = `arn:aws:ecs:${REGION}:${ACCOUNT}:task/prod/0123`;

const ACCOUNT_GROUP = `account:${ACCOUNT}`;
const REGION_GROUP = `region:${ACCOUNT}:${REGION}`;
const VPC_GROUP = `vpc:${ACCOUNT}:${REGION}:vpc-1`;
const SUBNET_GROUP = `subnet:${ACCOUNT}:${REGION}:subnet-1`;

function node(partial: Partial<GraphNode> & Pick<GraphNode, "resource_type" | "service" | "label" | "resource_id">): GraphNode {
  return {
    id: `${ACCOUNT}:${REGION}:${partial.resource_type}:${partial.arn ?? partial.resource_id}`,
    kind: "resource",
    account_id: ACCOUNT,
    region: REGION,
    arn: null,
    status: "active",
    vpc_id: "vpc-1",
    subnet_id: null,
    subnet_ids: [],
    is_stale: false,
    last_seen_at: "2026-11-20T10:00:00Z",
    source: "aws.elbv2",
    tags: {},
    context_only: false,
    parent_group: VPC_GROUP,
    ...partial,
  };
}

export const alb = node({ resource_type: "load-balancer", service: "elbv2", label: "web", resource_id: LB, arn: LB });
export const listener = node({ resource_type: "listener", service: "elbv2", label: "HTTPS:443", resource_id: LISTENER, arn: LISTENER });
export const rule = node({ resource_type: "listener-rule", service: "elbv2", label: "path /api/*", resource_id: RULE, arn: RULE });
export const tg = node({ resource_type: "target-group", service: "elbv2", label: "web-tg", resource_id: TG, arn: TG });
export const task = node({
  resource_type: "ecs-task",
  service: "ecs",
  label: "task 0123",
  resource_id: TASK,
  arn: TASK,
  subnet_id: "subnet-1",
  subnet_ids: ["subnet-1"],
  parent_group: SUBNET_GROUP,
  source: "aws.ecs",
});
export const external = node({
  id: "external:ip:10.0.9.9",
  kind: "external",
  resource_type: "registered-target",
  service: "elbv2",
  label: "10.0.9.9:8080",
  resource_id: "10.0.9.9",
  vpc_id: null,
  parent_group: REGION_GROUP,
  status: null,
  last_seen_at: null,
});
export const staleTask = node({
  resource_type: "ecs-task",
  service: "ecs",
  label: "old task",
  resource_id: `${TASK}-old`,
  arn: `${TASK}-old`,
  is_stale: true,
  parent_group: SUBNET_GROUP,
});

function edge(
  id: string,
  source: GraphNode,
  target: GraphNode,
  partial: Partial<GraphEdge> = {},
): GraphEdge {
  return {
    id,
    source: source.id,
    target: target.id,
    relationship_type: "forwards_to",
    category: "flow",
    confidence: "verified",
    evidence: [
      {
        source_api: "DescribeRules:Actions[].TargetGroupArn",
        collector: "elbv2.listener_rule",
        collector_version: "0.1.0",
        from_resource: source.id,
        observed_at: "2026-11-20T10:00:00Z",
        detail: null,
      },
    ],
    attributes: {},
    ...partial,
  };
}

export const sampleDoc: GraphDocument = {
  schema_version: "1",
  generated_at: "2026-11-20T10:05:00Z",
  scope: {
    project_alias: "demo",
    account_ids: [ACCOUNT],
    regions: [REGION],
    view: "application",
    vpc_id: null,
    service: null,
    seed: alb.id,
    application_tag: null,
    include_inferred: true,
    include_stale: true,
    detail: "standard",
  },
  nodes: [alb, listener, rule, tg, task, external, staleTask],
  edges: [
    edge("e1", listener, alb, { relationship_type: "belongs_to", category: "membership" }),
    edge("e2", rule, listener, { relationship_type: "belongs_to", category: "membership" }),
    edge("e3", rule, tg),
    edge("e4", tg, task, {
      relationship_type: "routes_to",
      confidence: "inferred",
      attributes: { port: 8080, health_state: "healthy" },
    }),
    edge("e5", tg, external, { relationship_type: "routes_to", attributes: { port: 8080 } }),
    edge("e6", alb, task, { relationship_type: "uses", category: "security", confidence: "verified" }),
  ],
  groups: [
    { id: ACCOUNT_GROUP, kind: "account", label: `demo / ${ACCOUNT}`, parent_id: null, node_id: null },
    { id: `global:${ACCOUNT}`, kind: "global", label: "Global", parent_id: ACCOUNT_GROUP, node_id: null },
    { id: REGION_GROUP, kind: "region", label: REGION, parent_id: ACCOUNT_GROUP, node_id: null },
    { id: VPC_GROUP, kind: "vpc", label: "prod-vpc (vpc-1)", parent_id: REGION_GROUP, node_id: "vpc-node" },
    { id: SUBNET_GROUP, kind: "subnet", label: "private-a (subnet-1)", parent_id: VPC_GROUP, node_id: null },
  ],
  stats: {
    node_count: 7,
    edge_count: 6,
    verified_edges: 5,
    inferred_edges: 1,
    unknown_edges: 0,
    by_resource_type: { "load-balancer": 1, listener: 1, "listener-rule": 1, "target-group": 1, "ecs-task": 2, "registered-target": 1 },
    truncated: false,
    total_nodes_before_limit: 7,
  },
  coverage: {
    collectors: [],
    missing_permissions: [],
    unresolved_references: [],
    stale_resources: 1,
    oldest_observation: "2026-11-20T10:00:00Z",
    newest_observation: "2026-11-20T10:00:00Z",
  },
  assumptions: ["Security group associations are not proof of traffic."],
};
