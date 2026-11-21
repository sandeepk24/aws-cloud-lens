import type { GraphDocument, GraphEdge, GraphNode } from "./graphTypes";

/**
 * Client-side display filters. Stale resources and detail level are server-side
 * query parameters (include_stale / detail); these only hide what was returned.
 */
export interface DisplayFilters {
  showInferred: boolean;
  /** Security-group / WAF associations. Hidden by default: they are not traffic. */
  showSecurity: boolean;
}

export const DEFAULT_FILTERS: DisplayFilters = {
  showInferred: true,
  showSecurity: false,
};

export interface FilteredGraph {
  nodes: GraphNode[];
  edges: GraphEdge[];
  hiddenEdges: number;
  hiddenNodes: number;
}

/** Apply client-side edge filters (pure). Nodes are never removed. */
export function filterGraph(doc: GraphDocument, filters: DisplayFilters): FilteredGraph {
  const nodes = doc.nodes;
  const kept = new Set(nodes.map((n) => n.id));
  const edges = doc.edges.filter((edge) => {
    if (!kept.has(edge.source) || !kept.has(edge.target)) return false;
    if (!filters.showInferred && edge.confidence !== "verified") return false;
    if (!filters.showSecurity && edge.category === "security") return false;
    return true;
  });
  return {
    nodes,
    edges,
    hiddenNodes: doc.nodes.length - nodes.length,
    hiddenEdges: doc.edges.length - edges.length,
  };
}

/** Case-insensitive match on label, resource id, ARN or node id (pure). */
export function searchNodes(nodes: GraphNode[], term: string): GraphNode[] {
  const needle = term.trim().toLowerCase();
  if (!needle) return [];
  return nodes.filter((node) =>
    [node.label, node.resource_id, node.arn ?? "", node.id].some((value) =>
      value.toLowerCase().includes(needle),
    ),
  );
}

/** Sorted [resource_type, count] pairs, largest first (pure). */
export function sortedTypeCounts(byType: Record<string, number>): [string, number][] {
  return Object.entries(byType).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}
