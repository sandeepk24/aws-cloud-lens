import type { Confidence, EdgeCategory, GraphEdge } from "./graphTypes";

export const CATEGORY_COLORS: Record<EdgeCategory, string> = {
  flow: "#38bdf8",
  membership: "#a78bfa",
  network: "#64748b",
  security: "#f59e0b",
  dependency: "#34d399",
};

/** verified = solid, inferred = dashed, unknown = dotted. */
export const CONFIDENCE_DASH: Record<Confidence, string | undefined> = {
  verified: undefined,
  inferred: "8 5",
  unknown: "2 4",
};

export interface EdgeVisual {
  stroke: string;
  strokeWidth: number;
  strokeDasharray?: string;
  animated: boolean;
  label: string;
}

/** Visual encoding for a graph edge (pure). */
export function edgeVisual(edge: GraphEdge): EdgeVisual {
  return {
    stroke: CATEGORY_COLORS[edge.category] ?? CATEGORY_COLORS.dependency,
    strokeWidth: edge.category === "flow" ? 2 : edge.category === "security" ? 1 : 1.5,
    strokeDasharray: CONFIDENCE_DASH[edge.confidence],
    animated: false,
    label: edge.confidence === "verified" ? edge.relationship_type : `${edge.relationship_type} (${edge.confidence})`,
  };
}
