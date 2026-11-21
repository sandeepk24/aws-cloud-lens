import type { GraphDocument, GraphQuery, NodeDetail, ProjectSummary } from "./graphTypes";
import { buildGraphParams } from "./graphQuery";

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");

export type ExportFormat = "mermaid" | "svg" | "json";

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, { signal });
  if (!response.ok) {
    let detail = "";
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = `: ${body.detail}`;
    } catch {
      // non-JSON error body
    }
    throw new Error(`Request failed (${response.status})${detail}`);
  }
  return response.json() as Promise<T>;
}

export function fetchProjects(signal?: AbortSignal): Promise<ProjectSummary[]> {
  return getJson<ProjectSummary[]>("/graph/projects", signal);
}

export function fetchGraph(query: GraphQuery, signal?: AbortSignal): Promise<GraphDocument> {
  return getJson<GraphDocument>(`/graph?${buildGraphParams(query).toString()}`, signal);
}

export function fetchNodeDetail(
  project: string,
  nodeId: string,
  signal?: AbortSignal,
): Promise<NodeDetail> {
  const params = new URLSearchParams({ project, id: nodeId });
  return getJson<NodeDetail>(`/graph/node?${params.toString()}`, signal);
}

/** Download a backend export (Mermaid / SVG / JSON) for the current query. */
export async function fetchExport(query: GraphQuery, format: ExportFormat): Promise<Blob> {
  const response = await fetch(
    `${API_BASE}/graph/export/${format}?${buildGraphParams(query).toString()}`,
  );
  if (!response.ok) {
    throw new Error(`Export failed (${response.status})`);
  }
  return response.blob();
}

export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.rel = "noopener";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function saveDataUrl(dataUrl: string, filename: string): void {
  const anchor = document.createElement("a");
  anchor.href = dataUrl;
  anchor.download = filename;
  anchor.rel = "noopener";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
}
