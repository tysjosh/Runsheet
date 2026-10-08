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

import { TABS as COMMERCE_TABS } from "../components/CommerceHub";
import { TABS as COMPLIANCE_TABS } from "../components/ComplianceHub";
import {
  TAB_GATES as CUSTOMER_TAB_GATES,
  TABS as CUSTOMER_TABS,
} from "../components/customers/CustomersHub";
import { TABS as FLEET_TABS } from "../components/FleetDashboard";
import { TABS as FUEL_OPS_TABS } from "../components/FuelOpsPage";
import {
  SETTINGS_SECTIONS,
  sectionVisible,
} from "../components/settings/SettingsPage";
import {
  CUSTOMER_ROLE,
  canSee,
  hasAnyRole,
  isCustomerRole,
  moduleDescriptor,
  registeredModuleIds,
  visibleByCanSee,
} from "./modules";
import { NAV_SECTIONS } from "./nav";

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

  it.each(["invoices", "reconciliation"])(
    "hides %s from drivers and shows it to dispatchers and admins (OI-19)",
    (id) => {
      expect(canSee(id, RESOLVED(["driver"]))).toBe(false);
      expect(canSee(id, RESOLVED(["dispatcher"]))).toBe(true);
      expect(canSee(id, RESOLVED(["admin"]))).toBe(true);
    },
  );

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

