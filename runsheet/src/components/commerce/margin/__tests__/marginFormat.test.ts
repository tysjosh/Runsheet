/**
 * marginFormat: integer-only money formatting and the freeze-12 null rules.
 * A missing cost (null) is "No cost", never "$0.00"; undefined is a bug and
 * throws; anything that is not a safe integer throws.
 */
import * as fs from "node:fs";
import * as path from "node:path";
import {
  formatBasisPoints,
  formatCents,
  formatCostedCents,
  formatGallons,
  formatLocalDate,
  formatMicros,
  formatPct,
  microsToDecimalString,
  milliToDecimalString,
  NO_COST,
  noCostReasonLabel,
} from "../marginFormat";

describe("formatCents", () => {
  it("formats integer cents exactly", () => {
    expect(formatCents(0)).toBe("$0.00");
    expect(formatCents(5)).toBe("$0.05");
    expect(formatCents(123456)).toBe("$1,234.56");
    expect(formatCents(-20000)).toBe("-$200.00");
    expect(formatCents(Number.MAX_SAFE_INTEGER)).toBe("$90,071,992,547,409.91");
  });

  it("renders null as No cost, never $0.00", () => {
    expect(formatCents(null)).toBe("No cost");
    expect(formatCents(null)).not.toBe("$0.00");
  });

  it("throws on undefined and non-integers", () => {
    expect(() => formatCents(undefined as unknown as number)).toThrow(
      TypeError,
    );
    expect(() => formatCents(1.5)).toThrow(TypeError);
    expect(() => formatCents(Number.NaN)).toThrow(TypeError);
    expect(() => formatCents("100" as unknown as number)).toThrow(TypeError);
    expect(() => formatCents(2 ** 60)).toThrow(TypeError);
  });
});

describe("formatMicros", () => {
  it("keeps every significant digit and at least two", () => {
    expect(formatMicros(2_500_000)).toBe("$2.50");
    expect(formatMicros(2_456_789)).toBe("$2.456789");
    expect(formatMicros(100_000)).toBe("$0.10");
    expect(formatMicros(1)).toBe("$0.000001");
    expect(formatMicros(-200_000)).toBe("-$0.20");
    expect(formatMicros(100_000_000)).toBe("$100.00");
  });

  it("null is No cost; undefined and floats throw", () => {
    expect(formatMicros(null)).toBe(NO_COST);
    expect(() => formatMicros(undefined as unknown as number)).toThrow(
      TypeError,
    );
    expect(() => formatMicros(2.5)).toThrow(TypeError);
  });
});

describe("formatPct", () => {
  it("appends % to the server's 2-dp string", () => {
    expect(formatPct("16.67")).toBe("16.67%");
    expect(formatPct("-6.67")).toBe("-6.67%");
  });

  it("null is No cost; undefined and malformed values throw", () => {
    expect(formatPct(null)).toBe("No cost");
    expect(() => formatPct(undefined as unknown as string)).toThrow(TypeError);
    expect(() => formatPct(12.5 as unknown as string)).toThrow(TypeError);
    expect(() => formatPct("12.5")).toThrow(TypeError);
  });
});

describe("other integer formatters", () => {
  it("formats gallons, basis points and input strings", () => {
    expect(formatGallons(1_000_000_000)).toBe("1,000");
    expect(formatGallons(1_500_000)).toBe("1.5");
    expect(formatGallons(1)).toBe("0.000001");
    expect(formatBasisPoints(1250)).toBe("12.50%");
    expect(formatBasisPoints(5)).toBe("0.05%");
    expect(formatBasisPoints(null)).toBe("n/a");
    expect(microsToDecimalString(100_000)).toBe("0.100000");
    expect(microsToDecimalString(2_456_789)).toBe("2.456789");
    expect(milliToDecimalString(1_500)).toBe("1.500");
    expect(milliToDecimalString(1)).toBe("0.001");
  });

  it("labels no-cost reasons", () => {
    expect(noCostReasonLabel("no_lots_no_rack")).toMatch(/No purchases/);
    expect(noCostReasonLabel(null)).toBe("Reason not recorded");
    expect(noCostReasonLabel("new_reason")).toBe("new_reason");
  });
});

describe("formatLocalDate", () => {
  it("returns the calendar date in the given zone", () => {
    expect(
      formatLocalDate("2026-10-05T03:00:00+00:00", "America/Chicago"),
    ).toBe("2026-10-04");
    expect(formatLocalDate("2026-10-05T03:00:00+00:00", "UTC")).toBe(
      "2026-10-05",
    );
    // Winter (CST, UTC-6): 05:59Z is still the previous day.
    expect(formatLocalDate("2026-01-10T05:59:00Z", "America/Chicago")).toBe(
      "2026-01-09",
    );
  });
});

describe("formatCostedCents", () => {
  const block = (records: number, missing: number) => ({
    records,
    flag_counts: { missing_cost: { records: missing } },
  });
  it("is No cost when the block has no costed record", () => {
    expect(formatCostedCents(block(2, 2), 0)).toBe(NO_COST);
    expect(formatCostedCents(block(0, 0), 0)).toBe(NO_COST);
  });
  it("shows a real zero when a costed record exists", () => {
    expect(formatCostedCents(block(2, 1), 0)).toBe("$0.00");
    expect(formatCostedCents(block(1, 0), -20_000)).toBe("-$200.00");
  });
});

describe("no float division", () => {
  it("the module source divides nothing", () => {
    const source: string = fs.readFileSync(
      path.join(__dirname, "..", "marginFormat.ts"),
      "utf8",
    );
    // Comments stripped; biome formats a division operator as " / ".
    const code = source
      .replace(/\/\*[\s\S]*?\*\//g, "")
      .replace(/^\s*\/\/.*$/gm, "");
    expect(code).not.toMatch(/\s\/\s/);
    expect(code).not.toMatch(/toFixed|parseFloat|Math\.round/);
  });
});
