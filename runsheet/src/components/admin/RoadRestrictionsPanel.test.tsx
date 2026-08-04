/**
 * Tests for :file:`RoadRestrictionsPanel.tsx`.
 *
 * Coverage targets the behaviours the task calls out:
 *
 *  1. **``parseGeoJsonPolygon`` pure helper** — malformed JSON is
 *     surfaced before hitting the network, and only Polygon /
 *     MultiPolygon shapes are accepted.
 *  2. **``canUploadRoadRestriction`` role gate** — an **exact** match
 *     against ``dispatcher`` / ``admin``, case- and whitespace-
 *     insensitive, mirroring the backend's
 *     ``require_role(tenant, "dispatcher", "admin")``. This used to be a
 *     substring match, and the tests below used to assert that
 *     permissive behaviour; both were wrong, because the UI enabled an
 *     upload the API then refused with 403.
 *  3. **List rendering** — fetched restrictions render as cards with
 *     severity + GeoJSON preview.
 *  4. **Upload form role gating** — hidden when the caller is not a
 *     dispatcher / admin.
 *  5. **Upload form submission** — valid GeoJSON posts through
 *     ``uploadStormRoadRestriction`` and re-fetches the list on
 *     success.
 *  6. **GeoJSON validation** — malformed polygons surface an inline
 *     error and never call the API.
 *
 * The fuelApi module is mocked wholesale so these tests never touch
 * the network.
 *
 * Validates: Requirements 9.3.3, 9.3.5.
 */

import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

jest.mock("../../services/fuelApi", () => {
  const actual = jest.requireActual("../../services/fuelApi");
  return {
    ...actual,
    listStormRoadRestrictions: jest.fn(),
    uploadStormRoadRestriction: jest.fn(),
  };
});

import type {
  StormRoadRestriction,
  StormRoadRestrictionListResponse,
} from "../../services/fuelApi";
import {
  listStormRoadRestrictions,
  uploadStormRoadRestriction,
} from "../../services/fuelApi";
import RoadRestrictionsPanel, {
  canUploadRoadRestriction,
  parseGeoJsonPolygon,
  previewGeoJson,
  SEVERITY_BADGE_CONFIG,
} from "./RoadRestrictionsPanel";

const mockList = listStormRoadRestrictions as jest.MockedFunction<
  typeof listStormRoadRestrictions
>;
const mockUpload = uploadStormRoadRestriction as jest.MockedFunction<
  typeof uploadStormRoadRestriction
>;

// ─── Fixtures ────────────────────────────────────────────────────────────────

function restrictionFixture(
  overrides: Partial<StormRoadRestriction> = {},
): StormRoadRestriction {
  const now = new Date().toISOString();
  const base: StormRoadRestriction = {
    restriction_id: "rr-001",
    tenant_id: "tenant-a",
    polygon: {
      type: "Polygon",
      coordinates: [
        [
          [-74.1, 40.7],
          [-74.0, 40.7],
          [-74.0, 40.8],
          [-74.1, 40.8],
          [-74.1, 40.7],
        ],
      ],
    },
    effective_from: now,
    effective_to: null,
    source: "dispatcher",
    severity: "severe",
    reason: "Broad St bridge closure",
    created_at: now,
    updated_at: now,
  };
  return { ...base, ...overrides };
}

function listResponseFixture(
  items: StormRoadRestriction[],
): StormRoadRestrictionListResponse {
  return { items, total: items.length };
}

const VALID_GEOJSON = JSON.stringify({
  type: "Polygon",
  coordinates: [
    [
      [-74.1, 40.7],
      [-74.0, 40.7],
      [-74.0, 40.8],
      [-74.1, 40.8],
      [-74.1, 40.7],
    ],
  ],
});

// ─── Pure helpers ────────────────────────────────────────────────────────────

