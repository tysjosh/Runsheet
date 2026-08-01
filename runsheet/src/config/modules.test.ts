/**
 * Module visibility predicate + registry drift guard.
 *
 * Two things are under test:
 *
 *  1. **`canSee` semantics** — the MVP tier rule, the exact-match role rule, and
 *     the unresolved-roles rule. The exact-match cases matter most: they mirror
 *     the backend's Req 4.2, and a permissive UI gate in front of a strict
 *     backend enables controls that then 403.
 *  2. **Registry drift** — every id the sidebar and every hub actually renders
 *     must exist in the registry. `canSee` fails closed on an unknown id, which
 *     is only a safe default if something loudly proves the real ids are all
 *     registered. That is this file's job; without it a typo silently deletes
 *     navigation.
 */

import { TABS as ADMIN_TABS } from "../components/AdminHub";
import { TABS as COMMERCE_TABS } from "../components/CommerceHub";
import { TABS as COMPLIANCE_TABS } from "../components/ComplianceHub";
import { TABS as FUEL_OPS_TABS } from "../components/FuelOpsPage";
import { TABS as SETTINGS_TABS } from "../components/SettingsPage";
import { TABS as SETUP_TABS } from "../components/SetupHub";
import { NAV_SECTIONS } from "../components/Sidebar";
import {
  canSee,
  hasAnyRole,
  moduleDescriptor,
  registeredModuleIds,
  visibleByCanSee,
} from "./modules";

// Every test pins `mvpMode` explicitly rather than relying on the
// `NEXT_PUBLIC_MVP_MODE` default, so these cases keep their meaning if the
// default is ever flipped.
const RESOLVED = (roles: string[], mvpMode = false) => ({ roles, mvpMode });

describe("canSee — unknown ids fail closed", () => {
  it("hides an id that is not in the registry", () => {
    expect(canSee("not-a-module", RESOLVED(["admin"]))).toBe(false);
    expect(canSee("", RESOLVED(["admin"]))).toBe(false);
  });
});

describe("canSee — MVP mode hides only Tier 4", () => {
  it("hides Tier 4 when mvpMode is on", () => {
    expect(moduleDescriptor("price-books")?.tier).toBe(4);
    expect(canSee("price-books", RESOLVED(["admin"], true))).toBe(false);
  });

  it("shows Tier 4 when mvpMode is off", () => {
    expect(canSee("price-books", RESOLVED(["admin"], false))).toBe(true);
  });

  it("keeps invoices and reconciliation visible in mvpMode", () => {
    // Capabilities 6 and 7 of the pipeline. If these ever land in Tier 4 the
    // MVP loses its billing half, so assert the tier as well as the visibility.
    expect(moduleDescriptor("invoices")?.tier).toBe(1);
    expect(moduleDescriptor("reconciliation")?.tier).toBe(1);
    expect(canSee("invoices", RESOLVED(["dispatcher"], true))).toBe(true);
    expect(canSee("reconciliation", RESOLVED(["dispatcher"], true))).toBe(true);
  });

  it("leaves Tier 1-3 alone in mvpMode", () => {
    for (const id of ["depots", "weather-alerts", "tax", "ifta"]) {
      expect(canSee(id, RESOLVED(["dispatcher"], true))).toBe(true);
    }
  });
});

describe("canSee — roles are matched exactly, never by substring", () => {
  it("grants an exactly held role", () => {
    expect(canSee("admin", RESOLVED(["admin"]))).toBe(true);
    expect(canSee("feature-flags", RESOLVED(["admin"]))).toBe(true);
  });

  it("refuses admin_ops for a requirement of admin", () => {
    // Mirrors the backend's Req 4.2. A substring matcher would grant this.
    expect(canSee("admin", RESOLVED(["admin_ops"]))).toBe(false);
    expect(canSee("feature-flags", RESOLVED(["admin_ops"]))).toBe(false);
  });

  it("refuses platform_admin for a requirement of admin", () => {
    // The staff role is additive and implies nothing, exactly as in
    // `auth.authorization.require_role`. Staff accounts carry `admin` too.
    expect(canSee("admin", RESOLVED(["platform_admin"]))).toBe(false);
    expect(canSee("admin", RESOLVED(["platform_admin", "admin"]))).toBe(true);
  });

  it("refuses a role that merely contains the required one", () => {
    expect(canSee("today", RESOLVED(["lead-dispatcher"]))).toBe(false);
    expect(canSee("today", RESOLVED(["ops-admin-eu"]))).toBe(false);
  });

  it("normalizes case and surrounding whitespace", () => {
    // A role arrives from a JSON claim; " Admin" is the same grant as "admin".
    expect(canSee("admin", RESOLVED([" ADMIN "]))).toBe(true);
  });

  it("grants when any one of several required roles is held", () => {
    expect(canSee("dispatch", RESOLVED(["dispatcher"]))).toBe(true);
    expect(canSee("dispatch", RESOLVED(["admin"]))).toBe(true);
    expect(canSee("dispatch", RESOLVED(["driver"]))).toBe(false);
  });
});

