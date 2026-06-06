/**
 * Tests for the shared <EntityLink> primitive.
 *
 * EntityLink is the single source of truth for rendering a cross-module
 * canonical reference as navigation to its owning module (or an explicit
 * "Unlinked" affordance when the reference does not resolve). These tests pin:
 * - the canonical per-entity-type route map (one href per entity type)
 * - resolved references link and prefer the resolved summary name
 * - unresolved references render the "Unlinked" affordance, never a dead link
 * - empty/absent references link optimistically on a bare id, else render —
 *
 * Validates: Requirements 13.1, 13.3
 */

import "@testing-library/jest-dom";
import { render, screen } from "@testing-library/react";
import {
  EntityLink,
  type EntityType,
  entityHref,
  type ResolvedLink,
  summaryLabel,
} from "./EntityLink";

// ─── Canonical route map ───────────────────────────────────────────────────

describe("entityHref — canonical owning-module route map", () => {
  const cases: Array<[EntityType, string, string]> = [
    ["customer", "CUST-1", "/commerce/customers/CUST-1"],
    ["account", "ACC-1", "/commerce/accounts/ACC-1"],
    ["invoice", "INV-1", "/commerce/invoices/INV-1"],
    ["order", "ORD-1", "/orders/ORD-1"],
    ["job", "JOB-1", "/ops/scheduling/JOB-1"],
    ["asset", "ASSET-1", "/ops/tracking/ASSET-1"],
    ["driver", "DRV-1", "/ops/drivers?driver=DRV-1"],
    ["tank", "TANK-1", "/ops/fuel/tanks/TANK-1"],
    ["depot", "DEP-1", "/ops/fuel/depots/DEP-1"],
    ["terminal", "TERM-1", "/compliance/terminals/TERM-1"],
  ];

  it.each(cases)("routes %s ids to the owning module", (type, id, expected) => {
    expect(entityHref(type, id)).toBe(expected);
  });

  it("URL-encodes ids containing special characters", () => {
    expect(entityHref("customer", "a b/c")).toBe(
      "/commerce/customers/a%20b%2Fc",
    );
  });
});

// ─── summaryLabel ──────────────────────────────────────────────────────────

describe("summaryLabel", () => {
  it("prefers display_name and falls back through known name keys", () => {
    expect(summaryLabel({ display_name: "Acme" })).toBe("Acme");
    expect(summaryLabel({ driver_name: "Jane" })).toBe("Jane");
    expect(summaryLabel({ customer_name: "Co" })).toBe("Co");
  });

  it("returns undefined when no usable name is present", () => {
    expect(summaryLabel({})).toBeUndefined();
    expect(summaryLabel({ display_name: "   " })).toBeUndefined();
  });
});

// ─── Resolved references ───────────────────────────────────────────────────

describe("EntityLink — resolved reference", () => {
  it("links to the owning module and prefers the resolved summary name", () => {
    const link: ResolvedLink = {
      status: "resolved",
      id: "CUST-7",
      summary: { display_name: "Acme Fuels" },
    };
    render(<EntityLink type="customer" id="CUST-7" link={link} />);

    const anchor = screen.getByRole("link", { name: /Acme Fuels/ });
    expect(anchor).toHaveAttribute("href", "/commerce/customers/CUST-7");
    // The id is appended in parentheses when the display differs from the id.
    expect(anchor).toHaveTextContent("Acme Fuels (CUST-7)");
  });

  it("prefers the resolved name over a caller-supplied snapshot label", () => {
    const link: ResolvedLink = {
      status: "resolved",
      id: "CUST-7",
      summary: { display_name: "Current Name" },
    };
    render(<EntityLink type="customer" label="Stale Snapshot" link={link} />);
    expect(
      screen.getByRole("link", { name: /Current Name/ }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Stale Snapshot/)).not.toBeInTheDocument();
  });

  it("omits the id suffix when showId is false", () => {
    const link: ResolvedLink = {
      status: "resolved",
      id: "ORD-1",
      summary: { display_name: "Order One" },
    };
    render(<EntityLink type="order" link={link} showId={false} />);
    const anchor = screen.getByRole("link");
    expect(anchor).toHaveTextContent("Order One");
    expect(anchor).not.toHaveTextContent("(ORD-1)");
  });
});

// ─── Unresolved references ─────────────────────────────────────────────────

describe("EntityLink — unresolved reference", () => {
  it("renders an Unlinked affordance and offers no navigation", () => {
    const link: ResolvedLink = { status: "unresolved", id: "CUST-MISSING" };
    render(<EntityLink type="customer" label="Ghost Co" link={link} />);

    expect(screen.getByText(/Unlinked/i)).toBeInTheDocument();
    // The snapshot label is shown but it is NOT a link (dead reference).
    expect(screen.getByText("Ghost Co")).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });
});

// ─── Empty / optimistic references ─────────────────────────────────────────

describe("EntityLink — empty/absent reference", () => {
  it("links optimistically on a bare id when no expanded link is present", () => {
    render(<EntityLink type="asset" id="ASSET-9" />);
    const anchor = screen.getByRole("link", { name: /ASSET-9/ });
    expect(anchor).toHaveAttribute("href", "/ops/tracking/ASSET-9");
  });

  it("uses the label as display text for an optimistic link", () => {
    render(<EntityLink type="driver" id="DRV-3" label="Jane Driver" />);
    const anchor = screen.getByRole("link", { name: /Jane Driver/ });
    expect(anchor).toHaveAttribute("href", "/ops/drivers?driver=DRV-3");
    expect(anchor).toHaveTextContent("Jane Driver (DRV-3)");
  });

  it("renders a neutral placeholder when there is no id and no link", () => {
    render(<EntityLink type="order" />);
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(screen.getByText("—")).toBeInTheDocument();
  });

  it("renders a placeholder for an explicitly empty link with no id", () => {
    const link: ResolvedLink = { status: "empty" };
    render(<EntityLink type="order" link={link} />);
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(screen.getByText("—")).toBeInTheDocument();
  });
});
