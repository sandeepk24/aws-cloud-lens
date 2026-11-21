import type { ReactNode } from "react";

import type { DisplayFilters } from "../../lib/filtering";
import { sortedTypeCounts } from "../../lib/filtering";
import type { GraphDocument, GraphQuery, ProjectSummary, ViewKind } from "../../lib/graphTypes";

const VIEWS: { id: ViewKind; label: string }[] = [
  { id: "application", label: "Application" },
  { id: "vpc", label: "VPC" },
  { id: "service", label: "Service" },
  { id: "account", label: "Account" },
];

const selectClass =
  "w-full rounded-md border border-white/10 bg-lens-ink px-2 py-1.5 text-xs text-lens-mist focus:border-lens-accent focus:outline-none";

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1 block text-[10px] uppercase tracking-wide text-lens-mist/50">{label}</span>
      {children}
    </label>
  );
}

function Toggle({
  label,
  checked,
  onChange,
  hint,
}: {
  label: string;
  checked: boolean;
  onChange: (value: boolean) => void;
  hint?: string;
}) {
  return (
    <label className="flex cursor-pointer items-start gap-2 text-xs text-lens-mist/85" title={hint}>
      <input
        type="checkbox"
        className="mt-0.5 accent-sky-400"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span>
        {label}
        {hint && <span className="block text-[10px] text-lens-mist/45">{hint}</span>}
      </span>
    </label>
  );
}

interface Props {
  projects: ProjectSummary[];
  query: GraphQuery;
  onQueryChange: (patch: Partial<GraphQuery>) => void;
  filters: DisplayFilters;
  onFiltersChange: (patch: Partial<DisplayFilters>) => void;
  search: string;
  onSearchChange: (value: string) => void;
  onSearchSubmit: () => void;
  matchCount: number;
  doc: GraphDocument | null;
}

