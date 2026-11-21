import type { GraphQuery } from "./graphTypes";

/** Build the backend query string for /graph and /graph/export/* (pure). */
export function buildGraphParams(query: GraphQuery): URLSearchParams {
  const params = new URLSearchParams();
  params.set("project", query.project);
  if (query.accountId) params.append("account_id", query.accountId);
  if (query.region) params.set("region", query.region);
  params.set("view", query.view);
  if (query.view === "vpc" && query.vpcId) params.set("vpc_id", query.vpcId);
  if (query.view === "service" && query.service) params.set("service", query.service);
  if (query.view === "application") {
    if (query.seed) params.set("seed", query.seed);
    if (query.appTag) params.set("app_tag", query.appTag);
  }
  params.set("include_inferred", String(query.includeInferred));
  params.set("include_stale", String(query.includeStale));
  params.set("detail", query.detail);
  params.set("max_nodes", String(query.maxNodes));
  return params;
}

/** A view needs a selection before it can be requested (pure). */
export function missingSelection(query: GraphQuery): string | null {
  if (!query.project) return "Select a project.";
  if (query.view === "vpc" && !query.vpcId) return "Select a VPC for the VPC view.";
  if (query.view === "service" && !query.service) return "Select a service for the service view.";
  if (query.view === "application" && !query.seed && !query.appTag) {
    return "Select an entry point or application tag for the application view.";
  }
  return null;
}
