import { memo } from "react";
import type { NodeProps } from "@xyflow/react";

import type { GroupKind } from "../../lib/graphTypes";
import type { GroupFlowNode } from "../../lib/layout";
import { useGraphActions } from "./GraphContext";

const GROUP_STYLE: Record<GroupKind, { box: string; tag: string }> = {
  account: { box: "border-slate-400/40 bg-white/[0.02]", tag: "Account" },
  global: { box: "border-fuchsia-400/40 bg-fuchsia-500/[0.04]", tag: "Global" },
  region: { box: "border-sky-400/40 bg-sky-500/[0.03]", tag: "Region" },
  vpc: { box: "border-emerald-400/50 bg-emerald-500/[0.04]", tag: "VPC" },
  subnet: { box: "border-teal-300/40 bg-teal-400/[0.05] border-dashed", tag: "Subnet" },
};

function GroupNodeImpl({ data }: NodeProps<GroupFlowNode>) {
  const { group, resourceCount } = data;
  const { openNode } = useGraphActions();
  const style = GROUP_STYLE[group.kind] ?? GROUP_STYLE.account;
  const nodeId = group.node_id;

  return (
    <div className={`h-full w-full rounded-lg border ${style.box}`}>
      <div className="flex items-center gap-2 px-3 pt-2 text-[11px]">
        <span className="rounded bg-white/10 px-1.5 py-0.5 font-mono uppercase tracking-wide text-lens-mist/70">
          {style.tag}
        </span>
        {nodeId ? (
          <button
            type="button"
            className="nodrag truncate font-medium text-lens-mist underline-offset-2 hover:underline"
            onClick={(event) => {
              event.stopPropagation();
              openNode(nodeId);
            }}
          >
            {group.label}
          </button>
        ) : (
          <span className="truncate font-medium text-lens-mist">{group.label}</span>
        )}
        <span className="ml-auto shrink-0 font-mono text-lens-mist/50">{resourceCount}</span>
      </div>
    </div>
  );
}

export const GroupNode = memo(GroupNodeImpl);
