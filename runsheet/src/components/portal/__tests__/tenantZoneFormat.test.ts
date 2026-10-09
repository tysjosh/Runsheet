/**
 * portal-fixes A5: portal dates render in the tenant's zone (from
 * `/api/portal/me.time_zone`, pushed into `lib/format` by `PortalGate`), name
 * the zone on every time, and request windows are wall clock in that zone.
 */
import { configureFormat } from "../../../lib/format";
import type { PortalOrder } from "../../../services/portalApi";
import { tank } from "../__fixtures__/portal";
import { deliveryParts, quantityParts, quantityText } from "../OrderRow";
import {
  dateTimeZone,
  window as formatWindow,
  zonedInstant,
  zonedIsoDate,
  zoneLongName,
} from "../portalFormat";
import { buildWindow, maxGallons, minGallons } from "../useOrderRequest";

function order(overrides: Partial<PortalOrder> = {}): PortalOrder {
  return {
    order_id: "QA-ORD-1",
    status_code: "awaiting_confirmation",
    status_label: "Awaiting confirmation",
    product_code: "DIESEL_2",
    gallons_requested: 500,
    fill_to_full: false,
    window_start: null,
    window_end: null,
    po_number: null,
    tank: null,
    created_at: null,
    delivered_at: null,
    delivered_gallons: null,
    ticket_number: null,
    cancellable: false,
    ...overrides,
  };
}

const NOW = new Date("2026-10-08T17:00:00Z");

afterEach(() => configureFormat({ timeZone: undefined }));

describe("tenant time zone", () => {
  it("renders windows and moments in the tenant zone with the zone named", () => {
    configureFormat({ timeZone: "America/Chicago" });
    expect(
      formatWindow("2026-10-09T14:00:00Z", "2026-10-09T18:00:00Z", NOW),
    ).toBe("Fri 9 Oct, 9:00 AM – 1:00 PM CDT");
    expect(dateTimeZone("2026-09-26T23:55:00Z", NOW)).toBe(
      "Sat 26 Sep, 6:55 PM CDT",
    );
    configureFormat({ timeZone: "America/New_York" });
    expect(dateTimeZone("2026-09-26T23:55:00Z", NOW)).toBe(
      "Sat 26 Sep, 7:55 PM EDT",
    );
  });

  it("names the zone after a DST change on each row", () => {
    configureFormat({ timeZone: "America/Chicago" });
    expect(dateTimeZone("2026-12-01T18:00:00Z", NOW)).toBe(
      "Tue 1 Dec, 12:00 PM CST",
    );
  });

  it("a bare due date stays that calendar day", () => {
    configureFormat({ timeZone: "Pacific/Auckland" });
    expect(dateTimeZone("2026-10-31", NOW)).toBe("Sat 31 Oct");
  });

  it("builds request windows as wall clock in the tenant zone", () => {
    configureFormat({ timeZone: "America/Chicago" });
    expect(zonedInstant(2026, 10, 9, 9, 0).toISOString()).toBe(
      "2026-10-09T14:00:00.000Z",
    );
    expect(buildWindow("2026-10-09", "09:00", "13:00")).toEqual({
      window_start: "2026-10-09T14:00:00.000Z",
      window_end: "2026-10-09T18:00:00.000Z",
    });
    // A date alone is the whole local day, across the November DST change.
    expect(buildWindow("2026-11-01", "", "")).toEqual({
      window_start: "2026-11-01T05:00:00.000Z",
      window_end: "2026-11-02T06:00:00.000Z",
    });
    expect(zoneLongName(new Date("2026-10-09T14:00:00Z"))).toBe(
      "Central Daylight Time",
    );
  });

  it("today is the tenant's date", () => {
    configureFormat({ timeZone: "Pacific/Auckland" });
    expect(zonedIsoDate(0, new Date("2026-10-08T23:30:00Z"))).toBe(
      "2026-10-09",
    );
    configureFormat({ timeZone: "America/Chicago" });
    expect(zonedIsoDate(1, new Date("2026-10-08T23:30:00Z"))).toBe(
      "2026-10-09",
    );
  });
});

describe("order quantity and delivery cells (portal-fixes A4, A6)", () => {
  it("one pattern: whole gallons, then a qualifier", () => {
    expect(
      quantityParts(
        order({
          gallons_requested: 1600,
          status_code: "awaiting_confirmation",
        }),
      ),
    ).toEqual({ value: "1,600 gal", qualifier: "requested" });
    expect(
      quantityText(
        order({
          status_code: "delivered",
          delivered_gallons: 275.5,
          delivered_at: "2026-09-26T23:55:00Z",
        }),
      ),
    ).toBe("276 gal delivered");
    expect(
      quantityParts(order({ fill_to_full: true, gallons_requested: null })),
    ).toEqual({ value: "Fill to full", qualifier: null });
  });

  it("splits the delivery text into the day and the times", () => {
    configureFormat({ timeZone: "America/Chicago" });
    expect(
      deliveryParts(
        order({
          window_start: "2026-10-09T14:00:00Z",
          window_end: "2026-10-09T18:00:00Z",
          delivered_at: null,
        }),
      ),
    ).toEqual({ day: "Fri 9 Oct", rest: "9:00 AM – 1:00 PM CDT" });
  });
});

describe("request gallons limits (portal-fixes B2)", () => {
  it("room left at a fresh reading, capacity when stale, minimum 25", () => {
    expect(
      maxGallons(
        tank({
          capacity_gallons: 2000,
          current_level_gallons: 300,
          reading_stale: false,
        }),
      ),
    ).toBe(1700);
    expect(
      maxGallons(
        tank({
          capacity_gallons: 2000,
          current_level_gallons: 300,
          reading_stale: true,
        }),
      ),
    ).toBe(2000);
    expect(minGallons(tank({ capacity_gallons: 2000 }))).toBe(25);
    expect(minGallons(tank({ capacity_gallons: 20 }))).toBe(20);
  });
});
