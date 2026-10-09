/**
 * URL-synced tabs (R2.4): `?tab=` restores the tab, aliases map legacy ids,
 * unknown values fall back to the first visible tab, and changes use
 * `router.replace` (no history entry) while keeping other params.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { resolveTab, TabPanel, Tabs, useUrlTab } from "./Tabs";

const replace = jest.fn();
let params = new URLSearchParams();
jest.mock("next/navigation", () => ({
  useRouter: () => ({ replace, push: jest.fn() }),
  usePathname: () => "/dashboard/dispatch",
  useSearchParams: () => params,
}));

const TABS = [
  { id: "board", label: "Board" },
  { id: "jobs", label: "Jobs" },
  { id: "plans", label: "Plans" },
];
const ALIASES = { scheduling: "jobs", distribution: "plans" };

beforeEach(() => {
  replace.mockClear();
  params = new URLSearchParams();
});

describe("resolveTab", () => {
  it("restores, aliases and falls back", () => {
    const ids = TABS.map((t) => t.id);
    expect(resolveTab("plans", ids)).toBe("plans");
    expect(resolveTab("scheduling", ids, { aliases: ALIASES })).toBe("jobs");
    expect(resolveTab("nope", ids)).toBe("board");
    expect(resolveTab(null, ids)).toBe("board");
    expect(resolveTab(null, ids, { fallback: "jobs" })).toBe("jobs");
    // A hidden (not visible) tab falls back too.
    expect(resolveTab("plans", ["board", "jobs"])).toBe("board");
  });
});

describe("<Tabs> bound to the URL", () => {
  it("selects the tab named by ?tab= and exposes tablist semantics", () => {
    params = new URLSearchParams("tab=plans");
    render(<Tabs tabs={TABS} label="Dispatch views" />);
    expect(
      screen.getByRole("tablist", { name: "Dispatch views" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Plans" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("tab", { name: "Board" })).toHaveAttribute(
      "tabindex",
      "-1",
    );
  });

  it("maps a legacy alias", () => {
    params = new URLSearchParams("tab=distribution");
    render(<Tabs tabs={TABS} aliases={ALIASES} />);
    expect(screen.getByRole("tab", { name: "Plans" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("falls back to the first visible tab for unknown values", () => {
    params = new URLSearchParams("tab=bogus");
    render(<Tabs tabs={[{ ...TABS[0], hidden: true }, TABS[1], TABS[2]]} />);
    expect(screen.queryByRole("tab", { name: "Board" })).toBeNull();
    expect(screen.getByRole("tab", { name: "Jobs" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("writes the change with router.replace and keeps other params", () => {
    params = new URLSearchParams("tab=board&date=2026-10-08");
    render(<Tabs tabs={TABS} />);
    fireEvent.click(screen.getByRole("tab", { name: "Jobs" }));
    expect(replace).toHaveBeenCalledWith(
      "/dashboard/dispatch?tab=jobs&date=2026-10-08",
      { scroll: false },
    );
  });

  it("moves with the arrow keys and Home/End (automatic activation)", () => {
    params = new URLSearchParams("tab=board");
    render(<Tabs tabs={TABS} />);
    const board = screen.getByRole("tab", { name: "Board" });
    fireEvent.keyDown(board, { key: "ArrowRight" });
    expect(replace).toHaveBeenLastCalledWith("/dashboard/dispatch?tab=jobs", {
      scroll: false,
    });
    fireEvent.keyDown(board, { key: "End" });
    expect(replace).toHaveBeenLastCalledWith("/dashboard/dispatch?tab=plans", {
      scroll: false,
    });
    fireEvent.keyDown(board, { key: "ArrowLeft" });
    expect(replace).toHaveBeenLastCalledWith("/dashboard/dispatch?tab=plans", {
      scroll: false,
    });
  });
});

describe("useUrlTab + TabPanel", () => {
  function Hub() {
    const [tab, setTab] = useUrlTab(["one", "two"], { param: "view" });
    return (
      <>
        <Tabs
          tabs={[
            { id: "one", label: "One" },
            { id: "two", label: "Two" },
          ]}
          value={tab}
          onChange={setTab}
          idBase="hub"
        />
        <TabPanel idBase="hub" value={tab}>
          panel {tab}
        </TabPanel>
      </>
    );
  }
  it("links the panel to the active tab and uses a custom param", () => {
    params = new URLSearchParams("view=two");
    render(<Hub />);
    const panel = screen.getByRole("tabpanel");
    expect(panel).toHaveTextContent("panel two");
    expect(panel).toHaveAccessibleName("Two");
    expect(screen.getByRole("tab", { name: "Two" })).toHaveAttribute(
      "aria-controls",
      "hub-panel",
    );
    fireEvent.click(screen.getByRole("tab", { name: "One" }));
    expect(replace).toHaveBeenCalledWith("/dashboard/dispatch?view=one", {
      scroll: false,
    });
  });
});