describe("Settings is the home of Setup and Admin (UI revamp R10.4)", () => {
  it("registers settings for the operations roles", () => {
    expect(moduleDescriptor("settings")?.requiredRoles).toEqual([
      "admin",
      "dispatcher",
    ]);
    expect(canSee("settings", RESOLVED(["dispatcher"]))).toBe(true);
    expect(canSee("settings", RESOLVED(["driver"]))).toBe(false);
  });

  it("does not widen Admin sections to dispatchers", () => {
    // Admin's tabs used to sit behind the admin-only `admin` nav item. Under
    // Settings each keeps that gate, so a dispatcher sees only Setup's
    // sections (and notification rules, which they could already edit).
    const dispatcher = SETTINGS_SECTIONS.filter((s) =>
      sectionVisible(s, ["dispatcher"]),
    ).map((s) => s.id);
    expect(dispatcher).toEqual([
      "company",
      "road-restrictions",
      "tax",
      "exemptions",
      "notifications",
    ]);
    for (const s of SETTINGS_SECTIONS.filter((x) => x.gate === "admin")) {
      expect(sectionVisible(s, ["dispatcher"])).toBe(false);
    }
  });

  it("shows an admin every section except platform_admin-only Stripe", () => {
    const admin = SETTINGS_SECTIONS.filter((s) => sectionVisible(s, ["admin"]));
    expect(admin.map((s) => s.id)).not.toContain("stripe");
    expect(admin).toHaveLength(SETTINGS_SECTIONS.length - 1);
    expect(
      sectionVisible(
        SETTINGS_SECTIONS.find((s) => s.id === "stripe") as never,
        ["admin", "platform_admin"],
      ),
    ).toBe(true);
  });

  it("makes Agent Settings admin-only, matching the backend", () => {
    // `Agents/api_authz.py`: PATCH /agent/config/autonomy and DELETE
    // /agent/memory/{id} require `admin`; pause/resume additionally require
    // `platform_admin` (the page hides those controls otherwise).
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

  it("still lets a dispatcher read the autonomy level on Live", () => {
    expect(canSee("control", RESOLVED(["dispatcher"]))).toBe(true);
    expect(canSee("live", RESOLVED(["dispatcher"]))).toBe(true);
  });

  it("no longer registers a security or support tab", () => {
    expect(moduleDescriptor("security")).toBeUndefined();
    expect(moduleDescriptor("support")).toBeUndefined();
  });

  it("gates Data Import to admin", () => {
    // import_endpoints.py::IMPORT_ADMIN_ROLES — admin only: one CSV can
    // overwrite the tenant's master data.
    expect(canSee("import", RESOLVED(["admin"]))).toBe(true);
    expect(canSee("import", RESOLVED(["dispatcher"]))).toBe(false);
    expect(canSee("import", RESOLVED(["driver"]))).toBe(false);
  });

  it("keeps the customer notification surfaces for both operations roles", () => {
    for (const role of ["admin", "dispatcher"]) {
      expect(canSee("notification-history", RESOLVED([role]))).toBe(true);
      expect(canSee("notification-settings", RESOLVED([role]))).toBe(true);
    }
  });

  it("registers system-health for platform_admin only", () => {
    expect(canSee("system-health", RESOLVED(["platform_admin"]))).toBe(true);
    expect(canSee("system-health", RESOLVED(["admin"]))).toBe(false);
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
    section.items.map((item) => [`${section.id}/${item.id}`, item.id]),
  );

  it.each(navItems)("nav item %s is registered", (_label, id) => {
    expect(registered.has(id as string)).toBe(true);
  });

  const hubTabs: [string, string][] = (
    [
      ["CommerceHub", COMMERCE_TABS.map((t) => t.id)],
      ["ComplianceHub", COMPLIANCE_TABS.map((t) => t.id)],
      ["FuelOpsPage", FUEL_OPS_TABS.map((t) => t.id)],
      // Fleet's Trucks and Inventory ride on the `fleet` id; Drivers keeps
      // its own `drivers` gate.
      ["FleetDashboard", ["fleet", "drivers"]],
      [
        "CustomersHub",
        CUSTOMER_TABS.flatMap((t) => CUSTOMER_TAB_GATES[t.id] ?? [t.id]),
      ],
      ["SettingsPage", SETTINGS_SECTIONS.flatMap((s) => [s.moduleId, s.gate])],
    ] as const
  ).flatMap(([hub, ids]) =>
    ids.map((id) => [`${hub}/${id}`, id] as [string, string]),
  );

  it.each(hubTabs)("hub tab %s is registered", (_label, id) => {
    expect(registered.has(id)).toBe(true);
  });

  it("has no duplicate ids", () => {
    const ids = registeredModuleIds();
    expect(new Set(ids).size).toBe(ids.length);
  });

  // Ids that gate a route rather than a nav item or tab: `control` is the
  // /dashboard/control route the Live nav item opens. `system-health` is
  // rendered by task 3.9 (Settings → System health).
  const ROUTE_GATES = ["control", "system-health"];

  it("registers nothing that no surface renders", () => {
    // The reverse direction: a stale entry is harmless at runtime but it rots,
    // and a reader cannot tell a dead id from a live one.
    const rendered = new Set([
      ...navItems.map(([, id]) => id as string),
      ...hubTabs.map(([, id]) => id),
      ...ROUTE_GATES,
    ]);
    expect(registeredModuleIds().filter((id) => !rendered.has(id))).toEqual([]);
  });

  it("covers every Fleet tab", () => {
    expect(FLEET_TABS.map((t) => t.id)).toEqual([
      "trucks",
      "drivers",
      "inventory",
    ]);
  });
});

describe("canSee — a customer sees no staff module (portal §10.1)", () => {
  it("hides every registered module, including ones without requiredRoles", () => {
    const ids = registeredModuleIds();
    expect(ids.some((id) => !moduleDescriptor(id)?.requiredRoles)).toBe(true);
    for (const id of ids) {
      expect(canSee(id, RESOLVED([CUSTOMER_ROLE]))).toBe(false);
      expect(canSee(id, RESOLVED([CUSTOMER_ROLE], true))).toBe(false);
    }
  });

  it("hides modules even when a customer claim is mixed with staff roles", () => {
    expect(canSee("today", RESOLVED(["admin", "customer"]))).toBe(false);
    expect(canSee("depots", RESOLVED([" Customer "]))).toBe(false);
  });

  it("leaves staff visibility unchanged", () => {
    expect(canSee("today", RESOLVED(["dispatcher"]))).toBe(true);
    expect(canSee("depots", RESOLVED(["driver"]))).toBe(true);
  });

  it("isCustomerRole matches the role exactly", () => {
    expect(isCustomerRole(["customer"])).toBe(true);
    expect(isCustomerRole(["customers", "admin"])).toBe(false);
    expect(isCustomerRole(null)).toBe(false);
  });
});
