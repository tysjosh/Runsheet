/**
 * Module visibility — one predicate for "should this user see this thing".
 *
 * Two questions used to be answered in different places and in different ways:
 * whether a module is part of the MVP surface at all, and whether the signed-in
 * user holds the role it needs. Keeping them apart means the two passes fight:
 * a nav item can be role-visible but MVP-hidden, a tab can be MVP-visible but
 * role-hidden, and the "is anything left in this section" question has no single
 * answer. {@link canSee} answers both at once so every call site agrees.
 *
 * This is presentation only. The backend re-verifies the session and re-checks
 * the role on every request, so hiding a control is a usability decision, never
 * the security boundary. A user who types a hidden URL gets redirected by the
 * shell and, if they get past that, a 403 from the API.
 *
 * ## Source of truth
 *
 * The registry is a module-level constant today. {@link canSee} takes the
 * caller's context as an argument rather than reading a global, so the registry
 * can later be fetched per tenant (a customer who does not license billing, say)
 * without touching a single call site.
 */

/**
 * The canonical backend roles, mirroring
 * `auth.supertokens_init.CANONICAL_ROLES`.
 *
 * `ops_manager` is deliberately absent — it was retired because it gated
 * nothing. `platform_admin` is the Runsheet-staff role; it is *additive* and
 * implies nothing, exactly as on the backend, so staff accounts carry `admin`
 * alongside it.
 */
export type Role = "admin" | "dispatcher" | "driver" | "platform_admin";

/**
 * Why a module might be deferrable.
 *
 * - **1** — the MVP pipeline: intake, forecast, plan, load, deliver, invoice,
 *   reconcile. Without it there is no product.
 * - **2** — a dispatcher cannot work a full shift without it.
 * - **3** — legally required (DOT / FMCSA / IRS / IFTA). Not deferrable for
 *   regulatory reasons even though no dispatcher touches it daily.
 * - **4** — pricing and billing, which the customer's ERP already owns.
 *   Every Tier 4 module is gated to `platform_admin`, so it is Runsheet-staff
 *   only: a customer's own `admin` does not see it, because the authoritative
 *   price and invoice live in their ERP and a second editable copy here invites
 *   disagreement about which one is real. Staff retain access to diagnose and
 *   to run a tenant that has no ERP.
 *
 *   Tier 4 is also the only tier `mvpMode` hides. The two controls are
 *   independent and compose: the role gate decides *who*, `mvpMode` decides
 *   *whether at all*, so setting `NEXT_PUBLIC_MVP_MODE` hides these from staff
 *   as well.
 */
export type ModuleTier = 1 | 2 | 3 | 4;

export interface ModuleDescriptor {
  id: string;
  tier: ModuleTier;
  /**
   * Roles that may see this module. Omitted means any signed-in user.
   *
   * Matching is **exact**, never substring, mirroring the backend's Req 4.2:
   * `admin_ops` does not satisfy `admin`, and neither does `platform_admin`.
   * There is no implication graph here for the same reason there is none in
   * `auth.authorization.require_role` — it would silently widen every gate at
   * once, including ones nobody re-reviewed.
   */
  requiredRoles?: readonly Role[];
  /** Why this module sits in its tier. Kept short; read as a justification. */
  note?: string;
}

/** Context a visibility decision is made against. */
export interface VisibilityContext {
  /**
   * The caller's roles, or `null` when the session has not resolved yet.
   *
   * `null` is treated as **no roles**, not as "allow": a role-gated item must
   * never flash visible and then disappear. Modules with no `requiredRoles`
   * still show immediately, so the common case does not flicker.
   */
  roles: readonly string[] | null;
  /** Overrides the {@link NEXT_PUBLIC_MVP_MODE} default. Tests pass this. */
  mvpMode?: boolean;
}

// ─── Role matching ───────────────────────────────────────────────────────────

/**
 * True when `roles` contains at least one of `allowed`, compared exactly.
 *
 * Comparison trims surrounding whitespace and lowercases, because a role
 * arrives from a JSON claim and `" Admin"` is the same grant as `"admin"`.
 * It does **not** match substrings: `lead-dispatcher` is a different role from
 * `dispatcher` and the backend will refuse it, so the UI must too. A permissive
 * UI gate here is worse than a strict one — it enables a control that then 403s.
 */
