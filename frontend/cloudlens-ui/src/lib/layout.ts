import ELK from "elkjs/lib/elk.bundled.js";
import type { ElkExtendedEdge, ElkNode } from "elkjs/lib/elk-api";
import type { Edge, Node } from "@xyflow/react";
import { MarkerType } from "@xyflow/react";

import { edgeVisual } from "./edgeStyle";
import { DEFAULT_FILTERS, filterGraph, type DisplayFilters } from "./filtering";
import type { GraphDocument, GraphEdge, GraphGroup, GraphNode } from "./graphTypes";

export const RESOURCE_WIDTH = 220;
export const RESOURCE_HEIGHT = 60;
const GROUP_PADDING = "[top=40,left=16,bottom=16,right=16]";

export type ResourceNodeData = { graphNode: GraphNode; highlighted: boolean };
export type GroupNodeData = { group: GraphGroup; resourceCount: number };
export type ResourceFlowNode = Node<ResourceNodeData, "resource">;
export type GroupFlowNode = Node<GroupNodeData, "cloudGroup">;
export type FlowNode = ResourceFlowNode | GroupFlowNode;
export type FlowEdge = Edge<{ graphEdge: GraphEdge }>;

export interface LayoutOptions {
  filters?: DisplayFilters;
  direction?: "RIGHT" | "DOWN";
}

export interface LayoutResult {
  nodes: FlowNode[];
  edges: FlowEdge[];
  hiddenEdges: number;
}

interface Hierarchy {
  /** child group ids keyed by parent group id ("" = root) */
  groupChildren: Map<string, GraphGroup[]>;
  /** resource nodes keyed by parent group id ("" = root) */
  nodeChildren: Map<string, GraphNode[]>;
  /** groups that (transitively) contain at least one node */
  nonEmpty: Set<string>;
  groupsById: Map<string, GraphGroup>;
}

function push<T>(map: Map<string, T[]>, key: string, value: T): void {
  const list = map.get(key);
  if (list) list.push(value);
  else map.set(key, [value]);
}

/** Index groups/nodes by parent; unknown parents fall back to the root (pure). */
export function buildHierarchy(nodes: GraphNode[], groups: GraphGroup[]): Hierarchy {
  const groupsById = new Map(groups.map((g) => [g.id, g]));
  const groupChildren = new Map<string, GraphGroup[]>();
  const nodeChildren = new Map<string, GraphNode[]>();
  for (const group of groups) {
    const parent = group.parent_id && groupsById.has(group.parent_id) ? group.parent_id : "";
    push(groupChildren, parent, group);
  }
  const nonEmpty = new Set<string>();
  for (const node of nodes) {
    const parent = node.parent_group && groupsById.has(node.parent_group) ? node.parent_group : "";
    push(nodeChildren, parent, node);
    let cursor: string | null = parent || null;
    const guard = new Set<string>();
    while (cursor && !guard.has(cursor)) {
      guard.add(cursor);
      nonEmpty.add(cursor);
      cursor = groupsById.get(cursor)?.parent_id ?? null;
    }
  }
  return { groupChildren, nodeChildren, nonEmpty, groupsById };
}

