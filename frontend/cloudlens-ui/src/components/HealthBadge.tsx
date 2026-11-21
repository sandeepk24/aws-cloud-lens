import { useEffect, useState } from "react";
import { fetchHealth, type HealthResponse } from "../lib/api";

type LoadState =
  | { kind: "loading" }
  | { kind: "ok"; data: HealthResponse }
  | { kind: "error"; message: string };

export function HealthBadge() {
  const [state, setState] = useState<LoadState>({ kind: "loading" });

  useEffect(() => {
    const controller = new AbortController();
    fetchHealth(controller.signal)
      .then((data) => setState({ kind: "ok", data }))
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        const message = err instanceof Error ? err.message : "Unknown error";
        setState({ kind: "error", message });
      });
    return () => controller.abort();
  }, []);

  if (state.kind === "loading") {
    return (
      <div className="inline-flex items-center gap-2 rounded-md border border-white/10 bg-white/5 px-3 py-1.5 text-sm text-lens-mist/80">
        <span className="health-dot h-2 w-2 rounded-full bg-lens-glow/60" />
        Checking API…
      </div>
    );
  }

  if (state.kind === "error") {
    return (
      <div className="inline-flex items-center gap-2 rounded-md border border-red-400/30 bg-red-500/10 px-3 py-1.5 text-sm text-red-200">
        <span className="h-2 w-2 rounded-full bg-red-400" />
        API unreachable — {state.message}
      </div>
    );
  }

  const { data } = state;
  return (
    <div className="inline-flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border border-emerald-400/25 bg-emerald-500/10 px-3 py-1.5 text-sm text-emerald-100">
      <span className="inline-flex items-center gap-2">
        <span className="health-dot h-2 w-2 rounded-full bg-emerald-400" />
        {data.service} · {data.status}
      </span>
      <span className="font-mono text-xs text-emerald-100/70">
        v{data.version} · {data.environment}
      </span>
    </div>
  );
}
