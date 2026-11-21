/**
 * TypeScript mirror of backend/cloudlens-api/app/graph/models.py (schema_version "1").
 * Keep in sync with the Pydantic contract.
 */

export type Confidence = "verified" | "inferred" | "unknown";
export type ViewKind = "account" | "vpc" | "application" | "service";
export type NodeKind = "resource" | "external";
export type GroupKind = "account" | "global" | "region" | "vpc" | "subnet";
export type DetailLevel = "standard" | "full";
export type EdgeCategory = "flow" | "membership" | "network" | "security" | "dependency";

export interface Evidence {
  source_api: string | null;
  collector: string | null;
  collector_version: string | null;
  from_resource: string;
  observed_at: string | null;
  detail: string | null;
}

export interface GraphNode {
  id: string;
  kind: NodeKind;
  resource_type: string;
  service: string;
  label: string;
  account_id: string;
  region: string;
  resource_id: string;
  arn: string | null;
  status: string | null;
  vpc_id: string | null;
  subnet_id: string | null;
  subnet_ids: string[];
  is_stale: boolean;
  last_seen_at: string | null;
  source: string | null;
  tags: Record<string, string>;
  context_only: boolean;
  /** Innermost GraphGroup id; used directly as the React Flow parentId. */
  parent_group: string | null;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  relationship_type: string;
  category: EdgeCategory;
  confidence: Confidence;
  evidence: Evidence[];
  attributes: Record<string, unknown>;
}

export interface GraphGroup {
  id: string;
  kind: GroupKind;
  label: string;
  parent_id: string | null;
  /** Collected VPC/subnet resource behind this group (null when only referenced). */
  node_id: string | null;
}

export interface CollectorCoverage {
  account_id: string;
  region: string | null;
  collector: string;
  status: string;
  finished_at: string | null;
  resources_seen: number;
  error_count: number;
}

export interface MissingPermission {
  account_id: string;
  region: string | null;
  collector: string;
  iam_action: string | null;
  error_code: string | null;
  message: string;
}

export interface UnresolvedReference {
  from_node: string;
  relationship_type: string;
  target_type: string;
  target_hint: string;
  reason: "not_collected" | "ambiguous" | "out_of_scope";
  evidence: string | null;
}

export interface Coverage {
  collectors: CollectorCoverage[];
  missing_permissions: MissingPermission[];
  unresolved_references: UnresolvedReference[];
  stale_resources: number;
  oldest_observation: string | null;
  newest_observation: string | null;
}

export interface GraphScope {
  project_alias: string;
  account_ids: string[];
  regions: string[];
  view: ViewKind;
  vpc_id: string | null;
  service: string | null;
  seed: string | null;
  application_tag: string | null;
  include_inferred: boolean;
  include_stale: boolean;
  detail: DetailLevel;
}

export interface GraphStats {
  node_count: number;
  edge_count: number;
  verified_edges: number;
  inferred_edges: number;
  unknown_edges: number;
  by_resource_type: Record<string, number>;
  truncated: boolean;
  total_nodes_before_limit: number;
}

export interface GraphDocument {
  schema_version: "1";
  generated_at: string;
  scope: GraphScope;
  nodes: GraphNode[];
  edges: GraphEdge[];
  groups: GraphGroup[];
  stats: GraphStats;
  coverage: Coverage;
  assumptions: string[];
}

export interface NodeDetail {
  node: GraphNode;
  observed_state: Record<string, unknown>;
  inbound: GraphEdge[];
  outbound: GraphEdge[];
}

export interface VpcSummary {
  account_id: string;
  region: string;
  vpc_id: string;
  label: string;
  resource_count: number;
}

export interface EntryPoint {
  node_id: string;
  label: string;
  resource_type: string;
  account_id: string;
  region: string;
}

export interface AccountSummary {
  account_alias: string;
  account_id: string;
  regions: string[];
}

export interface ProjectSummary {
  project_alias: string;
  accounts: AccountSummary[];
  vpcs: VpcSummary[];
  services: Record<string, number>;
  entry_points: EntryPoint[];
  application_tags: Record<string, string[]>;
}

/** Explorer query state → backend query parameters. */
export interface GraphQuery {
  project: string;
  accountId: string | null;
  region: string | null;
  view: ViewKind;
  vpcId: string | null;
  service: string | null;
  seed: string | null;
  appTag: string | null;
  includeInferred: boolean;
  includeStale: boolean;
  detail: DetailLevel;
  maxNodes: number;
}
