/** The portal status map (R14.2, AC 2, design §11.4). */
import { render, screen } from "@testing-library/react";
import PortalStatus from "../PortalStatus";
import type { PortalStatusKind } from "../portalStatusMap";

// [kind, server code, display status, lucide icon class]
const TABLE: [PortalStatusKind, string, string, string][] = [
  ["order", "awaiting_confirmation", "draft", "lucide-circle-dashed"],
  ["order", "on_hold", "draft", "lucide-circle-dashed"],
  ["order", "confirmed", "planned", "lucide-calendar-clock"],
  ["order", "out_for_delivery", "in_transit", "lucide-truck"],
  ["order", "delivered", "delivered", "lucide-check"],
  ["order", "not_delivered", "exception", "lucide-triangle-alert"],
  ["order", "cancelled", "cancelled", "lucide-x"],
  ["invoice", "open", "open", "lucide-file-text"],
  ["invoice", "partial", "partial", "lucide-circle-dollar-sign"],
  ["invoice", "overdue", "overdue", "lucide-calendar-x"],
  ["invoice", "paid", "paid", "lucide-badge-check"],
  ["invoice", "void", "cancelled", "lucide-x"],
  ["payment", "creating", "open", "lucide-clock"],
  ["payment", "created", "open", "lucide-clock"],
  ["payment", "pending", "open", "lucide-clock"],
  ["payment", "succeeded", "paid", "lucide-badge-check"],
  ["payment", "failed", "exception", "lucide-triangle-alert"],
  ["payment", "canceled", "exception", "lucide-triangle-alert"],
];

describe("PortalStatus", () => {
  it.each(TABLE)(
    "%s %s → %s with its icon and the server label",
    (kind, code, status, icon) => {
      const label = `Server label for ${code}`;
      render(<PortalStatus kind={kind} code={code} label={label} />);
      const text = screen.getByText(label);
      const badge = text.closest("[data-status]") as HTMLElement;
      expect(badge).toHaveAttribute("data-status", status);
      expect(badge.querySelector("svg")?.getAttribute("class")).toContain(icon);
      // The raw code is never the text.
      expect(badge.textContent).toBe(label);
    },
  );

  it("struck-through cancelled and dashed draft", () => {
    render(
      <>
        <PortalStatus kind="order" code="cancelled" label="Cancelled" />
        <PortalStatus
          kind="order"
          code="awaiting_confirmation"
          label="Awaiting confirmation"
        />
      </>,
    );
    expect(screen.getByText("Cancelled")).toHaveClass("line-through");
    expect(
      (
        screen
          .getByText("Awaiting confirmation")
          .closest("[data-status]") as HTMLElement
      ).style.borderStyle,
    ).toBe("dashed");
  });

  it("falls back to the dashed draft style with the server label for an unknown code", () => {
    render(
      <PortalStatus kind="order" code="something_new" label="Something new" />,
    );
    expect(
      screen.getByText("Something new").closest("[data-status]"),
    ).toHaveAttribute("data-status", "draft");
  });
});
