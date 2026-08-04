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
 * - **4** — deferrable *because the customer's ERP already prices and bills*.
 *   This is the only tier `mvpMode` hides.
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
  {
    id: "settings",
    tier: 1,
    // Deliberately NOT role-gated. Settings holds password change, which every
    // signed-in user needs regardless of role. Data Import inside it is
    // admin/dispatcher per `import_endpoints.py::_require_import_role`, so the
    // gate belongs on that TAB. Role-gating this nav item would lock a user out
    // of changing their own password.
    note: "Holds password change — reachable by every role, by design.",
  },

  // ── CommerceHub tabs ──────────────────────────────────────────────────────
  //
  // Tier 4 is the pricing/billing side: the deferral argument is that the
  // customer's ERP already prices and bills. Invoices and Reconciliation are
  // NOT part of that set — they are capabilities 6 and 7 of the MVP pipeline.
  { id: "accounts", tier: 4, note: "Customer master lives in the ERP." },
  { id: "invoices", tier: 1, note: "Capability 6." },
  { id: "price-books", tier: 4, note: "ERP prices." },
  { id: "pricing-rules", tier: 4, note: "ERP prices." },
  { id: "contracts", tier: 4, note: "Price-protection contracts; ERP prices." },
  { id: "payments", tier: 4, note: "ERP bills and collects." },
  { id: "ar-aging", tier: 4, note: "ERP receivables." },
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
  { id: "stripe", tier: 4, note: "Payment collection; ERP bills." },
  { id: "integrations", tier: 2, note: "Integration marketplace." },
  { id: "intake-channels", tier: 1, note: "Order intake — capability 1." },
  {
    id: "weather-alerts",
    tier: 2,
    note: "DeliveryPrioritizationAgent takes a storm_mode_evaluator.",
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

  // ── SettingsPage tabs ─────────────────────────────────────────────────────
  { id: "agent-settings", tier: 2, note: "Read-only for non-admins already." },
  {
    id: "import",
    tier: 1,
    requiredRoles: ["admin", "dispatcher"],
    note: "Matches import_endpoints.py::_require_import_role.",
  },
  { id: "security", tier: 1, note: "Password change. Never gated." },
  { id: "support", tier: 1, note: "Contact form." },
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
 * 2. **`mvpMode` and Tier 4 → `false`.** Tier 4 is the deferrable pricing and
 *    billing surface.
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
