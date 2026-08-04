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
import { TABS as NOTIFICATION_TABS } from "../components/NotificationsHub";

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

  it("hides Tier 4 from staff too when mvpMode is on", () => {
    // `mvpMode` is the broader switch: it precedes the role check, so even the
    // role that is allowed to see Tier 4 loses it. Without this, "mvpMode hides
    // Tier 4" would be false for exactly one role and nobody would notice.
    expect(
      canSee("price-books", RESOLVED(["admin", "platform_admin"], true)),
    ).toBe(false);
  });

  it("shows Tier 4 to platform_admin when mvpMode is off", () => {
    expect(
      canSee("price-books", RESOLVED(["admin", "platform_admin"], false)),
    ).toBe(true);
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

describe("canSee — Tier 4 is platform_admin only", () => {
  // The ERP is the authoritative price and invoice, so a customer's own admin
  // gets no second editable copy. Runsheet staff keep access to diagnose and to
  // run a tenant with no ERP.
  const TIER_4 = [
    "accounts",
    "price-books",
    "pricing-rules",
    "contracts",
    "payments",
    "ar-aging",
    "stripe",
  ] as const;

  it("gates every Tier 4 module on platform_admin", () => {
    // Derived from the registry rather than from the list above, so a Tier 4
    // module added later without the role requirement fails here instead of
    // shipping visible to every tenant admin.
    const unguarded = registeredModuleIds().filter((id) => {
      const descriptor = moduleDescriptor(id);
      return (
        descriptor?.tier === 4 &&
        !descriptor.requiredRoles?.includes("platform_admin")
      );
    });
    expect(unguarded).toEqual([]);
  });

  it("lists exactly the Tier 4 ids this suite expects", () => {
    // Pins the membership itself. Moving a module in or out of Tier 4 is a
    // product decision, so it should have to be made here too.
    const actual = registeredModuleIds()
      .filter((id) => moduleDescriptor(id)?.tier === 4)
      .sort();
    expect(actual).toEqual([...TIER_4].sort());
  });

  it("refuses a tenant admin", () => {
    for (const id of TIER_4) {
      expect(canSee(id, RESOLVED(["admin"]))).toBe(false);
    }
  });

  it("refuses a dispatcher", () => {
    for (const id of TIER_4) {
      expect(canSee(id, RESOLVED(["dispatcher"]))).toBe(false);
    }
  });

  it("allows staff holding platform_admin alongside an operations role", () => {
    for (const id of TIER_4) {
      expect(canSee(id, RESOLVED(["admin", "platform_admin"]))).toBe(true);
    }
  });

  it("keeps the operations half of CommerceHub visible to a dispatcher", () => {
    // If the Tier 4 gate ever widened to the whole hub, the MVP would lose
    // capabilities 6 and 7. These two must stay reachable without staff rights.
    expect(canSee("invoices", RESOLVED(["dispatcher"]))).toBe(true);
    expect(canSee("reconciliation", RESOLVED(["dispatcher"]))).toBe(true);
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
    // Otherwise the common case flickers on every page load. `depots` is the
    // exemplar now that `settings` is gone — it was the only ungated *nav*
    // item, so every Sidebar destination is role-gated and this property has to
    // be demonstrated on a tab-level module instead.
    expect(canSee("depots", { roles: null, mvpMode: false })).toBe(true);
  });

  it("treats an empty role list the same as null", () => {
    expect(canSee("admin", RESOLVED([]))).toBe(false);
    expect(canSee("depots", RESOLVED([]))).toBe(true);
  });
});

describe("the Settings nav item is gone", () => {
  it("no longer registers a settings module", () => {
    // It emptied out one piece at a time — password change to
    // `/dashboard/profile`, Support deleted, Data Import to AdminHub — leaving a
    // top-level nav item holding a single admin-policy tab. That tab moved to
    // AdminHub beside Agent Monitoring and the nav entry was removed, which also
    // settles the "pending the driver/web-app decision" note that used to live
    // here: a driver now matches no nav item and gets the shell's
    // "for dispatchers and administrators" wall.
    expect(moduleDescriptor("settings")).toBeUndefined();
    expect(canSee("settings", RESOLVED(["admin"]))).toBe(false);
  });

  it("makes Agent Settings admin-only, matching the backend", () => {
    // `Agents/api_authz.py`: PATCH /agent/config/autonomy, POST
    // /agent/{id}/pause|resume and DELETE /agent/memory/{id} all require
    // `admin` via agent_admin_dependency. The old note claimed the tab was
    // "read-only for non-admins already", which covered the autonomy radios
    // only — pause/resume and memory deletion were ungated in UI and API both.
    expect(moduleDescriptor("agent-settings")?.requiredRoles).toEqual([
      "admin",
    ]);
    expect(canSee("agent-settings", RESOLVED(["admin"]))).toBe(true);
    for (const roles of [
      ["dispatcher"],
      ["driver"],
      ["platform_admin"],
      ["whatever-an-operator-typed"],
      [],
    ]) {
      expect(canSee("agent-settings", RESOLVED(roles))).toBe(false);
    }
  });

  it("still lets a dispatcher read the autonomy level elsewhere", () => {
    // Losing the tab must not blind the shift. `AgentAutonomyBanner` renders in
    // OperationsControlView on `/ops/control`, and GET /agent/config/autonomy is
    // gated to admin+dispatcher — so a dispatcher keeps the same information in
    // the surface where they actually work.
    expect(canSee("control", RESOLVED(["dispatcher"]))).toBe(true);
  });

  it("no longer registers a security tab", () => {
    // It rendered <ChangePassword />, the same component ProfilePage renders, so
    // it was a second door onto one form. Removed rather than gated. `canSee`
    // fails closed on unknown ids, so a stray reference now hides rather than
    // renders — and the drift guard below proves no surface still asks for it.
    expect(moduleDescriptor("security")).toBeUndefined();
    expect(canSee("security", RESOLVED(["admin"]))).toBe(false);
  });

  it("gates the Data Import tab in AdminHub", () => {
    // import_endpoints.py::IMPORT_ADMIN_ROLES — admin only. A dispatcher is
    // deliberately excluded: one CSV can overwrite the customer, asset, driver
    // or inventory master data for the entire tenant, and those rows then drive
    // pricing, routing and readiness. That is administration, not shift work.
    expect(canSee("import", RESOLVED(["admin"]))).toBe(true);
    expect(canSee("import", RESOLVED(["dispatcher"]))).toBe(false);
    expect(canSee("import", RESOLVED(["driver"]))).toBe(false);
  });

  it("keeps the rest of AdminHub reachable for an admin", () => {
    // Narrowing Data Import must not narrow the hub around it.
    expect(canSee("admin", RESOLVED(["admin"]))).toBe(true);
    expect(canSee("agents", RESOLVED(["admin"]))).toBe(true);
  });

  it("no longer registers a support tab", () => {
    // It was a ticketing UI for the legacy Nigerian last-mile CRM: the list
    // endpoint sits behind LEGACY_NG_DELIVERY_ENABLED (false everywhere) and
    // create/detail/update were never implemented, so the create-ticket modal
    // could not work in any environment.
    expect(moduleDescriptor("support")).toBeUndefined();
    expect(canSee("support", RESOLVED(["admin"]))).toBe(false);
  });

  it("keeps the customer notification surfaces, on their own route", () => {
    // Support's other two tabs were real and backed by notifications/api. They
    // moved to /dashboard/notifications rather than being deleted with it.
    expect(moduleDescriptor("notification-history")).toBeDefined();
    expect(moduleDescriptor("notification-settings")).toBeDefined();
    for (const role of ["admin", "dispatcher"]) {
      expect(canSee("notification-history", RESOLVED([role]))).toBe(true);
      expect(canSee("notification-settings", RESOLVED([role]))).toBe(true);
    }
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
    const items = [{ id: "depots" }, { id: "admin" }, { id: "control" }];
    expect(visibleByCanSee(items, RESOLVED(["dispatcher"]))).toEqual([
      { id: "depots" },
      { id: "control" },
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
      // No SettingsPage: it was a one-tab shell and its tab (agent-settings)
      // moved into AdminHub, so ADMIN_TABS above now covers it.
      ["NotificationsPage", NOTIFICATION_TABS],
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