export function ControlsPanel({
  projects,
  query,
  onQueryChange,
  filters,
  onFiltersChange,
  search,
  onSearchChange,
  onSearchSubmit,
  matchCount,
  doc,
}: Props) {
  const project = projects.find((p) => p.project_alias === query.project) ?? null;
  const accounts = project?.accounts ?? [];
  const inAccount = <T extends { account_id: string }>(items: T[]) =>
    query.accountId ? items.filter((i) => i.account_id === query.accountId) : items;
  const regions = [
    ...new Set(inAccount(accounts).flatMap((a) => a.regions)),
  ].sort();
  const vpcs = inAccount(project?.vpcs ?? []).filter((v) => !query.region || v.region === query.region);
  const entryPoints = inAccount(project?.entry_points ?? []).filter(
    (e) => !query.region || e.region === query.region,
  );
  const appTags = Object.entries(project?.application_tags ?? {}).flatMap(([key, values]) =>
    values.map((value) => `${key}=${value}`),
  );
  const services = Object.entries(project?.services ?? {}).sort((a, b) => a[0].localeCompare(b[0]));

  return (
    <div className="space-y-4 overflow-y-auto px-4 py-3">
      <Field label="Project">
        <select
          className={selectClass}
          value={query.project}
          onChange={(e) =>
            onQueryChange({ project: e.target.value, accountId: null, region: null, vpcId: null, seed: null, appTag: null })
          }
        >
          {projects.length === 0 && <option value="">No projects configured</option>}
          {projects.map((p) => (
            <option key={p.project_alias} value={p.project_alias}>
              {p.project_alias}
            </option>
          ))}
        </select>
      </Field>

      <div className="grid grid-cols-2 gap-2">
        <Field label="Account">
          <select
            className={selectClass}
            value={query.accountId ?? ""}
            onChange={(e) => onQueryChange({ accountId: e.target.value || null, vpcId: null, seed: null })}
          >
            <option value="">All accounts</option>
            {accounts.map((a) => (
              <option key={a.account_id} value={a.account_id}>
                {a.account_alias} ({a.account_id})
              </option>
            ))}
          </select>
        </Field>
        <Field label="Region">
          <select
            className={selectClass}
            value={query.region ?? ""}
            onChange={(e) => onQueryChange({ region: e.target.value || null, vpcId: null })}
          >
            <option value="">All regions</option>
            {regions.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </Field>
      </div>

      <Field label="View">
        <div className="grid grid-cols-4 gap-1">
          {VIEWS.map((v) => (
            <button
              key={v.id}
              type="button"
              onClick={() => onQueryChange({ view: v.id })}
              className={[
                "rounded px-1 py-1 text-[11px]",
                query.view === v.id ? "bg-lens-accent text-white" : "bg-white/5 text-lens-mist/80 hover:bg-white/10",
              ].join(" ")}
            >
              {v.label}
            </button>
          ))}
        </div>
      </Field>

      {query.view === "vpc" && (
        <Field label="VPC">
          <select
            className={selectClass}
            value={query.vpcId ?? ""}
            onChange={(e) => onQueryChange({ vpcId: e.target.value || null })}
          >
            <option value="">Select a VPC…</option>
            {vpcs.map((v) => (
              <option key={`${v.account_id}:${v.region}:${v.vpc_id}`} value={v.vpc_id}>
                {v.label} · {v.region} · {v.resource_count}
              </option>
            ))}
          </select>
        </Field>
      )}

      {query.view === "service" && (
        <Field label="Service">
          <select
            className={selectClass}
            value={query.service ?? ""}
            onChange={(e) => onQueryChange({ service: e.target.value || null })}
          >
            <option value="">Select a service…</option>
            {services.map(([svc, count]) => (
              <option key={svc} value={svc}>
                {svc} ({count})
              </option>
            ))}
          </select>
        </Field>
      )}

      {query.view === "application" && (
        <>
          <Field label="Entry point">
            <select
              className={selectClass}
              value={query.seed ?? ""}
              onChange={(e) => onQueryChange({ seed: e.target.value || null, appTag: null })}
            >
              <option value="">Select an entry point…</option>
              {entryPoints.map((ep) => (
                <option key={ep.node_id} value={ep.node_id}>
                  {ep.label} · {ep.resource_type} · {ep.region}
                </option>
              ))}
            </select>
          </Field>
          <Field label="…or application tag">
            <select
              className={selectClass}
              value={query.appTag ?? ""}
              onChange={(e) => onQueryChange({ appTag: e.target.value || null, seed: null })}
            >
              <option value="">Select a tag…</option>
              {appTags.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </Field>
        </>
      )}

      <Field label="Search">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            onSearchSubmit();
          }}
        >
          <input
            type="search"
            value={search}
            onChange={(e) => onSearchChange(e.target.value)}
            placeholder="Name, resource ID or ARN — Enter to focus"
            className={selectClass}
          />
        </form>
        {search.trim() && (
          <span className="mt-1 block text-[10px] text-lens-mist/50">
            {matchCount} match{matchCount === 1 ? "" : "es"} in this view
          </span>
        )}
      </Field>

      <div className="space-y-2">
        <Toggle
          label="Show inferred relationships"
          checked={filters.showInferred}
          onChange={(v) => onFiltersChange({ showInferred: v })}
          hint="Dashed lines; derived, not read directly from an API field"
        />
        <Toggle
          label="Show security associations"
          checked={filters.showSecurity}
          onChange={(v) => onFiltersChange({ showSecurity: v })}
          hint="Security group / WAF attachment — not proof of traffic"
        />
        <Toggle
          label="Show stale resources"
          checked={query.includeStale}
          onChange={(v) => onQueryChange({ includeStale: v })}
          hint="Not seen in the latest complete collection"
        />
        <Toggle
          label="Full detail"
          checked={query.detail === "full"}
          onChange={(v) => onQueryChange({ detail: v ? "full" : "standard" })}
          hint="Adds route tables, NACLs, volumes, task definitions, log groups"
        />
      </div>

      {doc && (
        <section>
          <h4 className="mb-1 text-[10px] uppercase tracking-wide text-lens-mist/50">
            Resources in view ({doc.stats.node_count})
          </h4>
          <ul className="space-y-0.5">
            {sortedTypeCounts(doc.stats.by_resource_type).map(([type, count]) => (
              <li key={type} className="flex justify-between text-xs text-lens-mist/80">
                <span className="truncate font-mono">{type}</span>
                <span className="font-mono text-lens-mist/60">{count}</span>
              </li>
            ))}
          </ul>
          <p className="mt-2 text-[10px] text-lens-mist/50">
            Edges: {doc.stats.verified_edges} verified · {doc.stats.inferred_edges} inferred ·{" "}
            {doc.stats.unknown_edges} unknown
          </p>
        </section>
      )}

      <section className="text-[10px] text-lens-mist/50">
        <div className="flex items-center gap-2">
          <svg width="28" height="6" aria-hidden><line x1="0" y1="3" x2="28" y2="3" stroke="#38bdf8" strokeWidth="2" /></svg>
          verified
        </div>
        <div className="flex items-center gap-2">
          <svg width="28" height="6" aria-hidden><line x1="0" y1="3" x2="28" y2="3" stroke="#38bdf8" strokeWidth="2" strokeDasharray="8 5" /></svg>
          inferred
        </div>
        <div className="flex items-center gap-2">
          <svg width="28" height="6" aria-hidden><line x1="0" y1="3" x2="28" y2="3" stroke="#38bdf8" strokeWidth="2" strokeDasharray="2 4" /></svg>
          unknown
        </div>
      </section>
    </div>
  );
}
