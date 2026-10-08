/**
 * Portal formatting (R14.4, design §11.7). Runs in the test zone; expected
 * values are computed for local wall-clock instants so the suite passes in
 * any zone.
 */
import {
  date,
  dateTime,
  days,
  deliveredVolume,
  window as formatWindow,
  money,
  percent,
  time,
  unitPrice,
  volume,
  zoneName,
} from "../portalFormat";

const NOW = new Date(2026, 9, 8, 12, 0);

describe("portalFormat", () => {
  it("dates: weekday day month, year only outside the current year", () => {
    expect(date(new Date(2026, 9, 9, 9, 20), NOW)).toBe("Fri 9 Oct");
    expect(date(new Date(2025, 9, 9, 9, 20), NOW)).toBe("Thu 9 Oct 2025");
  });

  it("a bare YYYY-MM-DD is that calendar day in every zone", () => {
    expect(date("2026-10-31", NOW)).toBe("Sat 31 Oct");
  });

  it("12-hour times without a leading zero", () => {
    expect(time(new Date(2026, 9, 9, 9, 20))).toBe("9:20 AM");
    expect(time(new Date(2026, 9, 9, 15, 5))).toBe("3:05 PM");
  });

  it("windows name the zone once, at the end", () => {
    const a = new Date(2026, 9, 9, 9, 20);
    const b = new Date(2026, 9, 9, 15, 20);
    const z = zoneName(b);
    expect(formatWindow(a, b, NOW)).toBe(
      `Fri 9 Oct, 9:20 AM – 3:20 PM ${z}`.trim(),
    );
    expect(formatWindow(a, b, NOW).split(z).length - 1).toBe(z ? 1 : 0);
  });

  it("a whole local day is just the date; no start is Not scheduled", () => {
    expect(formatWindow(new Date(2026, 9, 9), new Date(2026, 9, 10), NOW)).toBe(
      "Fri 9 Oct",
    );
    expect(formatWindow(null, null, NOW)).toBe("Not scheduled");
  });

  it("dateTime", () => {
    expect(dateTime(new Date(2026, 9, 2, 11, 44), NOW)).toBe(
      "Fri 2 Oct, 11:44 AM",
    );
  });

  it("money from cents, volumes, percent", () => {
    expect(money(124050)).toBe("$1,240.50");
    expect(money(null)).toBe("—");
    expect(volume(1240)).toBe("1,240 gal");
    expect(volume(1240, "L")).toBe("1,240 L");
    expect(deliveredVolume(1187.4)).toBe("1,187.4 gal");
    expect(deliveredVolume(1200)).toBe("1,200 gal");
    expect(percent(31.6)).toBe("32 %");
  });

  it("unit price at its stored precision (PE3)", () => {
    expect(unitPrice("2.9193")).toBe("$2.9193");
    expect(unitPrice("2.91")).toBe("$2.91");
    expect(unitPrice("2.915")).toBe("$2.915");
    expect(unitPrice(null)).toBe("—");
  });

  it("pluralises days", () => {
    expect(days(1)).toBe("1 day");
    expect(days(4)).toBe("4 days");
    expect(days(0)).toBe("0 days");
  });
});