export function hasAnyRole(
  roles: readonly string[] | null | undefined,
  allowed: readonly string[],
): boolean {
  if (!roles || roles.length === 0) return false;
  const wanted = new Set(allowed.map((r) => r.trim().toLowerCase()));
  for (const raw of roles) {
    if (typeof raw !== "string") continue;
    const normalized = raw.trim().toLowerCase();
    if (normalized && wanted.has(normalized)) return true;
  }
  return false;
}

// ─── MVP mode ────────────────────────────────────────────────────────────────

/**
 * Whether Tier 4 modules are hidden, read from `NEXT_PUBLIC_MVP_MODE`.
 *
 * **Defaults to `false`.** The Tier 4 assignments below ship in the registry
 * ready to switch on, but nothing is hidden until the deferrable list is
 * confirmed — flipping this default is a product decision, not a code one.
 */
export function mvpModeDefault(): boolean {
  return process.env.NEXT_PUBLIC_MVP_MODE === "true";
}

// ─── The registry ────────────────────────────────────────────────────────────

/**
 * Every gateable surface: sidebar nav ids and hub tab ids in one namespace.
 *
 * Ids are flat and global rather than scoped per hub, because a tab id is
 * already unique across the app and a flat lookup keeps `canSee(id, ctx)` a
 * one-argument question at every call site. The drift guard in
 * `modules.test.ts` fails if a nav item or hub tab exists without an entry
 * here, which is what makes the fail-closed unknown-id rule safe.
 */
