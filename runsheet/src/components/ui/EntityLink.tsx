"use client";

/**
 * EntityLink — the single reusable component for rendering a cross-module
 * canonical reference (customer, asset, driver, order, job, invoice, account,
 * tank, terminal, depot) as navigation to its owning module.
 *
 * This consolidates the `LinkedRefField` / `CustomerCell` "Unlinked" patterns
 * that were previously re-implemented inline in `JobDetailPage`,
 * `OrderDetailPage` (`app/orders/[orderId]/page.tsx`), and `OrdersPage`, so that
 * navigability is consistent and not re-derived per table (Req 13.1, 13.3).
 *
 * Behaviour:
 * - A **resolved** reference renders as a link to the owning module, preferring
 *   the resolved summary display name (the source of truth) over any caller
 *   supplied `label` snapshot.
 * - An **unresolved** reference (an id that did not resolve in this tenant)
 *   renders an explicit "Unlinked" affordance rather than an inert id or a dead
 *   link (Req 13.3).
 * - An empty or absent reference with a bare `id` links optimistically (used by
 *   list reads that carry ids but no expanded `links` payload); with no id it
 *   renders a neutral em dash.
 *
 * Validates: Requirements 13.1, 13.3
 */

import Link from "next/link";
import type React from "react";
import { Badge } from "./Badge";
import { useInShellNav } from "./InShellNav";

// ─── Entity types + canonical route map ──────────────────────────────────────

/**
 * The set of canonical entity types that can be linked across modules. Mirrors
 * the reference taxonomy in the cross-module-entity-linkage design.
 */
export type EntityType =
  | "customer"
  | "asset"
  | "driver"
  | "order"
  | "job"
  | "invoice"
  | "account"
  | "tank"
  | "terminal"
  | "depot";

/**
 * The single source of truth for owning-module hrefs. Every surface that links
 * a canonical reference resolves its destination through this map (via
 * {@link entityHref}) so there is exactly one place that knows where an entity
 * "lives". Later UI phases (E/F/G) adopt this component and therefore inherit
 * this map; if an owning route changes, it changes here once.
 */
const ENTITY_ROUTES: Record<EntityType, (id: string) => string> = {
  // Commerce
  customer: (id) => `/commerce/customers/${encodeURIComponent(id)}`,
  account: (id) => `/commerce/accounts/${encodeURIComponent(id)}`,
  invoice: (id) => `/commerce/invoices/${encodeURIComponent(id)}`,
  // Orders + scheduling
  order: (id) => `/orders/${encodeURIComponent(id)}`,
  job: (id) => `/ops/scheduling/${encodeURIComponent(id)}`,
  // Fleet / ops
  asset: (id) => `/ops/tracking/${encodeURIComponent(id)}`,
  driver: (id) => `/ops/drivers?driver=${encodeURIComponent(id)}`,
  // Fuel ops
  tank: (id) => `/ops/fuel/tanks/${encodeURIComponent(id)}`,
  depot: (id) => `/ops/fuel/depots/${encodeURIComponent(id)}`,
  // Sourcing / compliance
  terminal: (id) => `/compliance/terminals/${encodeURIComponent(id)}`,
};

/**
 * Resolve the canonical owning-module href for an entity reference. Exposed so
 * non-link callers (e.g. row click handlers) can reuse the same route map.
 */
export function entityHref(type: EntityType, id: string): string {
  return ENTITY_ROUTES[type](id);
}

// ─── Resolved-reference contract ──────────────────────────────────────────────

/**
 * A single resolved reference, structurally identical to the backend
 * `RefResolver`/`ResolvedRef` payload and the `ResolvedLink` type exported by
 * `schedulingApi.ts` / `ordersApi.ts`. Defined here (structurally compatible)
 * so the shared UI primitive carries no dependency on a service module while
 * still accepting links produced by those services.
 *
 * - `resolved`   — resolved to a same-tenant entity; `summary` holds a small
 *   display payload (e.g. `display_name`).
 * - `unresolved` — an id was present but did not resolve; render "Unlinked".
 * - `empty`      — no id was supplied (the reference is simply absent).
 */
export type ResolvedLink =
  | { status: "resolved"; id: string; summary: Record<string, unknown> }
  | { status: "unresolved"; id: string }
  | { status: "empty"; id?: string | null };

// ─── Display-name extraction ──────────────────────────────────────────────────

const SUMMARY_NAME_KEYS = [
  "display_name",
  "legal_name",
  "name",
  "driver_name",
  "customer_name",
] as const;