describe("canSee — unresolved roles behave as no roles", () => {
  it("hides a role-gated module while roles are null", () => {
    expect(canSee("admin", { roles: null, mvpMode: false })).toBe(false);
    expect(canSee("dispatch", { roles: null, mvpMode: false })).toBe(false);
    expect(canSee("import", { roles: null, mvpMode: false })).toBe(false);
  });

  it("shows an ungated module immediately, before roles resolve", () => {
    // Otherwise the common case flickers on every page load.
    expect(canSee("settings", { roles: null, mvpMode: false })).toBe(true);
    expect(canSee("security", { roles: null, mvpMode: false })).toBe(true);
  });

  it("treats an empty role list the same as null", () => {
    expect(canSee("admin", RESOLVED([]))).toBe(false);
    expect(canSee("settings", RESOLVED([]))).toBe(true);
  });
});

describe("settings is reachable by every role", () => {
  it("is never role-gated, because it holds password change", () => {
    // Getting this wrong locks a user out of changing their own password.
    expect(moduleDescriptor("settings")?.requiredRoles).toBeUndefined();
    expect(moduleDescriptor("security")?.requiredRoles).toBeUndefined();
    for (const roles of [
      ["admin"],
      ["dispatcher"],
      ["driver"],
      ["platform_admin"],
      ["whatever-an-operator-typed"],
      [],
    ]) {
      expect(canSee("settings", RESOLVED(roles))).toBe(true);
      expect(canSee("security", RESOLVED(roles))).toBe(true);
    }
  });

  it("gates the Data Import tab instead of the Settings nav item", () => {
    // import_endpoints.py::_require_import_role — admin or dispatcher.
    expect(canSee("import", RESOLVED(["admin"]))).toBe(true);
    expect(canSee("import", RESOLVED(["dispatcher"]))).toBe(true);
    expect(canSee("import", RESOLVED(["driver"]))).toBe(false);
  });
});

describe("hasAnyRole", () => {
  it("is exact, case-insensitive, and fails closed on absent input", () => {
    expect(hasAnyRole(["dispatcher"], ["dispatcher", "admin"])).toBe(true);
    expect(hasAnyRole(["Admin"], ["dispatcher", "admin"])).toBe(true);
    expect(hasAnyRole(["dispatcher_lead"], ["dispatcher", "admin"])).toBe(
      false,
    );
    expect(hasAnyRole(["ops_admin"], ["dispatcher", "admin"])).toBe(false);
    expect(hasAnyRole([], ["admin"])).toBe(false);
    expect(hasAnyRole(null, ["admin"])).toBe(false);
    expect(hasAnyRole(undefined, ["admin"])).toBe(false);
  });
});

describe("visibleByCanSee", () => {
  it("filters in place and preserves order", () => {
    const items = [{ id: "settings" }, { id: "admin" }, { id: "security" }];
    expect(visibleByCanSee(items, RESOLVED(["dispatcher"]))).toEqual([
      { id: "settings" },
      { id: "security" },
    ]);
  });
});

// ─── Registry drift guard ────────────────────────────────────────────────────

describe("registry drift guard", () => {
  const registered = new Set(registeredModuleIds());

  const navItems = NAV_SECTIONS.flatMap((section) =>
    section.items.map((item) => [`${section.label}/${item.id}`, item.id]),
  );

  it.each(navItems)("nav item %s is registered", (_label, id) => {
    expect(registered.has(id as string)).toBe(true);
  });

  const hubTabs: [string, string][] = (
    [
      ["CommerceHub", COMMERCE_TABS],
      ["ComplianceHub", COMPLIANCE_TABS],
      ["AdminHub", ADMIN_TABS],
      ["SetupHub", SETUP_TABS],
      ["FuelOpsPage", FUEL_OPS_TABS],
      ["SettingsPage", SETTINGS_TABS],
    ] as const
  ).flatMap(([hub, tabs]) =>
    tabs.map((tab) => [`${hub}/${tab.id}`, tab.id] as [string, string]),
  );

  it.each(hubTabs)("hub tab %s is registered", (_label, id) => {
    expect(registered.has(id)).toBe(true);
  });

  it("has no duplicate ids", () => {
    const ids = registeredModuleIds();
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("registers nothing that no surface renders", () => {
    // The reverse direction: a stale entry is harmless at runtime but it rots,
    // and a reader cannot tell a dead id from a live one.
    const rendered = new Set([
      ...navItems.map(([, id]) => id as string),
      ...hubTabs.map(([, id]) => id),
    ]);
    expect(registeredModuleIds().filter((id) => !rendered.has(id))).toEqual([]);
  });
});
