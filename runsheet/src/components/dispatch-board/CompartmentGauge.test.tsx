/**
 * CompartmentGauge and CheckChip (plan task 28; R5.3, R5.4, R9.1, R9.2,
 * R19.4): never colour only, gauge text alternatives, hover preview from
 * the server's candidate results.
 */
import { act, render, screen, waitFor, within } from "@testing-library/react";

jest.mock("next/navigation", () => require("./testMocks").navigationMock);
jest.mock(
  "../../hooks/useDispatchBoardSocket",
  () => require("./testMocks").socketMock,
);
jest.mock(
  "@atlaskit/pragmatic-drag-and-drop/adapter/element-adapter",
  () => require("./testMocks").pragmaticMock,
);
jest.mock(
  "@atlaskit/pragmatic-drag-and-drop-auto-scroll/element",
  () => require("./testMocks").autoScrollMock,
);
jest.mock("../../services/dispatchBoardApi", () => ({
  ...jest.requireActual("../../services/dispatchBoardApi"),
  getBoard: jest.fn(),
  sendBoardCommand: jest.fn(),
  validateBoard: jest.fn(),
}));
jest.mock("../../utils/auth", () => ({
  ...jest.requireActual("../../utils/auth"),
  getCurrentUserId: jest.fn().mockResolvedValue("u1"),
}));

import { CheckChip, chipText } from "./CheckChip";
import {
  CompartmentGauge,
  gaugeSegments,
  previewFromChecks,
  segmentText,
} from "./CompartmentGauge";
import {
  makeCheck,
  makeCompartment,
  makeLane,
  makeTrayOrder,
} from "./state/testFixtures";
import {
  boardSnap,
  mockValidate,
  renderBoard,
  resetBoardMocks,
} from "./testBoard";
import { resetMocks, startDrag } from "./testMocks";

beforeEach(() => {
  resetMocks();
  resetBoardMocks();
});

describe("CheckChip", () => {
  it.each([
    ["pass", null, "OK"],
    ["warn", "Window at risk", "Warning: Window at risk"],
    ["block", "Product not allowed", "Blocked: Product not allowed"],
    ["info", "HOS checked on the day", "Info: HOS checked on the day"],
    ["checking", null, "Checking…"],
  ] as const)(
    "%s carries an icon and text, never colour only",
    (outcome, label, text) => {
      const { container } = render(
        <CheckChip outcome={outcome} label={label} />,
      );
      const chip = container.firstElementChild as HTMLElement;
      expect(chip).toHaveAttribute("data-outcome", outcome);
      expect(chip.querySelector("svg")).not.toBeNull();
      // The full outcome is in the text (read by screen readers), not only in the colour.
      expect(chip).toHaveTextContent(text);
    },
  );

  it("counts other reasons (R9.1)", () => {
    expect(chipText("block", "Product not allowed", 2)).toBe(
      "Blocked: Product not allowed (+2 more)",
    );
    render(<CheckChip outcome="block" label="Product not allowed" more={2} />);
    expect(screen.getByText("+2")).toBeInTheDocument();
  });
});

describe("CompartmentGauge", () => {
  const comps = [
    makeCompartment("c2", { position_index: 1, capacity_l: 5000 }),
    makeCompartment("c1", {
      position_index: 0,
      capacity_l: 10000,
      state: "needs_cleaning",
    }),
  ];
  const allocations = [
    {
      order_id: "O1",
      compartment_id: "c2",
      product_code: "ULSD",
      liters: 4600,
      capacity_liters: 5000,
    },
  ];

  it("gives each compartment a text alternative in position order", () => {
    render(<CompartmentGauge compartments={comps} allocations={allocations} />);
    const items = within(
      screen.getByRole("list", { name: "compartments" }),
    ).getAllByRole("listitem");
    expect(items.map((i) => i.querySelector(".sr-only")?.textContent)).toEqual([
      "Compartment 1, empty, 0% full, needs cleaning",
      "Compartment 2, ULSD, 92% full",
    ]);
    // Visible text, not colour, says which needs cleaning.
    expect(items[0]).toHaveTextContent("Clean");
    expect(items[1]).toHaveTextContent("ULSD 92%");
  });

  it("previews the post-drop fill and names blocked compartments (R5.4)", () => {
    const preview = previewFromChecks({ c1: 40, c2: 92 }, [
      makeCheck({
        outcome: "block",
        reason_code: "compatibility_blocked",
        fix_link: { kind: "compartment", id: "c1" },
      }),
    ]);
    const seg = gaugeSegments(comps, allocations, preview);
    expect(seg.map(segmentText)).toEqual([
      "Compartment 1, empty, 40% full, needs cleaning, blocked by this change",
      "Compartment 2, ULSD, 92% full",
    ]);
    render(
      <CompartmentGauge
        compartments={comps}
        allocations={allocations}
        preview={preview}
      />,
    );
    expect(
      screen.getByRole("list", { name: "compartments after this change" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Blocked/)).toBeInTheDocument();
  });
});

describe("hover preview on the board", () => {
  it("drag start shows each lane's outcome chip and post-drop gauge from validate (R8.3, R5.4)", async () => {
    mockValidate.mockResolvedValue({
      degraded_sources: [],
      results: {
        T1: {
          outcome: "block",
          reason: null,
          worst_checks: [
            makeCheck({
              outcome: "block",
              reason_code: "compatibility_blocked",
              message: "Compartment T1-c1 can't carry ULSD after GAS.",
              fix_link: { kind: "compartment", id: "T1-c1" },
            }),
          ],
          preview: {
            fill_by_compartment: { "T1-c1": 30 },
            insertion_index: 0,
            load_id: "new",
            eta_delta_minutes: null,
          },
        },
      },
    });
    await renderBoard(
      boardSnap({
        lanes: [makeLane("T1", 1, { loads: [] })],
        trays: {
          orders: [makeTrayOrder("O1")],
          orders_truncated: false,
          drivers: [],
          trucks: [],
        },
      }),
    );
    const card = within(
      screen.getByRole("listbox", { name: "Orders to plan" }),
    ).getByRole("option");
    await act(async () => {
      startDrag(card);
    });
    await waitFor(() => expect(mockValidate).toHaveBeenCalledTimes(1));
    expect(mockValidate.mock.calls[0][1]).toEqual({
      item: { kind: "order", ids: ["O1"] },
      candidates: ["T1"],
    });
    const header = document.querySelector(
      '[data-lane-row="T1"]',
    ) as HTMLElement;
    await waitFor(() =>
      expect(
        within(header).getByText(
          "Blocked: Compartment T1-c1 can't carry ULSD after GAS.",
        ),
      ).toBeInTheDocument(),
    );
    const gauge = within(header).getByRole("list", {
      name: "compartments after this change",
    });
    expect(gauge).toHaveTextContent(
      "Compartment 1, empty, 30% full, blocked by this change",
    );
  });
});
