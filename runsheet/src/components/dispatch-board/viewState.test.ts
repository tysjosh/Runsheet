/**
 * Board view state: URL first, local storage defaults (R4.4), date range (R2.2).
 */
import {
  addDays,
  clampDate,
  DEFAULT_VIEW,
  EMPTY_FILTERS,
  parseView,
  readStoredView,
  serverTrayFilters,
  todayIn,
  VIEW_STORAGE_KEY,
  viewToParams,
  writeStoredView,
} from "./viewState";

beforeEach(() => window.localStorage.clear());

describe("parseView", () => {
  it("defaults with no URL and no storage", () => {
    expect(parseView(new URLSearchParams())).toEqual(DEFAULT_VIEW);
  });

  it("reads every URL param and rejects bad values", () => {
    const v = parseView(
      new URLSearchParams(
        "date=2026-10-08&shift=night&zoom=sequence&density=compact&q=1042&call_type=will_call,one_off&warnings=1&truck=T-12&order=1042",
      ),
    );
    expect(v).toMatchObject({
      date: "2026-10-08",
      shift: "night",
      zoom: "sequence",
      density: "compact",
      search: "1042",
      truck: "T-12",
      order: "1042",
    });
    expect(v.filters.call_type).toEqual(["will_call", "one_off"]);
    expect(v.filters.has_warnings).toBe(true);
    const bad = parseView(
      new URLSearchParams(
        "date=2026-02-30&shift=evening&zoom=x&truck=<script>",
      ),
    );
    expect(bad.date).toBeNull();
    expect(bad.shift).toBe("all");
    expect(bad.zoom).toBe("timeline");
    expect(bad.truck).toBeNull();
  });

  it("local storage fills what the URL leaves out; the URL wins", () => {
    writeStoredView(window.localStorage, {
      ...DEFAULT_VIEW,
      date: "2026-10-01",
      shift: "day",
      density: "compact",
      search: "acme",
      filters: { ...EMPTY_FILTERS, product: ["ULSD"] },
    });
    const stored = readStoredView(window.localStorage);
    expect(stored).not.toHaveProperty("date");
    const v = parseView(new URLSearchParams("density=comfortable"), stored);
    expect(v).toMatchObject({
      date: null,
      shift: "day",
      density: "comfortable",
      search: "acme",
    });
    expect(v.filters.product).toEqual(["ULSD"]);
    // Any filter param in the URL replaces the stored filter set.
    expect(
      parseView(new URLSearchParams("status=on_hold"), stored).filters.product,
    ).toEqual([]);
  });

  it("corrupt storage is ignored", () => {
    window.localStorage.setItem(VIEW_STORAGE_KEY, "{not json");
    expect(readStoredView(window.localStorage)).toEqual({});
  });
});

describe("viewToParams", () => {
  it("round-trips and keeps unrelated params such as tab", () => {
    const view = parseView(
      new URLSearchParams(
        "date=2026-10-08&zoom=sequence&product=ULSD&truck=T1",
      ),
    );
    const p = viewToParams(view, new URLSearchParams("tab=board&shift=night"));
    expect(p.get("tab")).toBe("board");
    expect(p.get("shift")).toBeNull();
    expect(parseView(p)).toEqual(view);
  });
});

describe("dates", () => {
  it("clamps to 7 days back and 14 ahead", () => {
    expect(clampDate("2026-09-01", "2026-10-08")).toBe("2026-10-01");
    expect(clampDate("2026-11-01", "2026-10-08")).toBe("2026-10-22");
    expect(clampDate("2026-10-10", "2026-10-08")).toBe("2026-10-10");
    expect(addDays("2026-12-31", 1)).toBe("2027-01-01");
  });

  it("today follows the tenant zone", () => {
    const now = new Date("2026-10-08T03:00:00Z");
    expect(todayIn("America/Chicago", now)).toBe("2026-10-07");
    expect(todayIn("UTC", now)).toBe("2026-10-08");
    expect(todayIn("Not/AZone", now)).toMatch(/^\d{4}-\d{2}-\d{2}$/);
  });
});

describe("serverTrayFilters", () => {
  it("sends one value per param, never several", () => {
    expect(
      serverTrayFilters({
        ...EMPTY_FILTERS,
        call_type: ["will_call"],
        product: ["ULSD"],
        window: ["overdue"],
      }),
    ).toEqual({
      call_type: "will_call",
      product: "ULSD",
      window: "overdue",
    });
    expect(
      serverTrayFilters({
        ...EMPTY_FILTERS,
        call_type: ["will_call", "one_off"],
      }),
    ).toEqual({
      call_type: undefined,
      product: undefined,
      window: undefined,
    });
  });
});
