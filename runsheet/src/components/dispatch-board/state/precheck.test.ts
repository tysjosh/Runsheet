/**
 * `precheck` never says pass (design K3.8): it guesses a likely block from
 * server-computed fields, otherwise it answers `unknown`.
 */
import { type PrecheckItem, precheck, precheckLanes } from "./precheck";
import {
  makeCompartment,
  makeDriver,
  makeLane,
  makeTrayOrder,
} from "./testFixtures";

const lane = makeLane("T1", 1, {
  compartments: [
    makeCompartment("c1", {
      capacity_l: 10000,
      accepts_products: ["ULSD", "UNL87"],
    }),
    makeCompartment("c2", { capacity_l: 10000, accepts_products: ["ULSD"] }),
  ],
});

const cases: [
  string,
  PrecheckItem,
  ReturnType<typeof makeLane>,
  string | null,
  boolean,
][] = [
  [
    "on-hold order",
    {
      kind: "order",
      order: makeTrayOrder("O1", { draggable: false, block_reason: "on_hold" }),
    },
    lane,
    "on_hold",
    false,
  ],
  [
    "product no compartment accepts",
    { kind: "order", order: makeTrayOrder("O1", { product_code: "JET_A" }) },
    lane,
    "no_compatible_compartments",
    false,
  ],
  [
    "order bigger than the empty truck",
    { kind: "order", order: makeTrayOrder("O1", { gallons_requested: 6000 }) },
    lane,
    "capacity_shortfall",
    false,
  ],
  [
    "fitting order",
    { kind: "order", order: makeTrayOrder("O1") },
    lane,
    null,
    false,
  ],
  [
    "fill-to-full order",
    {
      kind: "order",
      order: makeTrayOrder("O1", {
        fill_to_full: true,
        gallons_requested: null,
      }),
    },
    lane,
    null,
    false,
  ],
  [
    "compartments with unknown products",
    { kind: "order", order: makeTrayOrder("O1", { product_code: "JET_A" }) },
    makeLane("T1", 1, {
      compartments: [makeCompartment("c1", { capacity_l: 20000 })],
    }),
    null,
    false,
  ],
  [
    "ineligible driver",
    {
      kind: "driver",
      driver: makeDriver("D1", {
        eligible: false,
        ineligible_reasons: ["cdl_expired"],
      }),
    },
    lane,
    "cdl_expired",
    false,
  ],
  [
    "eligible driver",
    { kind: "driver", driver: makeDriver("D1") },
    lane,
    null,
    false,
  ],
  [
    "driver with unknown eligibility",
    { kind: "driver", driver: makeDriver("D1", { eligible: null }) },
    lane,
    null,
    false,
  ],
  ["stop move", { kind: "stop", ids: ["O1"] }, lane, null, false],
  ["load move", { kind: "load", ids: ["L1"] }, lane, null, false],
  [
    "publishing lane",
    { kind: "order", order: makeTrayOrder("O1") },
    makeLane("T1", 1, { state: "publishing" }),
    "publishing",
    false,
  ],
  [
    "recovering lane",
    { kind: "stop", ids: ["O1"] },
    makeLane("T1", 1, { state: "recovering" }),
    "recovery_pending",
    false,
  ],
  [
    "read-only board",
    { kind: "order", order: makeTrayOrder("O1") },
    lane,
    "read_only",
    true,
  ],
];

describe("precheck", () => {
  it.each(cases)("%s", (_name, item, target, reason, readOnly) => {
    const result = precheck(item, target, { read_only: readOnly });
    if (reason === null) {
      expect(result).toEqual({ kind: "unknown" });
    } else {
      expect(result).toEqual({ kind: "likely_block", reason });
    }
  });

  it("never returns pass for any case", () => {
    for (const [, item, target, , readOnly] of cases) {
      const result = precheck(item, target, { read_only: readOnly });
      expect(["likely_block", "unknown"]).toContain(result.kind);
    }
  });

  it("precheckLanes keys results by truck", () => {
    const out = precheckLanes({ kind: "order", order: makeTrayOrder("O1") }, [
      lane,
      makeLane("T2", 1, { state: "publishing" }),
    ]);
    expect(out.T1.kind).toBe("unknown");
    expect(out.T2).toEqual({ kind: "likely_block", reason: "publishing" });
  });
});
