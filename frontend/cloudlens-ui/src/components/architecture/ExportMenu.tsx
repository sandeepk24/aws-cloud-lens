import { useState, type RefObject } from "react";
import { toPng, toSvg } from "html-to-image";

import { fetchExport, saveBlob, saveDataUrl, type ExportFormat } from "../../lib/graphApi";
import { exportFilename } from "../../lib/filenames";
import type { GraphQuery } from "../../lib/graphTypes";

const BACKEND_EXPORTS: { format: ExportFormat; label: string; ext: string }[] = [
  { format: "mermaid", label: "Mermaid (.mmd)", ext: "mmd" },
  { format: "svg", label: "SVG (server)", ext: "svg" },
  { format: "json", label: "Graph JSON", ext: "json" },
];

const EXCLUDED_CLASSES = ["react-flow__minimap", "react-flow__controls", "react-flow__attribution"];

function keepForImage(node: HTMLElement): boolean {
  return !EXCLUDED_CLASSES.some((cls) => node.classList?.contains(cls));
}

interface Props {
  query: GraphQuery;
  canvasRef: RefObject<HTMLDivElement>;
  disabled: boolean;
}

export function ExportMenu({ query, canvasRef, disabled }: Props) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const name = (ext: string) =>
    exportFilename({ project: query.project, accountId: query.accountId, view: query.view, extension: ext });

  const run = async (task: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await task();
      setOpen(false);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Export failed");
    } finally {
      setBusy(false);
    }
  };

  const canvasExport = (kind: "png" | "svg") =>
    run(async () => {
      const element = canvasRef.current?.querySelector<HTMLElement>(".react-flow");
      if (!element) throw new Error("Canvas not ready");
      const options = { backgroundColor: "#0b1220", filter: keepForImage, pixelRatio: 2 };
      const dataUrl = kind === "png" ? await toPng(element, options) : await toSvg(element, options);
      saveDataUrl(dataUrl, name(kind === "png" ? "png" : "canvas.svg"));
    });

  return (
    <div className="relative">
      <button
        type="button"
        disabled={disabled}
        onClick={() => setOpen((v) => !v)}
        className="rounded-md border border-white/15 bg-white/5 px-3 py-1.5 text-xs text-lens-mist hover:bg-white/10 disabled:opacity-40"
      >
        {busy ? "Exporting…" : "Export ▾"}
      </button>
      {open && (
        <div className="absolute right-0 z-30 mt-1 w-56 rounded-md border border-white/10 bg-lens-slate p-1 shadow-xl">
          <p className="px-2 pb-1 pt-1 text-[10px] uppercase tracking-wide text-lens-mist/50">From server</p>
          {BACKEND_EXPORTS.map((item) => (
            <button
              key={item.format}
              type="button"
              disabled={busy}
              className="block w-full rounded px-2 py-1 text-left text-xs text-lens-mist hover:bg-white/10"
              onClick={() => run(async () => saveBlob(await fetchExport(query, item.format), name(item.ext)))}
            >
              {item.label}
            </button>
          ))}
          <p className="px-2 pb-1 pt-2 text-[10px] uppercase tracking-wide text-lens-mist/50">Current canvas</p>
          <button
            type="button"
            disabled={busy}
            className="block w-full rounded px-2 py-1 text-left text-xs text-lens-mist hover:bg-white/10"
            onClick={() => void canvasExport("png")}
          >
            PNG (as shown)
          </button>
          <button
            type="button"
            disabled={busy}
            className="block w-full rounded px-2 py-1 text-left text-xs text-lens-mist hover:bg-white/10"
            onClick={() => void canvasExport("svg")}
          >
            SVG (as shown)
          </button>
          {error && <p className="px-2 py-1 text-[11px] text-red-200">{error}</p>}
        </div>
      )}
    </div>
  );
}
