import { useEffect, useState, type ReactNode } from "react";

import { fetchNodeDetail } from "../../lib/graphApi";
import type { Confidence, GraphEdge, NodeDetail } from "../../lib/graphTypes";

interface Props {
  project: string;
  nodeId: string;
  onClose: () => void;
  onNavigate: (nodeId: string) => void;
}

type State =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "ok"; detail: NodeDetail };

const CONFIDENCE_STYLE: Record<Confidence, string> = {
  verified: "bg-emerald-500/15 text-emerald-200",
  inferred: "bg-amber-500/15 text-amber-200",
  unknown: "bg-slate-500/20 text-slate-300",
};

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[96px_1fr] gap-2 py-0.5 text-xs">
      <dt className="text-lens-mist/50">{label}</dt>
      <dd className="min-w-0 break-all font-mono text-lens-mist/90">{children ?? "—"}</dd>
    </div>
  );
}

function CopyValue({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <span className="inline-flex items-start gap-1">
      <span>{value}</span>
      <button
        type="button"
        className="shrink-0 rounded border border-white/10 px-1 text-[10px] text-lens-mist/60 hover:text-lens-mist"
        onClick={() => {
          void navigator.clipboard?.writeText(value).then(() => {
            setCopied(true);
            setTimeout(() => setCopied(false), 1200);
          });
        }}
      >
        {copied ? "copied" : "copy"}
      </button>
    </span>
  );
}

function EdgeList({
  title,
  edges,
  otherEnd,
  onNavigate,
}: {
  title: string;
  edges: GraphEdge[];
  otherEnd: (edge: GraphEdge) => string;
  onNavigate: (nodeId: string) => void;
}) {
  return (
    <section className="mt-4">
      <h4 className="mb-1 text-xs font-semibold uppercase tracking-wide text-lens-mist/60">
        {title} ({edges.length})
      </h4>
      {edges.length === 0 && <p className="text-xs text-lens-mist/40">None</p>}
      <ul className="space-y-2">
        {edges.map((edge) => (
          <li key={edge.id} className="rounded border border-white/10 bg-white/[0.03] p-2 text-xs">
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="font-mono text-lens-glow">{edge.relationship_type}</span>
              <span className={`rounded px-1.5 py-0.5 text-[10px] ${CONFIDENCE_STYLE[edge.confidence]}`}>
                {edge.confidence}
              </span>
              <span className="text-[10px] text-lens-mist/50">{edge.category}</span>
            </div>
            <button
              type="button"
              className="mt-1 block w-full truncate text-left font-mono text-[11px] text-lens-mist/80 hover:underline"
              title={otherEnd(edge)}
              onClick={() => onNavigate(otherEnd(edge))}
            >
              {otherEnd(edge)}
            </button>
            {Object.keys(edge.attributes).length > 0 && (
              <div className="mt-1 font-mono text-[10px] text-lens-mist/60">
                {Object.entries(edge.attributes)
                  .map(([k, v]) => `${k}=${String(v)}`)
                  .join(" · ")}
              </div>
            )}
            <ul className="mt-1 space-y-1 border-l border-white/10 pl-2">
              {edge.evidence.length === 0 && (
                <li className="text-[10px] text-amber-200/80">No evidence recorded</li>
              )}
              {edge.evidence.map((ev, i) => (
                <li key={i} className="text-[10px] text-lens-mist/60">
                  <span className="text-lens-mist/80">{ev.source_api ?? "unspecified API"}</span>
                  {ev.collector && <> · {ev.collector}{ev.collector_version ? `@${ev.collector_version}` : ""}</>}
                  {ev.observed_at && <> · observed {new Date(ev.observed_at).toLocaleString()}</>}
                  {ev.detail && <div className="text-lens-mist/50">{ev.detail}</div>}
                </li>
              ))}
            </ul>
          </li>
        ))}
      </ul>
    </section>
  );
}

