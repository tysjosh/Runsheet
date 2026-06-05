/**
 * Tests for the shared Table primitive.
 *
 * The platform is migrating all hand-rolled <table> markup onto this
 * component, so these tests pin the behaviours pages rely on: column
 * rendering, alignment, empty/loading states, row click + selection,
 * per-row expansion, and the footer slot.
 */
import "@testing-library/jest-dom";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { type Column, Table } from "./Table";

interface Row {
  id: string;
  name: string;
  amount: number;
}

const rows: Row[] = [
  { id: "r1", name: "Alpha", amount: 10 },
  { id: "r2", name: "Beta", amount: 20 },
];

const columns: Column<Row>[] = [
  { key: "name", label: "Name" },
  {
    key: "amount",
    label: "Amount",
    align: "right",
    render: (r) => `$${r.amount}`,
  },
];

it("renders a header and a row per data item", () => {
  render(<Table columns={columns} data={rows} getRowId={(r) => r.id} />);
  expect(screen.getByText("Name")).toBeInTheDocument();
  expect(screen.getByText("Amount")).toBeInTheDocument();
  expect(screen.getByText("Alpha")).toBeInTheDocument();
  expect(screen.getByText("$20")).toBeInTheDocument();
});

it("applies right alignment to a right-aligned column", () => {
  render(<Table columns={columns} data={rows} getRowId={(r) => r.id} />);
  const amountHeader = screen.getByText("Amount");
  expect(amountHeader.className).toMatch(/text-right/);
});

it("falls back to item[key] when a column has no render fn", () => {
  render(<Table columns={columns} data={rows} getRowId={(r) => r.id} />);
  // "Name" column has no render → reads item.name directly.
  expect(screen.getByText("Beta")).toBeInTheDocument();
});

it("renders the empty state when data is empty", () => {
  render(
    <Table
      columns={columns}
      data={[]}
      emptyState={<span>Nothing here</span>}
    />,
  );
  expect(screen.getByText("Nothing here")).toBeInTheDocument();
});

it("renders the loading state instead of data when loading", () => {
  render(
    <Table
      columns={columns}
      data={rows}
      loading
      loadingState={<span>Please wait</span>}
    />,
  );
  expect(screen.getByText("Please wait")).toBeInTheDocument();
  expect(screen.queryByText("Alpha")).not.toBeInTheDocument();
});

it("invokes onRowClick with the clicked item", () => {
  const onRowClick = jest.fn();
  render(
    <Table
      columns={columns}
      data={rows}
      getRowId={(r) => r.id}
      onRowClick={onRowClick}
    />,
  );
  fireEvent.click(screen.getByText("Alpha"));
  expect(onRowClick).toHaveBeenCalledWith(rows[0]);
});

it("highlights the selected row", () => {
  render(
    <Table
      columns={columns}
      data={rows}
      getRowId={(r) => r.id}
      selectedId="r2"
    />,
  );
  const betaRow = screen.getByText("Beta").closest("tr");
  expect(betaRow?.className).toMatch(/bg-info-light/);
});

it("renders an expansion row when renderExpanded returns content", () => {
  render(
    <Table
      columns={columns}
      data={rows}
      getRowId={(r) => r.id}
      renderExpanded={(r) => (r.id === "r1" ? <div>expanded-alpha</div> : null)}
    />,
  );
  expect(screen.getByText("expanded-alpha")).toBeInTheDocument();
});

it("renders a footer when provided", () => {
  render(
    <Table
      columns={columns}
      data={rows}
      getRowId={(r) => r.id}
      footer={
        <tr>
          <td>Total</td>
          <td>$30</td>
        </tr>
      }
    />,
  );
  const footer = screen.getByText("Total").closest("tfoot");
  expect(footer).not.toBeNull();
  expect(within(footer as HTMLElement).getByText("$30")).toBeInTheDocument();
});

it("forwards ariaLabel to the table element", () => {
  render(
    <Table
      columns={columns}
      data={rows}
      getRowId={(r) => r.id}
      ariaLabel="My table"
    />,
  );
  expect(screen.getByRole("table", { name: "My table" })).toBeInTheDocument();
});

it("applies a per-row class from rowClassName", () => {
  render(
    <Table
      columns={columns}
      data={rows}
      getRowId={(r) => r.id}
      rowClassName={(r) => (r.id === "r1" ? "bg-error-light" : "")}
    />,
  );
  const alphaRow = screen.getByText("Alpha").closest("tr");
  const betaRow = screen.getByText("Beta").closest("tr");
  expect(alphaRow?.className).toMatch(/bg-error-light/);
  // The non-matching row keeps the default hover background.
  expect(betaRow?.className).toMatch(/hover:bg-gray-50/);
});

it("applies a per-row data-testid from rowTestId", () => {
  render(
    <Table
      columns={columns}
      data={rows}
      getRowId={(r) => r.id}
      rowTestId={(r) => `row-${r.id}`}
    />,
  );
  expect(screen.getByTestId("row-r1")).toBeInTheDocument();
  expect(screen.getByTestId("row-r2")).toBeInTheDocument();
});
