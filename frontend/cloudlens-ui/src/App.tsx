import { lazy, Suspense, useState } from "react";

import { HealthBadge } from "./components/HealthBadge";

// Code-split: React Flow + ELK are only loaded when the Architecture tab is opened.
const ArchitectureExplorer = lazy(() =>
  import("./components/architecture/ArchitectureExplorer").then((m) => ({
    default: m.ArchitectureExplorer,
  })),
);

type Tab = "overview" | "architecture";

const TABS: { id: Tab; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "architecture", label: "Architecture" },
];

function Overview() {
  return (
    <div className="mx-auto flex min-h-[calc(100vh-56px)] max-w-3xl flex-col justify-center px-6 py-16">
      <p className="mb-3 font-mono text-xs uppercase tracking-[0.2em] text-lens-glow/80">
        Read-only discovery
      </p>
      <h1 className="font-display text-4xl font-semibold tracking-tight text-white sm:text-5xl">
        AWS CloudLens
      </h1>
      <p className="mt-4 max-w-xl text-base leading-relaxed text-lens-mist/75">
        Discover, collect, and analyze AWS resources. The Architecture tab renders the
        relationships CloudLens has observed through read-only AWS APIs; it never creates or
        changes infrastructure.
      </p>
      <div className="mt-8">
        <HealthBadge />
      </div>
    </div>
  );
}

export default function App() {
  const [tab, setTab] = useState<Tab>("overview");
  return (
    <div className="min-h-screen">
      <header className="flex h-14 items-center gap-6 border-b border-white/10 px-6">
        <span className="font-display text-sm font-semibold text-white">CloudLens</span>
        <nav className="flex gap-1" aria-label="Main">
          {TABS.map((t) => (
            <button
              key={t.id}
              type="button"
              aria-current={tab === t.id ? "page" : undefined}
              onClick={() => setTab(t.id)}
              className={[
                "rounded-md px-3 py-1.5 text-sm",
                tab === t.id ? "bg-white/10 text-white" : "text-lens-mist/60 hover:text-lens-mist",
              ].join(" ")}
            >
              {t.label}
            </button>
          ))}
        </nav>
      </header>
      {tab === "overview" ? (
        <Overview />
      ) : (
        <Suspense fallback={<p className="p-6 text-sm text-lens-mist/60">Loading explorer…</p>}>
          <ArchitectureExplorer />
        </Suspense>
      )}
    </div>
  );
}
