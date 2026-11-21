import { describe, expect, it } from "vitest";

import { edgeVisual } from "./edgeStyle";
import { exportFilename } from "./filenames";
import { DEFAULT_FILTERS, filterGraph, searchNodes, sortedTypeCounts } from "./filtering";
import { buildGraphParams, missingSelection } from "./graphQuery";
import type { GraphQuery } from "./graphTypes";
import { iconUrl, sanitizeManifest, serviceBadge } from "./icons";
import { buildElkGraph, layoutGraph } from "./layout";
import { alb, external, sampleDoc, task, tg } from "./fixtures";

const baseQuery: GraphQuery = {
  project: "demo",
  accountId: "111122223333",
  region: null,
  view: "application",
  vpcId: null,
  service: null,
  seed: alb.id,
  appTag: null,
  includeInferred: true,
  includeStale: false,
  detail: "standard",
  maxNodes: 400,
};

describe("filterGraph", () => {
  it("hides security associations by default but keeps inferred edges", () => {
    const out = filterGraph(sampleDoc, DEFAULT_FILTERS);
    const ids = out.edges.map((e) => e.id);
    expect(ids).not.toContain("e6");
    expect(ids).toContain("e4");
    expect(out.hiddenEdges).toBe(1);
    expect(out.nodes).toHaveLength(sampleDoc.nodes.length);
  });

  it("drops non-verified edges when inferred is off", () => {
    const out = filterGraph(sampleDoc, { showInferred: false, showSecurity: true });
    expect(out.edges.every((e) => e.confidence === "verified")).toBe(true);
    expect(out.edges.map((e) => e.id)).toContain("e6");
  });
});

describe("searchNodes", () => {
  it("matches label, resource id and ARN case-insensitively", () => {
    expect(searchNodes(sampleDoc.nodes, "WEB-TG").map((n) => n.id)).toEqual([tg.id]);
    expect(searchNodes(sampleDoc.nodes, "10.0.9.9").map((n) => n.id)).toEqual([external.id]);
    expect(searchNodes(sampleDoc.nodes, "task/prod/0123").length).toBe(2);
    expect(searchNodes(sampleDoc.nodes, "   ")).toEqual([]);
  });
});

describe("edgeVisual", () => {
  it("encodes confidence as dash pattern", () => {
    const [verified, inferred] = [sampleDoc.edges[2], sampleDoc.edges[3]];
    expect(edgeVisual(verified).strokeDasharray).toBeUndefined();
    expect(edgeVisual(inferred).strokeDasharray).toBe("8 5");
    expect(edgeVisual({ ...verified, confidence: "unknown" }).strokeDasharray).toBe("2 4");
    expect(edgeVisual(inferred).label).toBe("routes_to (inferred)");
  });
});

describe("graph query", () => {
  it("serialises view-specific params only", () => {
    const params = buildGraphParams({ ...baseQuery, vpcId: "vpc-1", detail: "full", includeStale: true });
    expect(params.get("seed")).toBe(alb.id);
    expect(params.get("vpc_id")).toBeNull();
    expect(params.getAll("account_id")).toEqual(["111122223333"]);
    expect(params.get("include_stale")).toBe("true");
    expect(params.get("detail")).toBe("full");
  });

  it("requires a selection for scoped views", () => {
    expect(missingSelection({ ...baseQuery, seed: null })).toMatch(/entry point/);
    expect(missingSelection({ ...baseQuery, view: "vpc" })).toMatch(/VPC/);
    expect(missingSelection({ ...baseQuery, view: "account" })).toBeNull();
  });
});

describe("exportFilename", () => {
  it("includes project, account, view and a UTC timestamp", () => {
    const name = exportFilename({
      project: "demo app",
      accountId: "111122223333",
      view: "vpc",
      extension: "mmd",
      now: new Date("2026-11-20T10:05:06.789Z"),
    });
    expect(name).toBe("cloudlens-demo-app-111122223333-vpc-20261120T100506Z.mmd");
    expect(exportFilename({ project: "../x", accountId: null, view: "account", extension: "png" })).toMatch(
      /^cloudlens-x-all-accounts-account-\d{8}T\d{6}Z\.png$/,
    );
  });
});

describe("icons", () => {
  it("only accepts plain icon file names from the manifest", () => {
    const manifest = sanitizeManifest({
      "load-balancer": "Arch_ELB_48.svg",
      evil: "../../etc/passwd.svg",
      remote: "https://x.example/a.svg",
      bad: 42,
    });
    expect(manifest).toEqual({ "load-balancer": "Arch_ELB_48.svg" });
    expect(iconUrl(manifest, "load-balancer")).toBe("/aws-icons/Arch_ELB_48.svg");
    expect(iconUrl(manifest, "vpc")).toBeNull();
    expect(sanitizeManifest(["x"])).toEqual({});
  });

  it("falls back to service badges", () => {
    expect(serviceBadge("elbv2").text).toBe("ELB");
    expect(serviceBadge("unknownsvc").text).toBe("UNK");
    expect(serviceBadge("ecs", "external").text).toBe("EXT");
  });
});

describe("layout", () => {
  it("nests nodes under parent_group and drops empty groups", () => {
    const elk = buildElkGraph(sampleDoc.nodes, sampleDoc.edges, sampleDoc.groups);
    const account = elk.children?.[0];
    expect(account?.id).toBe("account:111122223333");
    // empty "global" group is dropped; region contains the external node + VPC
    expect(account?.children?.map((c) => c.id)).toEqual(["region:111122223333:us-east-1"]);
    const region = account?.children?.[0];
    const vpc = region?.children?.find((c) => c.id.startsWith("vpc:"));
    expect(region?.children?.some((c) => c.id === external.id)).toBe(true);
    const subnet = vpc?.children?.find((c) => c.id.startsWith("subnet:"));
    expect(subnet?.children?.map((c) => c.id)).toContain(task.id);
    expect(elk.edges).toHaveLength(sampleDoc.edges.length);
  });

  it("produces React Flow nodes with parents before children", async () => {
    const result = await layoutGraph(sampleDoc);
    const index = new Map(result.nodes.map((n, i) => [n.id, i]));
    for (const node of result.nodes) {
      if (node.parentId) {
        expect(index.get(node.parentId)).toBeLessThan(index.get(node.id) ?? -1);
        expect(node.extent).toBe("parent");
      }
    }
    const resources = result.nodes.filter((n) => n.type === "resource");
    expect(resources).toHaveLength(sampleDoc.nodes.length);
    expect(resources.every((n) => Number.isFinite(n.position.x) && Number.isFinite(n.position.y))).toBe(true);
    expect(result.edges.map((e) => e.id)).not.toContain("e6");
    const inferred = result.edges.find((e) => e.id === "e4");
    expect(inferred?.style?.strokeDasharray).toBe("8 5");
  });

  it("orders type counts largest first", () => {
    expect(sortedTypeCounts(sampleDoc.stats.by_resource_type)[0]).toEqual(["ecs-task", 2]);
  });
});
