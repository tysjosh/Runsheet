/**
 * @jest-environment node
 *
 * Redirect map (R3.1, design.md §4) and the shell's module paths.
 */
import { existsSync, readdirSync, statSync } from "node:fs";
import path from "node:path";
import {
  DASHBOARD_ITEM_PATH,
  dashboardHref,
} from "../app/dashboard/shell-context";
import { nextRedirects, REDIRECTS } from "./redirects";

const APP = path.join(__dirname, "..", "app");

/** Does an app-router page exist for this concrete or dynamic path? */
function routeExists(urlPath: string): boolean {
  const clean = urlPath.split("?")[0];
  const segments = clean.split("/").filter(Boolean);
  function walk(dir: string, rest: string[]): boolean {
    if (rest.length === 0) {
      if (existsSync(path.join(dir, "page.tsx"))) return true;
      // Route groups like (today) hold the index page.
      return readdirSync(dir).some(
        (d) =>
          d.startsWith("(") &&
          statSync(path.join(dir, d)).isDirectory() &&
          existsSync(path.join(dir, d, "page.tsx")),
      );
    }
    const [head, ...tail] = rest;
    const entries = readdirSync(dir).filter((d) =>
      statSync(path.join(dir, d)).isDirectory(),
    );
    const candidates = entries.filter(
      (d) =>
        d === head ||
        (head.startsWith(":") && d.startsWith("[") && d.endsWith("]")) ||
        d.startsWith("("),
    );
    return candidates.some((d) =>
      walk(path.join(dir, d), d.startsWith("(") && d !== head ? rest : tail),
    );
  }
  return walk(APP, segments);
}

describe("REDIRECTS", () => {
  it("has the 22 design.md §4 rows (row 5 waits for task 3.6) plus /ops", () => {
    const rows = REDIRECTS.map((r) => r.row).sort((a, b) => a - b);
    expect(rows).toEqual([
      0, 1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21,
      22, 23,
    ]);
  });

  it.each(REDIRECTS.map((r) => [r.source, r.destination]))(
    "%s → %s points at a page that exists, and the source page is gone",
    (source, destination) => {
      expect(routeExists(destination)).toBe(true);
      expect(routeExists(source)).toBe(false);
    },
  );

  it("keeps dynamic segments on both sides", () => {
    for (const r of REDIRECTS) {
      const params = (s: string) => (s.match(/:[a-zA-Z]+/g) ?? []).sort();
      expect(params(r.destination)).toEqual(params(r.source));
    }
  });

  it("lists a more specific source before its prefix", () => {
    const sources = REDIRECTS.map((r) => r.source);
    expect(sources.indexOf("/ops/scheduling/:id/cargo")).toBeLessThan(
      sources.indexOf("/ops/scheduling/:id"),
    );
    expect(sources.indexOf("/ops/fuel/tanks/:id")).toBeLessThan(
      sources.indexOf("/ops/fuel"),
    );
  });

  it("is permanent (308) in next.config", () => {
    for (const r of nextRedirects()) expect(r.permanent).toBe(true);
  });

  it("leaves /ops/command alone until task 3.6", () => {
    expect(REDIRECTS.some((r) => r.source.startsWith("/ops/command"))).toBe(
      false,
    );
    expect(routeExists("/ops/command")).toBe(true);
  });
});

describe("DASHBOARD_ITEM_PATH", () => {
  it("maps new and retired module ids to live routes", () => {
    expect(DASHBOARD_ITEM_PATH.live).toBe("/dashboard/control");
    expect(DASHBOARD_ITEM_PATH.orders).toBe("/dashboard/orders");
    expect(DASHBOARD_ITEM_PATH.settings).toBe("/dashboard/settings");
    expect(DASHBOARD_ITEM_PATH.drivers).toBe("/dashboard/fleet?tab=drivers");
    expect(DASHBOARD_ITEM_PATH.notifications).toBe(
      "/dashboard/customers?tab=communications",
    );
    for (const p of Object.values(DASHBOARD_ITEM_PATH)) {
      expect(routeExists(p)).toBe(true);
    }
  });

  it("replaces an existing tab when a tab is given", () => {
    expect(dashboardHref("setup", "depots")).toBe(
      "/dashboard/settings?tab=depots",
    );
    expect(dashboardHref("admin", "weather-alerts")).toBe(
      "/dashboard/settings?tab=weather-alerts",
    );
    expect(dashboardHref("fleet")).toBe("/dashboard/fleet");
  });
});
