import type { ViewKind } from "./graphTypes";

const UNSAFE = /[^a-zA-Z0-9._-]+/g;

function slug(value: string): string {
  return (
    value
      .replace(UNSAFE, "-")
      .replace(/\.{2,}/g, "-")
      .replace(/^[-.]+|[-.]+$/g, "")
      .replace(/-{2,}/g, "-")
      .slice(0, 64) || "x"
  );
}

/** cloudlens-<project>-<account|all>-<view>-<YYYYMMDDTHHMMSSZ>.<ext> (pure). */
export function exportFilename(options: {
  project: string;
  accountId: string | null;
  view: ViewKind;
  extension: string;
  now?: Date;
}): string {
  const stamp = (options.now ?? new Date())
    .toISOString()
    .replace(/[-:]/g, "")
    .replace(/\.\d{3}Z$/, "Z");
  const parts = [
    "cloudlens",
    slug(options.project),
    options.accountId ? slug(options.accountId) : "all-accounts",
    options.view,
    stamp,
  ];
  return `${parts.join("-")}.${slug(options.extension)}`;
}
