/** Tank level status, sort and text (D23, design §11.5). */
import { tank } from "../__fixtures__/portal";
import {
  levelLine,
  levelValueText,
  NO_FORECAST_TEXT,
  STALE_READING_TEXT,
  sortTanks,
  tankLevel,
} from "../tankLevel";

const fc = (days: number) => ({
  runout_at: "2026-10-20T12:00:00Z",
  days_to_runout: days,
  generated_at: "2026-10-08T12:00:00Z",
});

describe("tankLevel", () => {
  it.each([
    [{ percent_full: 62, forecast: fc(11) }, "ok", "OK"],
    [{ percent_full: 62, forecast: fc(5) }, "warning", "Order soon"],
    [{ percent_full: 62, forecast: fc(2) }, "critical", "Order now"],
    [{ percent_full: 29.9, forecast: null }, "warning", "Order soon"],
    [{ percent_full: 30, forecast: null }, "ok", "OK"],
    [{ percent_full: 14.9, forecast: fc(20) }, "critical", "Order now"],
    [{ percent_full: 12, forecast: null }, "critical", "Order now"],
  ] as const)("%j → %s", (input, status, label) => {
    expect(tankLevel(input)).toEqual({ status, label });
  });

  it("ignores a stale reading for the status", () => {
    expect(tankLevel(tank({ reading_stale: true })).status).toBe("ok");
  });
});

describe("sortTanks", () => {
  it("ranks critical, warning, ok, then days to runout (nulls last), then title", () => {
    const tanks = [
      tank({ customer_tank_id: "ok-b", label: "B", forecast: fc(20) }),
      tank({
        customer_tank_id: "warn-null",
        label: "W",
        percent_full: 25,
        forecast: null,
      }),
      tank({
        customer_tank_id: "crit",
        label: "C",
        percent_full: 12,
        forecast: fc(1),
      }),
      tank({ customer_tank_id: "warn-4", label: "W4", forecast: fc(4) }),
      tank({ customer_tank_id: "ok-a", label: "A", forecast: fc(20) }),
    ];
    expect(
      sortTanks(tanks, (t) => t.label).map((t) => t.customer_tank_id),
    ).toEqual(["crit", "warn-4", "warn-null", "ok-a", "ok-b"]);
  });
});

describe("level text", () => {
  it("reads the meter value as design §11.5 writes it", () => {
    expect(
      levelValueText({
        current_level_gallons: 180,
        capacity_gallons: 1500,
        percent_full: 12,
      }),
    ).toBe("180 of 1,500 gal, 12 %");
  });

  it("joins level, percent and runout, pluralised", () => {
    expect(levelLine(tank({ forecast: fc(4) }))).toBe(
      "1,240 of 2,000 gal · 62 % · empty in about 4 days",
    );
    expect(levelLine(tank({ forecast: fc(1) }))).toBe(
      "1,240 of 2,000 gal · 62 % · empty in about 1 day",
    );
  });

  it("uses the R7.2 and R7.3 fixed texts", () => {
    expect(levelLine(tank({ forecast: null, reading_stale: true }))).toBe(
      `1,240 of 2,000 gal · 62 % · ${NO_FORECAST_TEXT} · ${STALE_READING_TEXT}`,
    );
  });
});