describe("parseGeoJsonPolygon", () => {
  it("accepts a well-formed Polygon", () => {
    const result = parseGeoJsonPolygon(VALID_GEOJSON);
    expect(result.ok).toBe(true);
    if (result.ok) expect(result.value.type).toBe("Polygon");
  });

  it("accepts a well-formed MultiPolygon", () => {
    const raw = JSON.stringify({
      type: "MultiPolygon",
      coordinates: [[[[0, 0]]]],
    });
    expect(parseGeoJsonPolygon(raw).ok).toBe(true);
  });

  it("rejects empty input", () => {
    const result = parseGeoJsonPolygon("");
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toMatch(/required/i);
  });

  it("rejects malformed JSON", () => {
    const result = parseGeoJsonPolygon("{not-json");
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toMatch(/valid JSON/i);
  });

  it("rejects a non-polygon GeoJSON type", () => {
    const raw = JSON.stringify({ type: "Point", coordinates: [0, 0] });
    const result = parseGeoJsonPolygon(raw);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toMatch(/Polygon/);
  });

  it("rejects non-array coordinates", () => {
    const raw = JSON.stringify({ type: "Polygon", coordinates: "nope" });
    const result = parseGeoJsonPolygon(raw);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toMatch(/coordinates/);
  });
});

describe("canUploadRoadRestriction", () => {
  it("returns false for empty / missing roles", () => {
    expect(canUploadRoadRestriction(null)).toBe(false);
    expect(canUploadRoadRestriction([])).toBe(false);
    expect(canUploadRoadRestriction(["viewer"])).toBe(false);
  });

  it("matches dispatcher / admin exactly, ignoring case and padding", () => {
    expect(canUploadRoadRestriction(["Dispatcher"])).toBe(true);
    expect(canUploadRoadRestriction(["ADMIN"])).toBe(true);
    expect(canUploadRoadRestriction([" admin "])).toBe(true);
  });

  it("refuses a role that merely contains dispatcher or admin", () => {
    // These two assertions used to expect `true`. That was wrong, not merely
    // lenient: the backend gate is `require_role(tenant, "dispatcher", "admin")`,
    // an exact match, so a permissive UI enabled an upload form the API then
    // refused with 403. A user cannot tell that apart from a broken feature.
    expect(canUploadRoadRestriction(["lead-dispatcher"])).toBe(false);
    expect(canUploadRoadRestriction(["ops-admin-eu"])).toBe(false);
  });
});

describe("previewGeoJson", () => {
  it("truncates long payloads with an ellipsis", () => {
    const polygon = {
      type: "Polygon",
      coordinates: Array.from({ length: 50 }, (_, i) => [i, i]),
    } as unknown as Record<string, unknown>;
    const preview = previewGeoJson(polygon, 40);
    expect(preview.length).toBeLessThanOrEqual(41); // +1 for ellipsis
    expect(preview.endsWith("…")).toBe(true);
  });
});

describe("SEVERITY_BADGE_CONFIG", () => {
  it("covers every severity bucket", () => {
    expect(Object.keys(SEVERITY_BADGE_CONFIG).sort()).toEqual(
      ["extreme", "minor", "moderate", "severe"].sort(),
    );
  });
});

// ─── Component tests ─────────────────────────────────────────────────────────

