import {
  configureFormat,
  date,
  dateLong,
  dateTime,
  EMPTY,
  format,
  formatConfig,
  gallons,
  humanize,
  liters,
  money,
  number,
  parseNumber,
  pct,
  productName,
  relative,
  time,
} from "./format";

const TZ = "America/Chicago";
// 2026-10-08 13:30 UTC = Thu 8 Oct 08:30 in Chicago (CDT, UTC-5).
const T = "2026-10-08T13:30:00Z";

afterEach(() => configureFormat({ locale: undefined, timeZone: undefined }));

describe("numbers (en-US)", () => {
  beforeEach(() => configureFormat({ locale: "en-US", timeZone: TZ }));

  it("groups and rounds to whole gallons by default", () => {
    expect(gallons(4200)).toBe("4,200 gal");
    // The owner-reported station bug: 20,000 L shown as 5283,441047162968.
    expect(gallons(5283.441047162968)).toBe("5,283 gal");
    expect(gallons(12.5, { decimals: 1 })).toBe("12.5 gal");
  });

  it("formats litres, money and percentages", () => {
    expect(liters(15898.73)).toBe("15,899 L");
    expect(money(1234.5)).toBe("$1,234.50");
    expect(money(-3)).toBe("-$3.00");
    expect(pct(12)).toBe("12%");
    expect(pct(12.345, { decimals: 1 })).toBe("12.3%");
    expect(pct(0.25, { fraction: true })).toBe("25%");
  });

  it("renders missing and non-finite values as an em dash", () => {
    for (const v of [
      null,
      undefined,
      "",
      Number.NaN,
      Number.POSITIVE_INFINITY,
      "abc",
    ]) {
      expect(number(v as never)).toBe(EMPTY);
      expect(gallons(v as never)).toBe(EMPTY);
      expect(money(v as never)).toBe(EMPTY);
    }
  });

  it("parses en-US input", () => {
    expect(parseNumber("5,283.4")).toBe(5283.4);
    expect(parseNumber(" 20000 ")).toBe(20000);
    expect(parseNumber("-12")).toBe(-12);
    expect(parseNumber("")).toBeNull();
    expect(parseNumber("12abc")).toBeNaN();
    expect(parseNumber("1.2.3")).toBeNaN();
  });
});

describe("numbers (de-DE)", () => {
  beforeEach(() => configureFormat({ locale: "de-DE", timeZone: TZ }));

  it("uses German separators", () => {
    expect(gallons(5283.44)).toBe("5.283 gal");
    expect(number(5283.44, { decimals: 1 })).toBe("5.283,4");
    expect(money(1234.5)).toMatch(/1\.234,50/);
  });

  it("parses German input", () => {
    expect(parseNumber("5.283,4")).toBe(5283.4);
    expect(parseNumber("12,5")).toBe(12.5);
  });
});

describe("dates in the tenant time zone", () => {
  it("formats day, long date, time and date-time (en-US, Chicago)", () => {
    configureFormat({ locale: "en-US", timeZone: TZ });
    expect(date(T)).toBe("Thu 8 Oct");
    expect(dateLong(T)).toBe("Thu 8 Oct 2026");
    expect(time(T)).toBe("08:30");
    expect(dateTime(T)).toBe("Thu 8 Oct, 08:30");
  });

  it("uses the tenant zone, not UTC, for the calendar day", () => {
    configureFormat({ locale: "en-US", timeZone: TZ });
    // 03:00 UTC on the 9th is still the evening of the 8th in Chicago.
    expect(date("2026-10-09T03:00:00Z")).toBe("Thu 8 Oct");
    expect(time("2026-10-09T03:00:00Z")).toBe("22:00");
  });

  it("localises names in de-DE", () => {
    configureFormat({ locale: "de-DE", timeZone: TZ });
    expect(date(T)).toMatch(/^Do\.? 8\.? Okt\.?$/);
    expect(time(T)).toBe("08:30");
  });

  it("formats windows on one day and across days", () => {
    configureFormat({ locale: "en-US", timeZone: TZ });
    expect(format.window(T, "2026-10-08T15:30:00Z")).toBe("08:30–10:30");
    expect(format.window("2026-10-09T03:00:00Z", "2026-10-09T07:00:00Z")).toBe(
      "Thu 8 Oct, 22:00 – Fri 9 Oct, 02:00",
    );
    expect(format.window(T, null)).toBe("from 08:30");
    expect(format.window(null, null)).toBe(EMPTY);
  });

  it("formats relative times", () => {
    configureFormat({ locale: "en-US", timeZone: TZ });
    const now = Date.parse(T);
    expect(relative(now - 10_000, { now })).toBe("just now");
    expect(relative(now - 12 * 60_000, { now })).toBe("12 min ago");
    expect(relative(now + 20 * 60_000, { now })).toBe("in 20 min");
    expect(relative(now - 3 * 3_600_000, { now })).toBe("3 h ago");
    expect(relative(now - 3 * 86_400_000, { now })).toBe("Mon 5 Oct");
  });

  it("returns an em dash for invalid dates", () => {
    expect(date("not a date")).toBe(EMPTY);
    expect(time(undefined)).toBe(EMPTY);
  });

  it("configureFormat clears values with undefined", () => {
    configureFormat({ timeZone: TZ });
    expect(formatConfig().timeZone).toBe(TZ);
    configureFormat({ timeZone: undefined });
    expect(formatConfig().timeZone).toBeUndefined();
  });
});

describe("duration", () => {
  it("renders a short span with at most two units", () => {
    expect(format.duration(45)).toBe("45 s");
    expect(format.duration(720)).toBe("12 min");
    expect(format.duration(3599)).toBe("1 h");
    expect(format.duration(12_000)).toBe("3 h 20 min");
    expect(format.duration(86_400)).toBe("1 d");
    expect(format.duration(187_200)).toBe("2 d 4 h");
  });
  it("renders the empty dash for missing or negative values", () => {
    expect(format.duration(null)).toBe("—");
    expect(format.duration(-5)).toBe("—");
    expect(format.duration(Number.NaN)).toBe("—");
  });
});

describe("product names", () => {
  it("uses the token name for catalog codes", () => {
    expect(productName("GASOLINE_REG")).toBe("Regular unleaded");
    expect(productName("OFF_ROAD_DIESEL")).toBe("Off-road dyed diesel");
  });
  it("humanises unknown codes and handles empties", () => {
    expect(productName("JET_A")).toBe("Jet a");
    expect(productName(null)).toBe(EMPTY);
    expect(humanize("bio-diesel_b20")).toBe("Bio diesel b20");
  });
});