/** Build the ELK input graph (pure, no layout yet). Empty groups are dropped. */
export function buildElkGraph(
  nodes: GraphNode[],
  edges: GraphEdge[],
  groups: GraphGroup[],
  direction: "RIGHT" | "DOWN" = "RIGHT",
): ElkNode {
  const h = buildHierarchy(nodes, groups);

  const build = (parentKey: string): ElkNode[] => {
    const children: ElkNode[] = [];
    for (const group of h.groupChildren.get(parentKey) ?? []) {
      if (!h.nonEmpty.has(group.id)) continue;
      children.push({
        id: group.id,
        layoutOptions: { "elk.padding": GROUP_PADDING },
        children: build(group.id),
      });
    }
    for (const node of h.nodeChildren.get(parentKey) ?? []) {
      children.push({ id: node.id, width: RESOURCE_WIDTH, height: RESOURCE_HEIGHT });
    }
    return children;
  };

  const elkEdges: ElkExtendedEdge[] = edges.map((edge) => ({
    id: edge.id,
    sources: [edge.source],
    targets: [edge.target],
  }));

  return {
    id: "root",
    layoutOptions: {
      "elk.algorithm": "layered",
      "elk.direction": direction,
      "elk.hierarchyHandling": "INCLUDE_CHILDREN",
      "elk.layered.spacing.nodeNodeBetweenLayers": "70",
      "elk.spacing.nodeNode": "28",
      "elk.layered.nodePlacement.strategy": "BRANDES_KOEPF",
      "elk.edgeRouting": "POLYLINE",
    },
    children: build(""),
    edges: elkEdges,
  };
}

function countDescendants(h: Hierarchy, groupId: string): number {
  let total = (h.nodeChildren.get(groupId) ?? []).length;
  for (const child of h.groupChildren.get(groupId) ?? []) total += countDescendants(h, child.id);
  return total;
}

export function toFlowEdges(edges: GraphEdge[]): FlowEdge[] {
  return edges.map((edge) => {
    const visual = edgeVisual(edge);
    return {
      id: edge.id,
      source: edge.source,
      target: edge.target,
      label: visual.label,
      animated: visual.animated,
      style: {
        stroke: visual.stroke,
        strokeWidth: visual.strokeWidth,
        strokeDasharray: visual.strokeDasharray,
      },
      labelStyle: { fill: "#cbd5e1", fontSize: 10 },
      labelBgStyle: { fill: "#0b1220", fillOpacity: 0.85 },
      labelBgPadding: [4, 2] as [number, number],
      markerEnd: { type: MarkerType.ArrowClosed, color: visual.stroke, width: 16, height: 16 },
      data: { graphEdge: edge },
    };
  });
}

/**
 * Filter and lay out a graph document for React Flow. Groups become nested
 * parent nodes (parents always precede children, as React Flow requires).
 */
export async function layoutGraph(
  doc: GraphDocument,
  options: LayoutOptions = {},
): Promise<LayoutResult> {
  const filtered = filterGraph(doc, options.filters ?? DEFAULT_FILTERS);
  const elkInput = buildElkGraph(filtered.nodes, filtered.edges, doc.groups, options.direction);
  const elk = new ELK();
  const laidOut = await elk.layout(elkInput);

  const h = buildHierarchy(filtered.nodes, doc.groups);
  const nodesById = new Map(filtered.nodes.map((n) => [n.id, n]));
  const out: FlowNode[] = [];

  const visit = (elkNode: ElkNode, parentId: string | undefined): void => {
    for (const child of elkNode.children ?? []) {
      const position = { x: child.x ?? 0, y: child.y ?? 0 };
      const group = h.groupsById.get(child.id);
      if (group) {
        out.push({
          id: child.id,
          type: "cloudGroup",
          position,
          parentId,
          extent: parentId ? "parent" : undefined,
          selectable: false,
          draggable: false,
          style: { width: child.width ?? 0, height: child.height ?? 0 },
          zIndex: 0,
          data: { group, resourceCount: countDescendants(h, group.id) },
        });
        visit(child, child.id);
        continue;
      }
      const graphNode = nodesById.get(child.id);
      if (!graphNode) continue;
      out.push({
        id: child.id,
        type: "resource",
        position,
        parentId,
        extent: parentId ? "parent" : undefined,
        zIndex: 1,
        style: { width: RESOURCE_WIDTH, height: RESOURCE_HEIGHT },
        data: { graphNode, highlighted: false },
      });
    }
  };
  visit(laidOut, undefined);

  return { nodes: out, edges: toFlowEdges(filtered.edges), hiddenEdges: filtered.hiddenEdges };
}
