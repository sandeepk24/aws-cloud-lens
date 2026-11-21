import type { GraphDocument } from "../../lib/graphTypes";

function fmt(value: string | null): string {
  return value ? new Date(value).toLocaleString() : "—";
}

const STATUS_STYLE: Record<string, string> = {
  succeeded: "text-emerald-300",
  partial: "text-amber-300",
  failed: "text-red-300",
  running: "text-sky-300",
};

export function CoveragePanel({ doc }: { doc: GraphDocument }) {
  const { coverage, assumptions } = doc;
  return (
    <div className="h-full overflow-y-auto px-4 py-3 text-xs">
      <section>
        <h4 className="mb-1 font-semibold uppercase tracking-wide text-lens-mist/60">Freshness</h4>
        <p className="text-lens-mist/80">Generated {fmt(doc.generated_at)}</p>
        <p className="text-lens-mist/80">Oldest observation {fmt(coverage.oldest_observation)}</p>
        <p className="text-lens-mist/80">Newest observation {fmt(coverage.newest_observation)}</p>
        <p className="text-lens-mist/80">Stale resources {coverage.stale_resources}</p>
      </section>

      <section className="mt-4">
        <h4 className="mb-1 font-semibold uppercase tracking-wide text-lens-mist/60">
          Missing permissions ({coverage.missing_permissions.length})
        </h4>
        {coverage.missing_permissions.length === 0 && <p className="text-lens-mist/40">None reported</p>}
        <ul className="space-y-1">
          {coverage.missing_permissions.map((mp, i) => (
            <li key={i} className="rounded bg-red-500/10 px-2 py-1 text-red-100">
              <span className="font-mono">{mp.iam_action ?? mp.error_code ?? "unknown action"}</span>
              <span className="text-red-100/60">
                {" "}· {mp.collector} · {mp.account_id}
                {mp.region ? ` / ${mp.region}` : ""}
              </span>
              <div className="text-[10px] text-red-100/60">{mp.message}</div>
            </li>
          ))}
        </ul>
      </section>

      <section className="mt-4">
        <h4 className="mb-1 font-semibold uppercase tracking-wide text-lens-mist/60">
          Unresolved references ({coverage.unresolved_references.length})
        </h4>
        {coverage.unresolved_references.length === 0 && <p className="text-lens-mist/40">None</p>}
        <ul className="space-y-1">
          {coverage.unresolved_references.slice(0, 200).map((ref, i) => (
            <li key={i} className="rounded bg-white/[0.04] px-2 py-1">
              <span className="font-mono text-lens-mist/80">{ref.relationship_type}</span>
              <span className="text-lens-mist/60"> → {ref.target_type} </span>
              <span className="font-mono text-lens-mist/70">{ref.target_hint}</span>
              <span className="ml-1 rounded bg-amber-500/15 px-1 text-[10px] text-amber-200">{ref.reason}</span>
              {ref.evidence && <div className="text-[10px] text-lens-mist/50">{ref.evidence}</div>}
            </li>
          ))}
          {coverage.unresolved_references.length > 200 && (
            <li className="text-lens-mist/50">
              …and {coverage.unresolved_references.length - 200} more (see JSON export)
            </li>
          )}
        </ul>
      </section>

      <section className="mt-4">
        <h4 className="mb-1 font-semibold uppercase tracking-wide text-lens-mist/60">
          Collectors ({coverage.collectors.length})
        </h4>
        {coverage.collectors.length === 0 && <p className="text-lens-mist/40">No collection runs recorded</p>}
        <table className="w-full table-fixed">
          <tbody>
            {coverage.collectors.map((c, i) => (
              <tr key={i} className="border-t border-white/5">
                <td className="truncate py-0.5 font-mono text-lens-mist/80" title={c.collector}>{c.collector}</td>
                <td className="w-24 truncate text-lens-mist/60">{c.region ?? "global"}</td>
                <td className={`w-16 ${STATUS_STYLE[c.status] ?? "text-lens-mist/70"}`}>{c.status}</td>
                <td className="w-10 text-right font-mono text-lens-mist/60">{c.resources_seen}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="mt-4">
        <h4 className="mb-1 font-semibold uppercase tracking-wide text-lens-mist/60">Assumptions</h4>
        <ul className="list-disc space-y-1 pl-4 text-lens-mist/70">
          {assumptions.map((a, i) => (
            <li key={i}>{a}</li>
          ))}
        </ul>
      </section>
    </div>
  );
}
