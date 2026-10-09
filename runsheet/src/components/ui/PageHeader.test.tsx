/**
 * Compact PageHeader and page chrome (task 1.7, R4.1–R4.2, R2.5).
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { PageChromeProvider, PageHeader, usePageChrome } from "./PageHeader";

describe("PageHeader", () => {
  it("renders one 16 px h1 in a 44 px row; subtitle and icon are not rendered", () => {
    const { container } = render(
      <PageHeader
        title="Billing"
        subtitle="Invoices, payments and reconciliation"
        icon={<svg data-testid="hero" />}
        actions={<button type="button">New invoice</button>}
      />,
    );
    const h1s = container.querySelectorAll("h1");
    expect(h1s).toHaveLength(1);
    expect(h1s[0]).toHaveTextContent("Billing");
    expect(h1s[0]).toHaveClass("text-base");
    expect(container.firstElementChild).toHaveClass("h-11");
    expect(screen.queryByTestId("hero")).toBeNull();
    expect(
      screen.queryByText("Invoices, payments and reconciliation"),
    ).toBeNull();
    expect(
      screen.getByRole("button", { name: "New invoice" }),
    ).toBeInTheDocument();
  });

  it("moves the subtitle into the ⓘ help tooltip", () => {
    render(<PageHeader title="Billing" subtitle="Invoices and payments" />);
    const info = screen.getByRole("button", { name: "About Billing" });
    fireEvent.focus(info);
    expect(screen.getByRole("tooltip")).toHaveTextContent(
      "Invoices and payments",
    );
  });

  it("renders view tabs inline in the title row", () => {
    render(
      <PageHeader
        title="Dispatch"
        tabs={[
          { id: "board", label: "Board" },
          { id: "jobs", label: "Jobs" },
        ]}
        tab="jobs"
        onTabChange={jest.fn()}
      />,
    );
    expect(
      screen.getByRole("tablist", { name: "Dispatch views" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Jobs" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("renders a back link", () => {
    render(
      <PageHeader
        title="INV-1"
        back={{ href: "/dashboard/billing", label: "Billing" }}
      />,
    );
    expect(screen.getByRole("link", { name: "Billing" })).toHaveAttribute(
      "href",
      "/dashboard/billing",
    );
  });
});

describe("embedded mode", () => {
  function InnerPage() {
    return (
      <PageHeader
        title="Invoices"
        subtitle="All invoices"
        actions={<button type="button">New invoice</button>}
        badge={<span>12 open</span>}
      />
    );
  }
  function InnerWithHook() {
    usePageChrome({ actions: <button type="button">Export</button> });
    return <p>list</p>;
  }

  it("collapses a nested PageHeader into the host row (one h1)", () => {
    const { container } = render(
      <PageChromeProvider>
        <PageHeader host title="Billing" />
        <InnerPage />
        <InnerWithHook />
      </PageChromeProvider>,
    );
    const h1s = container.querySelectorAll("h1");
    expect(h1s).toHaveLength(1);
    expect(h1s[0]).toHaveTextContent("Billing");
    expect(screen.queryByText("Invoices")).toBeNull();
    // Contributions land in the host row.
    const row = container.querySelector(
      '[data-chrome="titlerow"]',
    ) as HTMLElement;
    expect(row).toContainElement(
      screen.getByRole("button", { name: "New invoice" }),
    );
    expect(row).toContainElement(
      screen.getByRole("button", { name: "Export" }),
    );
    expect(row).toHaveTextContent("12 open");
  });

  it("removes contributions when the inner page unmounts", () => {
    const { rerender } = render(
      <PageChromeProvider>
        <PageHeader host title="Billing" />
        <InnerPage />
      </PageChromeProvider>,
    );
    expect(
      screen.getByRole("button", { name: "New invoice" }),
    ).toBeInTheDocument();
    rerender(
      <PageChromeProvider>
        <PageHeader host title="Billing" />
      </PageChromeProvider>,
    );
    expect(screen.queryByRole("button", { name: "New invoice" })).toBeNull();
  });

  it("renders normally outside a provider", () => {
    render(<InnerPage />);
    expect(
      screen.getByRole("heading", { level: 1, name: "Invoices" }),
    ).toBeInTheDocument();
  });
});
