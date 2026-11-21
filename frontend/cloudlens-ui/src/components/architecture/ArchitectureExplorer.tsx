import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ReactFlowProvider } from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import { DEFAULT_FILTERS, searchNodes, type DisplayFilters } from "../../lib/filtering";
import { fetchGraph, fetchProjects } from "../../lib/graphApi";
import { missingSelection } from "../../lib/graphQuery";
import type { GraphDocument, GraphQuery, ProjectSummary } from "../../lib/graphTypes";
import { ControlsPanel } from "./ControlsPanel";
import { CoveragePanel } from "./CoveragePanel";
import { DetailPanel } from "./DetailPanel";
import { ExportMenu } from "./ExportMenu";
import { GraphCanvas, type FocusRequest } from "./GraphCanvas";
import { GraphActionsContext, useIconManifest } from "./GraphContext";

const MAX_NODES = 400;

const INITIAL_QUERY: GraphQuery = {
  project: "",
  accountId: null,
  region: null,
  view: "application",
  vpcId: null,
  service: null,
  seed: null,
  appTag: null,
  includeInferred: true,
  includeStale: false,
  detail: "standard",
  maxNodes: MAX_NODES,
};

type LoadState<T> =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "ok"; data: T };

export function ArchitectureExplorer() {
  const [projects, setProjects] = useState<LoadState<ProjectSummary[]>>({ kind: "loading" });
  const [query, setQuery] = useState<GraphQuery>(INITIAL_QUERY);
  const [graph, setGraph] = useState<LoadState<GraphDocument>>({ kind: "idle" });
  const [filters, setFilters] = useState<DisplayFilters>(DEFAULT_FILTERS);
  const [search, setSearch] = useState("");
  const [focus, setFocus] = useState<FocusRequest | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [sideTab, setSideTab] = useState<"detail" | "coverage">("coverage");
  const canvasRef = useRef<HTMLDivElement>(null);
  const iconManifest = useIconManifest();

  useEffect(() => {
    const controller = new AbortController();
    fetchProjects(controller.signal)
      .then((data) => {
        setProjects({ kind: "ok", data });
        if (data.length > 0) setQuery((q) => (q.project ? q : { ...q, project: data[0].project_alias }));
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        setProjects({ kind: "error", message: err instanceof Error ? err.message : "Unknown error" });
      });
    return () => controller.abort();
  }, []);

  const blocker = missingSelection(query);

  useEffect(() => {
    if (blocker) {
      setGraph({ kind: "idle" });
      return;
    }
    const controller = new AbortController();
    setGraph({ kind: "loading" });
    fetchGraph(query, controller.signal)
      .then((data) => setGraph({ kind: "ok", data }))
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        setGraph({ kind: "error", message: err instanceof Error ? err.message : "Unknown error" });
      });
    return () => controller.abort();
  }, [query, blocker]);

  const doc = graph.kind === "ok" ? graph.data : null;
  const matches = useMemo(() => (doc ? searchNodes(doc.nodes, search) : []), [doc, search]);
  const highlightIds = useMemo(() => new Set(matches.map((n) => n.id)), [matches]);

  const openNode = useCallback((nodeId: string) => {
    setSelected(nodeId);
    setSideTab("detail");
  }, []);

  const onSearchSubmit = () => {
    const first = matches[0];
    if (!first) return;
    setFocus({ nodeId: first.id, nonce: Date.now() });
    openNode(first.id);
  };

  const onNavigate = (nodeId: string) => {
    openNode(nodeId);
    if (doc?.nodes.some((n) => n.id === nodeId)) setFocus({ nodeId, nonce: Date.now() });
  };

  const actions = useMemo(
    () => ({ openNode, selectedNodeId: selected, iconManifest }),
    [openNode, selected, iconManifest],
  );

  const projectList = projects.kind === "ok" ? projects.data : [];

  return (
    <GraphActionsContext.Provider value={actions}>
      <div className="flex h-[calc(100vh-56px)] min-h-[560px] flex-col lg:flex-row">
        <aside className="w-full shrink-0 border-b border-white/10 bg-lens-ink/80 lg:w-72 lg:border-b-0 lg:border-r">
          {projects.kind === "error" && (
            <p className="px-4 py-3 text-xs text-red-200">Could not load projects: {projects.message}</p>
          )}
          {projects.kind === "loading" && <p className="px-4 py-3 text-xs text-lens-mist/60">Loading projects…</p>}
          {projects.kind === "ok" && (
            <ControlsPanel
              projects={projectList}
              query={query}
              onQueryChange={(patch) => setQuery((q) => ({ ...q, ...patch }))}
              filters={filters}
              onFiltersChange={(patch) => setFilters((f) => ({ ...f, ...patch }))}
              search={search}
              onSearchChange={setSearch}
              onSearchSubmit={onSearchSubmit}
              matchCount={matches.length}
              doc={doc}
            />
          )}
        </aside>

        <main className="relative min-h-[420px] flex-1">
          <div className="absolute left-3 right-3 top-3 z-20 flex items-start justify-between gap-2">
            <div className="space-y-1">
              {doc?.stats.truncated && (
                <div className="rounded-md border border-amber-400/30 bg-amber-500/15 px-3 py-1.5 text-xs text-amber-100">
                  Showing {doc.stats.node_count} of {doc.stats.total_nodes_before_limit} resources. Narrow the
                  view (VPC, service, application or region) to see everything.
                </div>
              )}
              {doc && doc.coverage.missing_permissions.length > 0 && (
                <button
                  type="button"
                  onClick={() => setSideTab("coverage")}
                  className="rounded-md border border-red-400/30 bg-red-500/15 px-3 py-1.5 text-xs text-red-100"
                >
                  {doc.coverage.missing_permissions.length} missing permission(s) — coverage is incomplete
                </button>
              )}
            </div>
            <ExportMenu
              query={{ ...query, includeInferred: filters.showInferred }}
              canvasRef={canvasRef}
              disabled={!doc}
            />
          </div>

          {blocker && (
            <div className="flex h-full items-center justify-center text-sm text-lens-mist/60">{blocker}</div>
          )}
          {graph.kind === "loading" && (
            <div className="flex h-full items-center justify-center text-sm text-lens-mist/60">Loading graph…</div>
          )}
          {graph.kind === "error" && (
            <div className="flex h-full items-center justify-center px-6 text-sm text-red-200">
              Could not load graph: {graph.message}
            </div>
          )}
          {doc && doc.nodes.length === 0 && (
            <div className="flex h-full items-center justify-center px-6 text-center text-sm text-lens-mist/60">
              No collected resources match this view. Run discovery for this account, or check the coverage
              panel for missing permissions.
            </div>
          )}
          {doc && doc.nodes.length > 0 && (
            <ReactFlowProvider>
              <GraphCanvas
                doc={doc}
                filters={filters}
                highlightIds={highlightIds}
                focus={focus}
                onSelectNode={openNode}
                canvasRef={canvasRef}
              />
            </ReactFlowProvider>
          )}
        </main>

        <aside className="flex h-96 w-full shrink-0 flex-col border-t border-white/10 bg-lens-ink/80 lg:h-auto lg:w-96 lg:border-l lg:border-t-0">
          <div className="flex border-b border-white/10 text-xs">
            {(["detail", "coverage"] as const).map((tab) => (
              <button
                key={tab}
                type="button"
                onClick={() => setSideTab(tab)}
                className={[
                  "flex-1 px-3 py-2 capitalize",
                  sideTab === tab ? "bg-white/5 text-lens-mist" : "text-lens-mist/50 hover:text-lens-mist",
                ].join(" ")}
              >
                {tab === "coverage" ? "Coverage & assumptions" : "Detail"}
              </button>
            ))}
          </div>
          <div className="min-h-0 flex-1">
            {sideTab === "detail" &&
              (selected && query.project ? (
                <DetailPanel
                  project={query.project}
                  nodeId={selected}
                  onClose={() => {
                    setSelected(null);
                    setSideTab("coverage");
                  }}
                  onNavigate={onNavigate}
                />
              ) : (
                <p className="px-4 py-3 text-xs text-lens-mist/50">Select a resource or a VPC/subnet header.</p>
              ))}
            {sideTab === "coverage" &&
              (doc ? (
                <CoveragePanel doc={doc} />
              ) : (
                <p className="px-4 py-3 text-xs text-lens-mist/50">Load a view to see coverage.</p>
              ))}
          </div>
        </aside>
      </div>
    </GraphActionsContext.Provider>
  );
}
