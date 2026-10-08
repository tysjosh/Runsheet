/**
 * Core component set (task 1.5, design.md §10 "Components").
 */
import { fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import { configureFormat } from "../../lib/format";
import {
  assignLaneIdentities,
  fnv1a,
  identityFor,
  initials,
} from "../../lib/identity";
import {
  IDENTITY,
  PRODUCT_CODES,
  STATUS,
  STATUS_KEYS,
} from "../../styles/tokens";
import { Button } from "./Button";
import { Field } from "./Field";
import { FilterChips } from "./FilterChips";
import { IconButton } from "./IconButton";
import { IdentityAvatar } from "./IdentityAvatar";
import { InlineBanner } from "./InlineBanner";
import { Menu } from "./Menu";
import { NumberField, validateNumber } from "./NumberField";
import { ProductChip } from "./ProductChip";
import { ProductSelect } from "./ProductSelect";
import { Skeleton } from "./Skeleton";
import { StatusBadge, statusKeyFor } from "./StatusBadge";
import { DataTable } from "./Table";
import { Toolbar } from "./Toolbar";
import { Tooltip } from "./Tooltip";

afterEach(() => configureFormat({ locale: undefined, timeZone: undefined }));

// ── Button ───────────────────────────────────────────────────────────────
describe("Button", () => {
  it("has no success or warning variant and shows a spinner while loading", () => {
    render(
      <Button loading variant="primary">
        Save
      </Button>,
    );
    const b = screen.getByRole("button", { name: "Save" });
    expect(b).toBeDisabled();
    expect(b.querySelector("svg.animate-spin")).not.toBeNull();
    expect(b.className).toMatch(/h-8/);
  });
});

// ── StatusBadge ──────────────────────────────────────────────────────────
describe("StatusBadge", () => {
  it.each(STATUS_KEYS)("%s renders an icon and its label", (key) => {
    const { container } = render(<StatusBadge status={key} />);
    expect(screen.getByText(STATUS[key].label)).toBeInTheDocument();
    expect(container.querySelector("svg")).not.toBeNull();
    expect(container.querySelector("svg")).toHaveAttribute(
      "aria-hidden",
      "true",
    );
  });
  it("covers all 13 statuses", () => {
    expect(STATUS_KEYS).toHaveLength(13);
  });
  it("strikes Cancelled and dashes Draft", () => {
    render(
      <>
        <StatusBadge status="cancelled" />
        <StatusBadge status="draft" />
      </>,
    );
    expect(screen.getByText("Cancelled")).toHaveClass("line-through");
    expect(screen.getByText("Draft").parentElement).toHaveStyle({
      borderStyle: "dashed",
    });
  });
  it("supports a custom label and a count", () => {
    render(<StatusBadge status="delayed" label="On hold" count={4} />);
    expect(screen.getByText("On hold")).toBeInTheDocument();
    expect(screen.getByText("4")).toBeInTheDocument();
  });
  it("maps backend vocabularies onto display statuses", () => {
    expect(statusKeyFor("in_progress")).toBe("in_transit");
    expect(statusKeyFor("COMPLETED")).toBe("delivered");
    expect(statusKeyFor("on-hold")).toBe("delayed");
    expect(statusKeyFor("failed")).toBe("exception");
    expect(statusKeyFor("mystery")).toBeNull();
    expect(statusKeyFor(undefined)).toBeNull();
  });
});

// ── ProductChip / ProductSelect ──────────────────────────────────────────
describe("ProductChip", () => {
  it("cap is a named image with the RP 1637 symbol", () => {
    render(<ProductChip code="OFF_ROAD_DIESEL" variant="cap" />);
    const cap = screen.getByRole("img", { name: "Off-road dyed diesel" });
    expect(cap).toHaveTextContent("OFF");
  });
  it("chip shows the readable name, never the raw code as primary text", () => {
    render(<ProductChip code="GASOLINE_REG" />);
    expect(screen.getByText("Regular unleaded")).toBeInTheDocument();
    expect(screen.queryByText("GASOLINE_REG")).toBeNull();
  });
  it("full adds the code as secondary text", () => {
    render(<ProductChip code="DEF" variant="full" />);
    expect(screen.getByText("Diesel exhaust fluid")).toBeInTheDocument();
    expect(
      screen.getByText("DEF", { selector: ".font-mono" }),
    ).toBeInTheDocument();
  });
  it("unknown codes get the neutral cap and a humanised name", () => {
    render(<ProductChip code="JET_A" />);
    expect(screen.getByText("Jet a")).toBeInTheDocument();
    expect(screen.getByText("?")).toBeInTheDocument();
  });
});

describe("ProductSelect", () => {
  function Harness() {
    const [v, setV] = useState<string | null>("GASOLINE_REG");
    return (
      <Field label="Fuel type">
        <ProductSelect value={v} onChange={setV} />
      </Field>
    );
  }
  it("labels the trigger, shows chip + name + code, never 'CODE (Name)'", () => {
    render(<Harness />);
    const trigger = screen.getByRole("combobox", { name: "Fuel type" });
    expect(trigger).toHaveTextContent("Regular unleaded");
    expect(trigger).toHaveTextContent("GASOLINE_REG");
    expect(trigger.textContent).not.toMatch(/GASOLINE_REG \(/);
    expect(trigger).toHaveAttribute("title", "Regular unleaded (GASOLINE_REG)");
  });
  it("opens a listbox of every catalog product and selects with the keyboard", () => {
    render(<Harness />);
    const trigger = screen.getByRole("combobox", { name: "Fuel type" });
    fireEvent.keyDown(trigger, { key: "ArrowDown" });
    const list = screen.getByRole("listbox");
    expect(within(list).getAllByRole("option")).toHaveLength(
      PRODUCT_CODES.length,
    );
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    // Selected option is active first; move down and choose.
    fireEvent.keyDown(trigger, { key: "ArrowDown" });
    fireEvent.keyDown(trigger, { key: "Enter" });
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(trigger).toHaveTextContent("Premium unleaded");
  });
  it("jumps by typed letter and closes on Escape", () => {
    render(<Harness />);
    const trigger = screen.getByRole("combobox", { name: "Fuel type" });
    fireEvent.click(trigger);
    fireEvent.keyDown(trigger, { key: "k" });
    expect(trigger.getAttribute("aria-activedescendant")).toMatch(/-\d+$/);
    fireEvent.keyDown(trigger, { key: "Enter" });
    expect(trigger).toHaveTextContent("Kerosene (K-1)");
    fireEvent.click(trigger);
    fireEvent.keyDown(trigger, { key: "Escape" });
    expect(screen.queryByRole("listbox")).toBeNull();
  });
});

// ── Identity ─────────────────────────────────────────────────────────────
describe("identity", () => {
  it("is stable per id", () => {
    expect(identityFor("TRK-104")).toEqual(identityFor("TRK-104"));
    expect(fnv1a("a")).toBe(0xe40c292c);
  });
  it("never gives adjacent lanes the same or a deuteranopia-merging colour", () => {
    const lanes = Array.from({ length: 40 }, (_, i) => `truck-${i}`);
    const map = assignLaneIdentities(lanes);
    for (let i = 1; i < lanes.length; i++) {
      const a = map.get(lanes[i - 1])?.token;
      const b = map.get(lanes[i])?.token;
      expect(a).not.toBe(b);
      const pair = new Set([a, b]);
      expect(pair.has("blue.600") && pair.has("violet.600")).toBe(false);
    }
    // Deterministic for a given order.
    expect(assignLaneIdentities(lanes)).toEqual(map);
  });
  it("makes initials", () => {
    expect(initials("Darnell Price")).toBe("DP");
    expect(initials("QA-UAT Darnell Price")).toBe("QP");
    expect(initials("Kim")).toBe("KI");
    expect(initials("")).toBe("?");
  });
  it("avatar exposes the label and uses an identity colour", () => {
    render(<IdentityAvatar id="d1" label="Darnell Price" />);
    const av = screen.getByRole("img", { name: "Darnell Price" });
    expect(av).toHaveTextContent("DP");
    const hexes = IDENTITY.map((c) => c.hex);
    const bg = av.style.backgroundColor;
    const toHex = (rgb: string) =>
      `#${(rgb.match(/\d+/g) ?? []).map((n) => Number(n).toString(16).padStart(2, "0")).join("")}`;
    expect(hexes).toContain(toHex(bg));
  });
});

// ── NumberField ──────────────────────────────────────────────────────────
describe("NumberField", () => {
  function Harness({
    initial,
    locale,
  }: {
    initial: number | null;
    locale?: string;
  }) {
    const [v, setV] = useState<number | null>(initial);
    return (
      <>
        <Field label="Capacity">
          <NumberField
            value={v}
            onChange={setV}
            unit="gal"
            locale={locale}
            min={0}
            max={100000}
          />
        </Field>
        <output data-testid="value">{String(v)}</output>
      </>
    );
  }
  it("shows whole gallons with grouping instead of a raw float", () => {
    render(<Harness initial={5283.441047162968} locale="en-US" />);
    const input = screen.getByRole("textbox", { name: "Capacity" });
    expect(input).toHaveValue("5,283");
    expect(input).toHaveAttribute("inputmode", "numeric");
    expect(input).toHaveAccessibleDescription("gal");
  });
  it("parses en-US grouping and rounds to the field's decimals", () => {
    render(<Harness initial={null} locale="en-US" />);
    const input = screen.getByRole("textbox", { name: "Capacity" });
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: "5,283.6" } });
    expect(screen.getByTestId("value")).toHaveTextContent("5284");
    fireEvent.blur(input);
    expect(input).toHaveValue("5,284");
  });
  it("parses de-DE separators", () => {
    render(<Harness initial={null} locale="de-DE" />);
    const input = screen.getByRole("textbox", { name: "Capacity" });
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: "5.283,4" } });
    expect(screen.getByTestId("value")).toHaveTextContent("5283");
    fireEvent.blur(input);
    expect(input).toHaveValue("5.283");
  });
  it("reports NaN for non-numbers and flags out-of-range values", () => {
    render(<Harness initial={null} locale="en-US" />);
    const input = screen.getByRole("textbox", { name: "Capacity" });
    fireEvent.change(input, { target: { value: "abc" } });
    expect(screen.getByTestId("value")).toHaveTextContent("NaN");
    expect(input).toHaveAttribute("aria-invalid", "true");
    fireEvent.change(input, { target: { value: "200000" } });
    expect(input).toHaveAttribute("aria-invalid", "true");
    fireEvent.change(input, { target: { value: "" } });
    expect(screen.getByTestId("value")).toHaveTextContent("null");
  });
  it("validateNumber covers required, NaN, min, max and step", () => {
    configureFormat({ locale: "en-US" });
    expect(validateNumber(null, { required: true })).toBe("Required");
    expect(validateNumber(Number.NaN)).toBe("Enter a number");
    expect(validateNumber(-1, { min: 0 })).toBe("Must be at least 0");
    expect(validateNumber(20001, { max: 20000 })).toBe(
      "Must be at most 20,000",
    );
    expect(validateNumber(7, { step: 5 })).toBe("Use steps of 5");
    expect(validateNumber(10, { min: 0, max: 20, step: 5 })).toBeNull();
  });
});

