import { memo } from "react";
import { Handle, Position, type NodeProps } from "@xyflow/react";

import { iconUrl, serviceBadge } from "../../lib/icons";
import type { ResourceFlowNode } from "../../lib/layout";
import { useGraphActions } from "./GraphContext";

function ResourceNodeImpl({ data }: NodeProps<ResourceFlowNode>) {
  const { graphNode, highlighted } = data;
  const { selectedNodeId, iconManifest } = useGraphActions();
  const external = graphNode.kind === "external";
  const badge = serviceBadge(graphNode.service, graphNode.kind);
  const icon = external ? null : iconUrl(iconManifest, graphNode.resource_type);
  const selected = selectedNodeId === graphNode.id;

  const border = external
    ? "border-dashed border-slate-400/60"
    : selected
      ? "border-lens-glow"
      : highlighted
        ? "border-amber-300"
        : "border-white/15";

  return (
    <div
      className={[
        "flex h-full w-full items-center gap-2 rounded-md border bg-lens-slate/95 px-2 text-left shadow-md",
        border,
        highlighted ? "ring-2 ring-amber-300/70" : "",
        graphNode.context_only || graphNode.is_stale ? "opacity-50" : "",
      ].join(" ")}
      title={graphNode.arn ?? graphNode.resource_id}
    >
      <Handle type="target" position={Position.Left} className="!h-1.5 !w-1.5 !bg-slate-500" />
      {icon ? (
        <img src={icon} alt="" className="h-8 w-8 shrink-0" draggable={false} />
      ) : (
        <span
          className="flex h-8 w-8 shrink-0 items-center justify-center rounded text-[10px] font-semibold text-white"
          style={{ backgroundColor: badge.color }}
          aria-hidden
        >
          {badge.text}
        </span>
      )}
      <div className="min-w-0 flex-1">
        <div className="truncate text-xs font-medium text-lens-mist">{graphNode.label}</div>
        <div className="truncate font-mono text-[10px] text-lens-mist/60">
          {graphNode.resource_type}
          {external ? " · not collected" : ""}
          {graphNode.is_stale ? " · stale" : ""}
          {graphNode.context_only ? " · context" : ""}
        </div>
      </div>
      <Handle type="source" position={Position.Right} className="!h-1.5 !w-1.5 !bg-slate-500" />
    </div>
  );
}

export const ResourceNode = memo(ResourceNodeImpl);