describe("RoadRestrictionsPanel", () => {
  beforeEach(() => {
    mockList.mockReset();
    mockUpload.mockReset();
  });

  it("renders the fetched list as cards", async () => {
    mockList.mockResolvedValue(
      listResponseFixture([
        restrictionFixture({
          restriction_id: "rr-001",
          reason: "Broad St bridge closure",
        }),
        restrictionFixture({
          restriction_id: "rr-002",
          reason: "I-95 flooding",
          severity: "extreme",
        }),
      ]),
    );

    render(<RoadRestrictionsPanel roles={["admin"]} />);

    await waitFor(() => {
      expect(
        screen.getByTestId("road-restriction-card-rr-001"),
      ).toBeInTheDocument();
    });
    expect(
      screen.getByTestId("road-restriction-card-rr-002"),
    ).toBeInTheDocument();
    expect(screen.getByText(/Broad St bridge closure/)).toBeInTheDocument();
    expect(screen.getByText(/I-95 flooding/)).toBeInTheDocument();
  });

  it("renders the empty state when the list is empty", async () => {
    mockList.mockResolvedValue(listResponseFixture([]));
    render(<RoadRestrictionsPanel roles={["admin"]} />);

    await waitFor(() => {
      expect(
        screen.getByText(/No road restrictions configured/i),
      ).toBeInTheDocument();
    });
  });

  it("surfaces a list fetch error without crashing", async () => {
    mockList.mockRejectedValue(new Error("backend boom"));
    render(<RoadRestrictionsPanel roles={["admin"]} />);

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent(/boom/);
    });
  });

  it("hides the upload form for non-dispatcher / non-admin roles", async () => {
    mockList.mockResolvedValue(listResponseFixture([restrictionFixture()]));
    render(<RoadRestrictionsPanel roles={["viewer"]} />);

    await waitFor(() => {
      expect(
        screen.getByTestId("road-restriction-card-rr-001"),
      ).toBeInTheDocument();
    });
    expect(
      screen.queryByTestId("road-restriction-upload-form"),
    ).not.toBeInTheDocument();
    expect(
      screen.getByTestId("road-restriction-role-gate-notice"),
    ).toBeInTheDocument();
  });

  it("shows the upload form for dispatcher / admin roles", async () => {
    mockList.mockResolvedValue(listResponseFixture([]));
    render(<RoadRestrictionsPanel roles={["dispatcher"]} />);

    await waitFor(() => {
      expect(
        screen.getByTestId("road-restriction-upload-form"),
      ).toBeInTheDocument();
    });
  });

  it("submits valid GeoJSON and re-fetches the list on success", async () => {
    mockList
      .mockResolvedValueOnce(listResponseFixture([]))
      .mockResolvedValueOnce(
        listResponseFixture([
          restrictionFixture({ reason: "Broad St bridge closure" }),
        ]),
      );
    mockUpload.mockResolvedValue(
      restrictionFixture({ reason: "Broad St bridge closure" }),
    );

    render(<RoadRestrictionsPanel roles={["dispatcher"]} />);

    const form = await screen.findByTestId("road-restriction-upload-form");
    fireEvent.change(within(form).getByLabelText(/Name/i), {
      target: { value: "Broad St bridge closure" },
    });
    fireEvent.change(
      within(form).getByTestId("road-restriction-polygon-input"),
      {
        target: { value: VALID_GEOJSON },
      },
    );
    await act(async () => {
      fireEvent.submit(form);
    });

    await waitFor(() => {
      expect(mockUpload).toHaveBeenCalledTimes(1);
    });
    const [body] = mockUpload.mock.calls[0];
    expect(body).toEqual(
      expect.objectContaining({
        severity: "severe",
        source: "dispatcher",
        reason: "Broad St bridge closure",
      }),
    );
    expect((body.polygon as { type: string }).type).toBe("Polygon");

    // List is re-fetched after success.
    await waitFor(() => {
      expect(mockList).toHaveBeenCalledTimes(2);
    });
  });

  it("blocks submit when the GeoJSON is malformed", async () => {
    mockList.mockResolvedValue(listResponseFixture([]));
    render(<RoadRestrictionsPanel roles={["dispatcher"]} />);

    const form = await screen.findByTestId("road-restriction-upload-form");
    fireEvent.change(within(form).getByLabelText(/Name/i), {
      target: { value: "Broken polygon" },
    });
    fireEvent.change(
      within(form).getByTestId("road-restriction-polygon-input"),
      {
        target: { value: "{ not json" },
      },
    );
    await act(async () => {
      fireEvent.submit(form);
    });

    expect(mockUpload).not.toHaveBeenCalled();
    expect(
      screen.getByTestId("road-restriction-polygon-error"),
    ).toHaveTextContent(/valid JSON/i);
  });

  it("blocks submit when the name is blank", async () => {
    mockList.mockResolvedValue(listResponseFixture([]));
    render(<RoadRestrictionsPanel roles={["admin"]} />);

    const form = await screen.findByTestId("road-restriction-upload-form");
    fireEvent.change(
      within(form).getByTestId("road-restriction-polygon-input"),
      {
        target: { value: VALID_GEOJSON },
      },
    );
    await act(async () => {
      fireEvent.submit(form);
    });

    expect(mockUpload).not.toHaveBeenCalled();
    expect(within(form).getByText(/Name is required/i)).toBeInTheDocument();
  });
});
