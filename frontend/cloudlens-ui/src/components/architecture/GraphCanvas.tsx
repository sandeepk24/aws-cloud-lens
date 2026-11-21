import { useEffect, useMemo, useState, type RefObject } from "react";
import {
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  ReactFlow,
  useReactFlow,
  type NodeMouseHandler,
  type NodeTypes,
} from "@xyflow/react";

import type { DisplayFilters } from "../../lib/filtering";
import type { GraphDocument } from "../../lib/graphTypes";
import { layoutGraph, type FlowNode, type LayoutResult } from "../../lib/layout";
import { GroupNode } from "./GroupNode";
import { ResourceNode } from "./ResourceNode";

const nodeTypes: NodeTypes = { resource: ResourceNode, cloudGroup: GroupNode };

export interface FocusRequest {
  nodeId: string;
  nonce: number;
}

interface Props {
  doc: GraphDocument;
  filters: DisplayFilters;
  highlightIds: Set<string>;
  focus: FocusRequest | null;
  onSelectNode: (nodeId: string) => void;
  canvasRef: RefObject<HTMLDivElement>;
}

export function GraphCanvas({ doc, filters, highlightIds, focus, onSelectNode, canvasRef }: Props) {
  const [layout, setLayout] = useState<LayoutResult | null>(null);
  const [layoutError, setLayoutError] = useState<string | null>(null);
  const flow = useReactFlow();

  // Layout only depends on the document and display filters.
  useEffect(() => {
    let cancelled = false;
    setLayoutError(null);
    layoutGraph(doc, { filters })
      .then((result) => {
        if (!cancelled) setLayout(result);
      })
      .catch((err: unknown) => {
        if (!cancelled) setLayoutError(err instanceof Error ? err.message : "Layout failed");
      });
    return () => {
      cancelled = true;
    };
  }, [doc, filters]);

  // Cheap pass: apply search highlighting without re-running layout.
  const nodes = useMemo<FlowNode[]>(() => {
    if (!layout) return [];
    if (highlightIds.size === 0) return layout.nodes;
    return layout.nodes.map((node) =>
      node.type === "resource" && highlightIds.has(node.id)
        ? { ...node, data: { ...node.data, highlighted: true } }
        : node,
    );
  }, [layout, highlightIds]);

  useEffect(() => {
    if (layout) {
      requestAnimationFrame(() => flow.fitView({ padding: 0.1, duration: 300 }));
    }
  }, [layout, flow]);

  useEffect(() => {
    if (!focus || !layout) return;
    flow.fitView({ nodes: [{ id: focus.nodeId }], duration: 400, maxZoom: 1.3, padding: 0.4 });
  }, [focus, layout, flow]);

  const onNodeClick: NodeMouseHandler = (_event, node) => {
    if (node.type === "resource") onSelectNode(node.id);
  };

  if (layoutError) {
    return <div className="p-6 text-sm text-red-200">Layout failed: {layoutError}</div>;
  }

  return (
    <div ref={canvasRef} className="relative h-full w-full">
      {!layout && (
        <div className="absolute inset-0 z-10 flex items-center justify-center text-sm text-lens-mist/70">
          Laying out graph…
        </div>
      )}
      <ReactFlow
        nodes={nodes}
        edges={layout?.edges ?? []}
        nodeTypes={nodeTypes}
        onNodeClick={onNodeClick}
        colorMode="dark"
        fitView
        minZoom={0.05}
        maxZoom={2}
        onlyRenderVisibleElements
        nodesConnectable={false}
        nodesDraggable={false}
        edgesFocusable={false}
      >
        <Background variant={BackgroundVariant.Dots} gap={18} size={1} color="#1e293b" />
        <Controls showInteractive={false} />
        <MiniMap
          pannable
          zoomable
          nodeColor={(n) => (n.type === "cloudGroup" ? "transparent" : "#38bdf8")}
          nodeStrokeColor={(n) => (n.type === "cloudGroup" ? "#334155" : "#38bdf8")}
          maskColor="rgba(11,18,32,0.7)"
          style={{ backgroundColor: "#0b1220" }}
        />
      </ReactFlow>
    </div>
  );
}