/** Pull a human display name out of a resolved reference summary. */
export function summaryLabel(
  summary: Record<string, unknown>,
): string | undefined {
  for (const key of SUMMARY_NAME_KEYS) {
    const value = summary[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return undefined;
}

// ─── Component ─────────────────────────────────────────────────────────────────

const DEFAULT_LINK_CLASS =
  "text-info hover:text-info-dark underline underline-offset-2";

export interface EntityLinkProps {
  /** The canonical entity type — selects the owning-module route. */
  type: EntityType;
  /**
   * The raw reference id from the parent document. Used for optimistic linking
   * when no expanded `link` is present, and as a display fallback.
   */
  id?: string | null;
  /**
   * Caller-supplied display label (e.g. a denormalized name snapshot). For a
   * resolved reference the resolved summary name wins (it is the source of
   * truth); `label` is used as the display for optimistic/unresolved states.
   */
  label?: React.ReactNode;
  /**
   * The resolver link for this reference (from an `expand`ed read). When
   * omitted, the component falls back to optimistic linking on `id`.
   */
  link?: ResolvedLink;
  /** Extra classes appended to the link element. */
  className?: string;
  /**
   * When true (default), a resolved/optimistic link whose display differs from
   * the id appends the id in muted parentheses for disambiguation.
   */
  showId?: boolean;
  /**
   * Stop click propagation — set when the link sits inside a clickable table
   * row so navigating the link does not also trigger the row handler.
   */
  stopPropagation?: boolean;
  /** Forwarded test hook. */
  "data-testid"?: string;
}

/** The muted "Unlinked" affordance shown for a dangling reference. */
function UnlinkedAffordance({
  display,
  testId,
}: {
  display?: React.ReactNode;
  testId?: string;
}) {
  return (
    <span className="inline-flex items-center gap-1.5" data-testid={testId}>
      {display != null && display !== "" && (
        <span className="text-gray-500">{display}</span>
      )}
      <Badge variant="warning" size="sm">
        Unlinked
      </Badge>
    </span>
  );
}

/**
 * Render a cross-module reference as navigation to its owning module, or an
 * explicit "Unlinked" affordance when the reference does not resolve.
 */
export function EntityLink({
  type,
  id,
  label,
  link,
  className = "",
  showId = true,
  stopPropagation = false,
  "data-testid": testId,
}: EntityLinkProps) {
  const nav = useInShellNav();
  const inShell = nav?.handles(type) ?? false;
  const linkClass = `${DEFAULT_LINK_CLASS} ${className}`.trim();
  const onClick = stopPropagation
    ? (e: React.MouseEvent) => e.stopPropagation()
    : undefined;

  // Render the navigable element: an in-shell button when the shell can host
  // this entity type, otherwise a canonical-route link (also the fallback on
  // standalone routes where there is no provider).
  const NavTarget = ({
    targetId,
    children,
  }: {
    targetId: string;
    children: React.ReactNode;
  }) => {
    if (inShell && nav) {
      return (
        <button
          type="button"
          className={`${linkClass} cursor-pointer border-0 bg-transparent p-0 text-left`}
          onClick={(e) => {
            if (stopPropagation) e.stopPropagation();
            nav.open(type, targetId);
          }}
          data-testid={testId}
        >
          {children}
        </button>
      );
    }
    return (
      <Link
        href={entityHref(type, targetId)}
        className={linkClass}
        onClick={onClick}
        data-testid={testId}
      >
        {children}
      </Link>
    );
  };

  // Resolved — link to the owning module, preferring the resolved name.
  if (link?.status === "resolved") {
    const resolvedName = summaryLabel(link.summary);
    // The resolved summary name is the source of truth (Req 1.4); a caller
    // supplied `label` snapshot is only a fallback when the summary has none.
    const display = resolvedName ?? label ?? link.id;
    const showSuffix =
      showId && typeof display === "string" && display !== link.id;
    return (
      <NavTarget targetId={link.id}>
        {display}
        {showSuffix && <span className="text-gray-500"> ({link.id})</span>}
      </NavTarget>
    );
  }

  // Explicitly unresolved — never present a dead link or a stale name as live.
  if (link?.status === "unresolved") {
    return <UnlinkedAffordance display={label ?? link.id} testId={testId} />;
  }

  // No expanded link (or an explicitly empty one): link optimistically when an
  // id is present, otherwise render a neutral placeholder.
  const fallbackId = id?.trim() || (link?.status === "empty" ? link.id : null);
  if (fallbackId) {
    const display = label ?? fallbackId;
    const showSuffix =
      showId && typeof display === "string" && display !== fallbackId;
    return (
      <NavTarget targetId={fallbackId}>
        {display}
        {showSuffix && <span className="text-gray-500"> ({fallbackId})</span>}
      </NavTarget>
    );
  }

  // No navigable id at all but we still hold a denormalized name snapshot — show
  // the snapshot with an explicit "Unlinked" badge rather than presenting an
  // un-navigable name as if it were a live link (Req 13.3).
  if (label != null && label !== "") {
    return <UnlinkedAffordance display={label} testId={testId} />;
  }

  return (
    <span className="text-gray-500" data-testid={testId}>
      —
    </span>
  );
}

export default EntityLink;