const MODULES: readonly ModuleDescriptor[] = [
  // ── Sidebar: Operations ───────────────────────────────────────────────────
  //
  // The web app is the dispatcher/admin surface; drivers use the separate
  // `driver-app`. So the operational nav requires `admin` or `dispatcher`
  // throughout, and a driver-role user signing in here sees almost nothing.
  {
    id: "today",
    tier: 1,
    requiredRoles: ["admin", "dispatcher"],
    note: "The pipeline dashboard.",
  },
  {
    id: "notifications",
    tier: 2,
    requiredRoles: ["admin", "dispatcher"],
    note: "Customer comms a dispatcher chases during a shift.",
  },
  {
    id: "fleet",
    tier: 1,
    requiredRoles: ["admin", "dispatcher"],
    note: "Load planning cannot run without assets.",
  },
  {
    id: "dispatch",
    tier: 1,
    requiredRoles: ["admin", "dispatcher"],
    note: "The plan itself.",
  },
  {
    id: "drivers",
    tier: 1,
    requiredRoles: ["admin", "dispatcher"],
    note: "Assignment needs a driver roster.",
  },
  {
    id: "fuel-ops",
    tier: 1,
    requiredRoles: ["admin", "dispatcher"],
    note: "Tank telemetry is the runout forecast's input.",
  },
  {
    id: "compliance",
    tier: 3,
    requiredRoles: ["admin", "dispatcher"],
    note: "DOT/IRS surfaces; every tab inside is Tier 3.",
  },
  {
    id: "control",
    tier: 2,
    requiredRoles: ["admin", "dispatcher"],
    note: "Live shift monitoring.",
  },

  // ── Sidebar: Commerce ─────────────────────────────────────────────────────
  {
    id: "customers",
    tier: 1,
    requiredRoles: ["admin", "dispatcher"],
    note: "An order needs a customer and a tank.",
  },
  {
    id: "billing",
    tier: 1,
    requiredRoles: ["admin", "dispatcher"],
    note: "Holds Invoices and Reconciliation — capabilities 6 and 7.",
  },
  {
    id: "analytics",
    tier: 2,
    requiredRoles: ["admin", "dispatcher"],
    note: "Utilization and consumption reporting.",
  },

  // ── Sidebar: Workspace ────────────────────────────────────────────────────
  {
    id: "setup",
    tier: 1,
    requiredRoles: ["admin", "dispatcher"],
    note: "Holds Depots, which the route agent resolves at location index 0.",
  },
  {
    id: "admin",
    tier: 2,
    requiredRoles: ["admin"],
    note: "Contains Feature Flags, whose API 403s for non-admins.",
  },
  // There is deliberately no `settings` entry.
  //
  // It emptied out one piece at a time: password change moved to
  // `/dashboard/profile`, Support was deleted, and Data Import moved to
  // AdminHub. That left a top-level nav item holding a single tab, Agent
  // Settings — which is admin policy (autonomy level, agent pause/resume,
  // memory deletion, all gated to `admin` by `Agents/api_authz.py`). AdminHub
  // already owns `agents` (Agent Monitoring), so Agent Settings now lives
  // beside it as an AdminHub tab and the nav entry is gone.
  //
  // Dispatchers are not blinded by this: `AgentAutonomyBanner` on
  // `/ops/control` still shows the current autonomy level, and that is the
  // surface where they work a shift.

  // ── CommerceHub tabs ──────────────────────────────────────────────────────
  //
  // Tier 4 is the pricing/billing side, and it is `platform_admin` only: the
  // customer's ERP is the authoritative price and invoice, so a tenant admin
  // gets no second editable copy here. Invoices and Reconciliation are NOT part
  // of that set — they are capabilities 6 and 7 of the MVP pipeline and stay
  // Tier 1, visible to the operations roles.
  //
  // Note the compound reach this implies. These are tabs, and the nav item that
  // renders CommerceHub (`billing`) requires `admin` or `dispatcher`, so the
  // effective audience is someone holding `platform_admin` *alongside* an
  // operations role — the documented staff shape. `platform_admin` on its own
  // still reaches nothing, because it implies nothing.
  {
    id: "accounts",
    tier: 4,
    requiredRoles: ["platform_admin"],
    note: "Customer master lives in the ERP.",
  },
  { id: "invoices", tier: 1, note: "Capability 6." },
  {
    id: "price-books",
    tier: 4,
    requiredRoles: ["platform_admin"],
    note: "ERP prices.",
  },
  {
    id: "pricing-rules",
    tier: 4,
    requiredRoles: ["platform_admin"],
    note: "ERP prices.",
  },
  {
    id: "contracts",
    tier: 4,
    requiredRoles: ["platform_admin"],
    note: "Price-protection contracts; ERP prices.",
  },
  {
    id: "payments",
    tier: 4,
    requiredRoles: ["platform_admin"],
    note: "ERP bills and collects.",
  },
  {
    id: "ar-aging",
    tier: 4,
    requiredRoles: ["platform_admin"],
    note: "ERP receivables.",
  },
  { id: "reconciliation", tier: 1, note: "Capability 7 — gallon variance." },

  // ── ComplianceHub tabs — all legally required ─────────────────────────────
  { id: "certifications", tier: 3, note: "DOT asset certifications." },
  { id: "meters", tier: 3, note: "Meter audit trail." },
  { id: "bols", tier: 3, note: "Terminal bills of lading." },
  { id: "ifta", tier: 3, note: "IFTA quarterly filing." },

  // ── AdminHub tabs ─────────────────────────────────────────────────────────
  { id: "metrics", tier: 2, note: "Notification delivery health." },
  {
    id: "feature-flags",
    tier: 2,
    requiredRoles: ["admin"],
    note: "The feature-flag API 403s for non-admins.",
  },
  { id: "agents", tier: 2, note: "Shadow-mode agent monitoring." },
  {
    id: "stripe",
    tier: 4,
    requiredRoles: ["platform_admin"],
    note: "Payment collection; ERP bills.",
  },
  { id: "integrations", tier: 2, note: "Integration marketplace." },
  { id: "intake-channels", tier: 1, note: "Order intake — capability 1." },
  {
    id: "weather-alerts",
    tier: 2,
    note: "DeliveryPrioritizationAgent takes a storm_mode_evaluator.",
  },
  {
    id: "import",
    tier: 1,
    requiredRoles: ["admin"],
    note: "Matches import_endpoints.py::IMPORT_ADMIN_ROLES — admin only, because one CSV can overwrite the customer, asset, driver or inventory master data for the whole tenant. Lives in AdminHub so the container's admin requirement matches the endpoint's.",
  },

  // ── SetupHub tabs ─────────────────────────────────────────────────────────
  {
    id: "depots",
    tier: 1,
    note: "The route agent resolves the depot at location index 0.",
  },
  { id: "road-restrictions", tier: 2, note: "Storm-mode routing input." },
  { id: "tax", tier: 3, note: "invoice_service.validate_invoice()." },
  { id: "exemptions", tier: 3, note: "invoice_service.validate_invoice()." },

  // ── FuelOpsPage tabs ──────────────────────────────────────────────────────
  { id: "stations", tier: 1, note: "Tank levels driving the forecast." },
  { id: "efficiency", tier: 2, note: "Consumption trend." },
  { id: "kfactor", tier: 2, note: "Telemetry calibration accuracy." },
  { id: "sourcing", tier: 2, note: "Terminal supply selection." },

  // ── Notifications page tabs ───────────────────────────────────────────────
  //
  // Both sit under the `notifications` nav item, which already requires the same
  // two roles, so these carry no extra `requiredRoles` of their own. They are
  // registered rather than left off so `canSee` resolves them and the drift guard
  // covers them like every other hub tab.
  {
    id: "notification-history",
    tier: 2,
    note: "Delivery log a dispatcher chases during a shift.",
  },
  {
    id: "notification-settings",
    tier: 2,
    note: "Rules, templates and per-customer preferences. Backed by notifications/api.",
  },

  // ── SettingsPage tabs ─────────────────────────────────────────────────────
  // AdminHub tab. Admin-only, matching the backend: `PATCH /agent/config/`
  // `autonomy`, `POST /agent/{id}/pause|resume` and `DELETE /agent/memory/{id}`
  // all require `admin` via `agent_admin_dependency`. The previous note here
  // claimed "read-only for non-admins already", which overstated it — the
  // read-only treatment covered the autonomy radios only, while pause/resume
  // and memory deletion were ungated in both the UI and the API.
  {
    id: "agent-settings",
    tier: 2,
    requiredRoles: ["admin"],
    note: "Agent policy: autonomy level, pause/resume, memory. Admin-only.",
  },
  // There is deliberately no `security` entry. That tab rendered
  // `<ChangePassword />`, the same component `ProfilePage` renders, so it was a
  // second door onto one form. Password change lives on `/dashboard/profile`,
  // which has no registry entry by design — see the route-guard comment in
  // `app/dashboard/layout.tsx` — and is reached from the Sidebar avatar rather
  // than the role-filtered nav, so every role keeps access to it.
  //
  // There is deliberately no `support` entry either. That tab was a 791-line
  // ticketing UI for the legacy Nigerian last-mile CRM: its list endpoint is
  // gated behind `LEGACY_NG_DELIVERY_ENABLED` (false in development, test,
  // example and production — audit 2026-05-08 recommendation #1) and its create,
  // detail and update endpoints were never implemented, so the create-ticket
  // modal could not work in any environment. Its other two tabs were customer
  // notifications, which now live at `/dashboard/notifications` under the
  // `notification-history` and `notification-settings` ids above.
];

