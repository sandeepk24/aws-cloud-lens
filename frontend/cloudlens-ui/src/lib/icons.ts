/**
 * Resource icons.
 *
 * CloudLens does not bundle AWS icons. To use the official AWS Architecture
 * Icons, download the package from AWS yourself (subject to its terms), copy
 * the SVGs you want into `public/aws-icons/`, and add
 * `public/aws-icons/manifest.json` mapping resource_type -> file name, e.g.
 * `{ "load-balancer": "Arch_Elastic-Load-Balancing_48.svg" }`.
 * Without a manifest, a neutral text badge is shown.
 */

export type IconManifest = Record<string, string>;

const SAFE_FILE = /^[A-Za-z0-9][A-Za-z0-9._ -]*\.(svg|png)$/;

/** Keep only plain file names (no paths, no URLs) from a manifest (pure). */
export function sanitizeManifest(raw: unknown): IconManifest {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return {};
  const out: IconManifest = {};
  for (const [type, file] of Object.entries(raw as Record<string, unknown>)) {
    if (typeof file === "string" && SAFE_FILE.test(file)) out[type] = file;
  }
  return out;
}

let manifestPromise: Promise<IconManifest> | null = null;

export function loadIconManifest(): Promise<IconManifest> {
  if (!manifestPromise) {
    manifestPromise = fetch("/aws-icons/manifest.json")
      .then((r) => (r.ok ? r.json() : {}))
      .then(sanitizeManifest)
      .catch(() => ({}));
  }
  return manifestPromise;
}

export function iconUrl(manifest: IconManifest, resourceType: string): string | null {
  const file = manifest[resourceType];
  return file ? `/aws-icons/${encodeURIComponent(file)}` : null;
}

interface Badge {
  text: string;
  color: string;
}

const SERVICE_BADGES: Record<string, Badge> = {
  ec2: { text: "EC2", color: "#f97316" },
  elbv2: { text: "ELB", color: "#a855f7" },
  ecs: { text: "ECS", color: "#f97316" },
  eks: { text: "EKS", color: "#f97316" },
  rds: { text: "RDS", color: "#3b82f6" },
  s3: { text: "S3", color: "#22c55e" },
  lambda: { text: "λ", color: "#f97316" },
  cloudfront: { text: "CF", color: "#a855f7" },
  apigateway: { text: "API", color: "#ec4899" },
  apigatewayv2: { text: "API", color: "#ec4899" },
  route53: { text: "R53", color: "#a855f7" },
  wafv2: { text: "WAF", color: "#ef4444" },
  logs: { text: "CWL", color: "#ec4899" },
  kubernetes: { text: "K8s", color: "#3b82f6" },
};

/** Fallback badge for a service (pure). */
export function serviceBadge(service: string, kind: "resource" | "external" = "resource"): Badge {
  if (kind === "external") return { text: "EXT", color: "#64748b" };
  return (
    SERVICE_BADGES[service] ?? {
      text: service.slice(0, 3).toUpperCase() || "?",
      color: "#64748b",
    }
  );
}