// ── DataTable ────────────────────────────────────────────────────────────
describe("DataTable", () => {
  type Row = { id: string; name: string; note: string };
  const rows: Row[] = [
    { id: "a", name: "Alpha", note: "A very long note that would wrap" },
    { id: "b", name: "Beta", note: "short" },
  ];
  it("puts aria-sort on th only, sticky 36 px header, truncation titles", () => {
    const onSort = jest.fn();
    const { container } = render(
      <DataTable<Row>
        ariaLabel="Things"
        data={rows}
        getRowId={(r) => r.id}
        sort={{ key: "name", direction: "asc" }}
        onSortChange={onSort}
        columns={[
          { key: "name", header: "Name", sortable: true },
          { key: "note", header: "Note", truncate: true },
        ]}
      />,
    );
    const withSort = container.querySelectorAll("[aria-sort]");
    expect(withSort).toHaveLength(1);
    expect(withSort[0].tagName).toBe("TH");
    expect(withSort[0]).toHaveAttribute("aria-sort", "ascending");
    expect(container.querySelector("thead")).toHaveClass("sticky");
    expect(container.querySelector("thead tr")).toHaveClass("h-9");
    expect(
      screen.getByText("A very long note that would wrap"),
    ).toHaveAttribute("title", "A very long note that would wrap");
    fireEvent.click(screen.getByRole("button", { name: /Name/ }));
    expect(onSort).toHaveBeenCalledWith({ key: "name", direction: "desc" });
  });
  it("selects rows (and all) and exposes a per-row menu", () => {
    const onSel = jest.fn();
    const onOpen = jest.fn();
    render(
      <DataTable<Row>
        data={rows}
        getRowId={(r) => r.id}
        rowLabel={(r) => r.name}
        selectable
        selectedIds={["a"]}
        onSelectionChange={onSel}
        rowMenu={(r) => [
          { id: "open", label: "Open", onSelect: () => onOpen(r.id) },
        ]}
        columns={[{ key: "name", header: "Name" }]}
      />,
    );
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Beta" }));
    expect(onSel).toHaveBeenCalledWith(["a", "b"]);
    fireEvent.click(screen.getByRole("checkbox", { name: "Select all rows" }));
    expect(onSel).toHaveBeenLastCalledWith(["a", "b"]);
    fireEvent.click(screen.getByRole("button", { name: "Actions for Beta" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Open" }));
    expect(onOpen).toHaveBeenCalledWith("b");
  });
  it("renders skeleton rows while loading and an error with retry", () => {
    const retry = jest.fn();
    const { container, rerender } = render(
      <DataTable<Row>
        data={[]}
        loading
        columns={[{ key: "name", header: "Name" }]}
      />,
    );
    expect(container.querySelectorAll("tbody tr")).toHaveLength(5);
    rerender(
      <DataTable<Row>
        data={[]}
        error={{ message: "Can't load", onRetry: retry }}
        columns={[{ key: "name", header: "Name" }]}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Can't load");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(retry).toHaveBeenCalled();
  });
  it("uses 40 px rows by default and 32 px when compact", () => {
    const { container, rerender } = render(
      <DataTable<Row>
        data={rows}
        getRowId={(r) => r.id}
        columns={[{ key: "name", header: "Name" }]}
      />,
    );
    expect(container.querySelector("tbody tr")).toHaveClass("h-10");
    rerender(
      <DataTable<Row>
        data={rows}
        rowHeight="compact"
        getRowId={(r) => r.id}
        columns={[{ key: "name", header: "Name" }]}
      />,
    );
    expect(container.querySelector("tbody tr")).toHaveClass("h-8");
  });
});

// ── Menu / Toolbar ───────────────────────────────────────────────────────
describe("Menu", () => {
  it("opens with ArrowDown, moves, selects with Enter, closes on Escape", () => {
    const a = jest.fn();
    const b = jest.fn();
    render(
      <Menu
        label="View"
        items={[
          {
            id: "t",
            label: "Timeline",
            checked: true,
            group: "Layout",
            onSelect: a,
          },
          { id: "s", label: "Sequence", checked: false, onSelect: b },
        ]}
        trigger={(p) => (
          <button type="button" {...p}>
            View
          </button>
        )}
      />,
    );
    const trigger = screen.getByRole("button", { name: "View" });
    fireEvent.keyDown(trigger, { key: "ArrowDown" });
    const menu = screen.getByRole("menu", { name: "View" });
    expect(
      within(menu).getByRole("group", { name: "Layout" }),
    ).toBeInTheDocument();
    const items = within(menu).getAllByRole("menuitemradio");
    expect(items[0]).toHaveAttribute("aria-checked", "true");
    expect(items[0]).toHaveFocus();
    fireEvent.keyDown(menu, { key: "ArrowDown" });
    expect(items[1]).toHaveFocus();
    fireEvent.click(items[1]);
    expect(b).toHaveBeenCalled();
    expect(screen.queryByRole("menu")).toBeNull();
    expect(trigger).toHaveFocus();
    fireEvent.click(trigger);
    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
    expect(screen.queryByRole("menu")).toBeNull();
  });
});

describe("Toolbar", () => {
  it("collapses inline actions into ⋯ when the row overflows", () => {
    const sw = Object.getOwnPropertyDescriptor(
      HTMLElement.prototype,
      "scrollWidth",
    );
    const cw = Object.getOwnPropertyDescriptor(
      HTMLElement.prototype,
      "clientWidth",
    );
    Object.defineProperty(HTMLElement.prototype, "scrollWidth", {
      configurable: true,
      get: () => 900,
    });
    Object.defineProperty(HTMLElement.prototype, "clientWidth", {
      configurable: true,
      get: () => 300,
    });
    try {
      const onHistory = jest.fn();
      render(
        <Toolbar
          label="Board tools"
          search={<input aria-label="Search" />}
          overflow={[
            { id: "undo", label: "Undo", onSelect: jest.fn(), priority: 1 },
            {
              id: "history",
              label: "History",
              onSelect: onHistory,
              priority: 9,
            },
            {
              id: "keys",
              label: "Shortcuts",
              onSelect: jest.fn(),
              inline: false,
            },
          ]}
        />,
      );
      expect(screen.getByRole("toolbar", { name: "Board tools" })).toHaveClass(
        "h-11",
      );
      // Everything collapsed: no inline buttons, all in the menu.
      expect(screen.queryByRole("button", { name: "History" })).toBeNull();
      fireEvent.click(screen.getByRole("button", { name: "More actions" }));
      const menu = screen.getByRole("menu");
      expect(
        within(menu)
          .getAllByRole("menuitem")
          .map((m) => m.textContent),
      ).toEqual(["Undo", "History", "Shortcuts"]);
      fireEvent.click(within(menu).getByRole("menuitem", { name: "History" }));
      expect(onHistory).toHaveBeenCalled();
    } finally {
      // jsdom defines these on Element.prototype; drop the HTMLElement overrides.
      if (sw) Object.defineProperty(HTMLElement.prototype, "scrollWidth", sw);
      else
        delete (HTMLElement.prototype as { scrollWidth?: number }).scrollWidth;
      if (cw) Object.defineProperty(HTMLElement.prototype, "clientWidth", cw);
      else
        delete (HTMLElement.prototype as { clientWidth?: number }).clientWidth;
    }
  });
  it("keeps inline actions visible when they fit, and supports a takeover", () => {
    const { rerender } = render(
      <Toolbar
        overflow={[{ id: "undo", label: "Undo", onSelect: jest.fn() }]}
      />,
    );
    expect(screen.getByRole("button", { name: "Undo" })).toBeInTheDocument();
    rerender(<Toolbar takeover={<span>Place mode</span>} overflow={[]} />);
    expect(screen.getByText("Place mode")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Undo" })).toBeNull();
  });
});

// ── Small pieces ─────────────────────────────────────────────────────────
describe("FilterChips", () => {
  it("is a group of pressed toggles with counts", () => {
    const onChange = jest.fn();
    render(
      <FilterChips
        label="Status"
        value="placed"
        onChange={onChange}
        options={[
          { id: "placed", label: "Placed", count: 4, status: "draft" },
          { id: "hold", label: "On hold", count: 1, status: "delayed" },
        ]}
      />,
    );
    expect(screen.getByRole("group", { name: "Status" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Placed\s*4/ })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    fireEvent.click(screen.getByRole("button", { name: /On hold/ }));
    expect(onChange).toHaveBeenCalledWith("hold");
  });
  it("toggles in multi mode", () => {
    const onChange = jest.fn();
    render(
      <FilterChips
        label="Status"
        multi
        value={["a"]}
        onChange={onChange}
        options={[
          { id: "a", label: "A" },
          { id: "b", label: "B" },
        ]}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "B" }));
    expect(onChange).toHaveBeenCalledWith(["a", "b"]);
  });
});

describe("Field, IconButton, Tooltip, InlineBanner, Skeleton", () => {
  it("Field wires label, help and error", () => {
    render(
      <Field label="Name" help="As on the sign" error="Too short" required>
        <input />
      </Field>,
    );
    const input = screen.getByRole("textbox", { name: /Name/ });
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(input).toHaveAttribute("aria-required", "true");
    expect(input).toHaveAccessibleDescription("As on the sign Too short");
  });
  it("IconButton requires a label and shows it as a tooltip on focus", () => {
    render(<IconButton label="Refresh" icon={<svg />} />);
    const b = screen.getByRole("button", { name: "Refresh" });
    fireEvent.focus(b);
    expect(screen.getByRole("tooltip", { hidden: true })).toHaveTextContent(
      "Refresh",
    );
  });
  it("Tooltip describes its trigger and closes on Escape", () => {
    render(
      <Tooltip content="More about this">
        <button type="button">Info</button>
      </Tooltip>,
    );
    const b = screen.getByRole("button", { name: "Info" });
    fireEvent.mouseEnter(b.parentElement as HTMLElement);
    expect(b).toHaveAccessibleDescription("More about this");
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("tooltip")).toBeNull();
  });
  it("InlineBanner uses alert for critical, status otherwise", () => {
    render(
      <>
        <InlineBanner tone="critical">Bad</InlineBanner>
        <InlineBanner tone="info">Note</InlineBanner>
      </>,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Bad");
    expect(screen.getByRole("status")).toHaveTextContent("Note");
  });
  it("Skeleton announces loading once", () => {
    render(<Skeleton rows={4} />);
    expect(screen.getByRole("status", { name: "Loading" })).toBeInTheDocument();
  });
});