const MODULE_BY_ID: ReadonlyMap<string, ModuleDescriptor> = new Map(
  MODULES.map((m) => [m.id, m]),
);

/** Every registered module id. Used by the drift guard. */
export function registeredModuleIds(): readonly string[] {
  return MODULES.map((m) => m.id);
}

/** Look up a descriptor, or `undefined` when the id is not registered. */
export function moduleDescriptor(id: string): ModuleDescriptor | undefined {
  return MODULE_BY_ID.get(id);
}

// ─── The predicate ───────────────────────────────────────────────────────────

/**
 * Should this user see this module?
 *
 * Answers the MVP question and the role question together, in that order:
 *
 * 1. **Unknown id → `false`.** Fails closed. Safe only because the drift guard
 *    in `modules.test.ts` proves every real nav item and hub tab is registered;
 *    without it, a typo would silently delete navigation.
 * 2. **`mvpMode` and Tier 4 → `false`.** Tier 4 is the pricing and billing
 *    surface the ERP owns. Note that Tier 4 *also* carries
 *    `requiredRoles: ["platform_admin"]`, so it is refused at step 3 for
 *    everyone else even when `mvpMode` is off. `mvpMode` is the broader switch:
 *    it hides Tier 4 from staff too.
 * 3. **`requiredRoles` with no exact match → `false`.** Unresolved roles
 *    (`null`) count as no roles, so nothing role-gated flashes visible.
 * 4. Otherwise visible. A module with no `requiredRoles` shows to any
 *    signed-in user immediately, including before roles resolve.
 */
export function canSee(id: string, ctx: VisibilityContext): boolean {
  const descriptor = MODULE_BY_ID.get(id);
  if (!descriptor) return false;

  const mvpMode = ctx.mvpMode ?? mvpModeDefault();
  if (mvpMode && descriptor.tier === 4) return false;

  if (!descriptor.requiredRoles) return true;
  return hasAnyRole(ctx.roles, descriptor.requiredRoles);
}

/** Filter a list of `{ id }` records through {@link canSee}, preserving order. */
export function visibleByCanSee<T extends { id: string }>(
  items: readonly T[],
  ctx: VisibilityContext,
): T[] {
  return items.filter((item) => canSee(item.id, ctx));
}