export function DetailPanel({ project, nodeId, onClose, onNavigate }: Props) {
  const [state, setState] = useState<State>({ kind: "loading" });
  const [showState, setShowState] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    setState({ kind: "loading" });
    fetchNodeDetail(project, nodeId, controller.signal)
      .then((detail) => setState({ kind: "ok", detail }))
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        setState({ kind: "error", message: err instanceof Error ? err.message : "Unknown error" });
      });
    return () => controller.abort();
  }, [project, nodeId]);

  return (
    <aside className="flex h-full flex-col overflow-hidden">
      <div className="flex items-center justify-between border-b border-white/10 px-4 py-2">
        <h3 className="text-sm font-semibold text-lens-mist">Resource detail</h3>
        <button type="button" onClick={onClose} className="text-xs text-lens-mist/60 hover:text-lens-mist">
          Close
        </button>
      </div>
      <div className="flex-1 overflow-y-auto px-4 py-3">
        {state.kind === "loading" && <p className="text-xs text-lens-mist/60">Loading…</p>}
        {state.kind === "error" && <p className="text-xs text-red-200">{state.message}</p>}
        {state.kind === "ok" && (
          <>
            <div className="mb-2 text-sm font-medium text-white">{state.detail.node.label}</div>
            {state.detail.node.kind === "external" && (
              <p className="mb-2 rounded border border-dashed border-slate-400/50 px-2 py-1 text-[11px] text-lens-mist/70">
                Referenced by an AWS API record but not collected by CloudLens.
              </p>
            )}
            {state.detail.node.is_stale && (
              <p className="mb-2 rounded bg-amber-500/10 px-2 py-1 text-[11px] text-amber-200">
                Stale: not observed in the latest complete collection.
              </p>
            )}
            <dl>
              <Row label="Node id"><CopyValue value={state.detail.node.id} /></Row>
              <Row label="Resource id"><CopyValue value={state.detail.node.resource_id} /></Row>
              <Row label="ARN">{state.detail.node.arn ? <CopyValue value={state.detail.node.arn} /> : null}</Row>
              <Row label="Type">{state.detail.node.resource_type}</Row>
              <Row label="Account">{state.detail.node.account_id}</Row>
              <Row label="Region">{state.detail.node.region}</Row>
              <Row label="VPC">{state.detail.node.vpc_id}</Row>
              <Row label="Subnets">
                {state.detail.node.subnet_ids.length ? state.detail.node.subnet_ids.join(", ") : null}
              </Row>
              <Row label="Status">{state.detail.node.status}</Row>
              <Row label="Last seen">
                {state.detail.node.last_seen_at
                  ? new Date(state.detail.node.last_seen_at).toLocaleString()
                  : null}
              </Row>
              <Row label="Stale">{state.detail.node.is_stale ? "yes" : "no"}</Row>
              <Row label="Source">{state.detail.node.source}</Row>
            </dl>

            <section className="mt-3">
              <h4 className="mb-1 text-xs font-semibold uppercase tracking-wide text-lens-mist/60">Tags</h4>
              {Object.keys(state.detail.node.tags).length === 0 ? (
                <p className="text-xs text-lens-mist/40">None</p>
              ) : (
                <dl>
                  {Object.entries(state.detail.node.tags).map(([k, v]) => (
                    <Row key={k} label={k}>{v}</Row>
                  ))}
                </dl>
              )}
            </section>

            <section className="mt-3">
              <button
                type="button"
                className="text-xs font-semibold uppercase tracking-wide text-lens-mist/60 hover:text-lens-mist"
                onClick={() => setShowState((v) => !v)}
              >
                {showState ? "▾" : "▸"} Observed state
              </button>
              {showState && (
                <pre className="mt-1 max-h-80 overflow-auto rounded bg-black/40 p-2 text-[10px] leading-snug text-lens-mist/80">
                  {JSON.stringify(state.detail.observed_state, null, 2)}
                </pre>
              )}
            </section>

            <EdgeList
              title="Outbound"
              edges={state.detail.outbound}
              otherEnd={(e) => e.target}
              onNavigate={onNavigate}
            />
            <EdgeList
              title="Inbound"
              edges={state.detail.inbound}
              otherEnd={(e) => e.source}
              onNavigate={onNavigate}
            />
          </>
        )}
      </div>
    </aside>
  );
}
