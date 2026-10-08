/**
 * Tests for FleetTracking's `?asset=` deep-link focus.
 *
 * `entityHref("asset", id)` routes to `/dashboard/fleet?asset=<id>`, and the
 * Fleet page hands that id down as `focusAssetId`. These tests pin that the
 * param is actually consumed — a link that navigates but changes nothing is the
 * defect being eliminated:
 * - a matching asset is selected and reported up (so the map focuses it)
 * - an asset hidden by the default "In transit only" filter is revealed
 * - focus is applied once, so a later manual row click is not hijacked
 * - an id that matches nothing raises a dismissible notice, not silence
 */

import "@testing-library/jest-dom";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

jest.mock("../services/api", () => ({
  apiService: {
    getTrucks: jest.fn(),
    getAssets: jest.fn(),
    getFleetSummary: jest.fn(),
    getAssetCompliance: jest.fn(),
    createAsset: jest.fn(),
  },
  ApiError: class ApiError extends Error {},
}));

// The live fleet socket is irrelevant here; keep the component deterministic.
jest.mock("../hooks", () => ({
  useFleetWebSocket: () => ({ state: "connected", reconnectAttempt: 0 }),
}));

import { apiService } from "../services/api";
import type { AssetSummary, Truck } from "../types/api";
import FleetTracking from "./FleetTracking";

const mockGetTrucks = apiService.getTrucks as jest.MockedFunction<
  typeof apiService.getTrucks
>;
const mockGetFleetSummary = apiService.getFleetSummary as jest.MockedFunction<
  typeof apiService.getFleetSummary
>;
const mockGetAssetCompliance =
  apiService.getAssetCompliance as jest.MockedFunction<
    typeof apiService.getAssetCompliance
  >;

// ─── Fixtures ────────────────────────────────────────────────────────────────

function location(name: string) {
  return {
    id: `loc-${name}`,
    name,
    type: "depot" as const,
    coordinates: { lat: 1, lon: 2 },
    address: name,
  };
}

function truckFixture(overrides: Partial<Truck> = {}): Truck {
  return {
    id: "TRK-1",
    assetType: "vehicle",
    assetSubtype: "fuel_truck",
    name: "Truck One",
    plateNumber: "PLATE-1",
    status: "on_time",
    currentLocation: location("Depot A"),
    destination: location("Site B"),
    route: {
      id: "route-1",
      origin: location("Depot A"),
      destination: location("Site B"),
      waypoints: [],
      distance: 10,
      estimatedDuration: 60,
    },
    lastUpdate: "2024-06-01T12:00:00Z",
    ...overrides,
  };
}

function summaryFixture(): AssetSummary {
  return {
    totalTrucks: 2,
    activeTrucks: 2,
    onTimeTrucks: 1,
    delayedTrucks: 0,
    averageDelay: 0,
    byType: { vehicle: 2, vessel: 0, equipment: 0, container: 0 },
    bySubtype: {
      truck: 0,
      fuel_truck: 2,
      personnel_vehicle: 0,
      boat: 0,
      barge: 0,
      crane: 0,
      forklift: 0,
      cargo_container: 0,
      ISO_tank: 0,
    },
  };
}

function apiResponse<T>(data: T) {
  return {
    data,
    success: true,
    timestamp: "2024-06-01T12:00:00Z",
  };
}

function seedFleet(trucks: Truck[]) {
  mockGetTrucks.mockResolvedValue(apiResponse(trucks));
  mockGetFleetSummary.mockResolvedValue(apiResponse(summaryFixture()));
  mockGetAssetCompliance.mockResolvedValue({
    asset_id: "any",
    overall_status: "unknown",
  } as Awaited<ReturnType<typeof apiService.getAssetCompliance>>);
}

/** The table row rendering a given plate/name, via its cell text. */
function rowFor(text: string): HTMLElement {
  const cell = screen.getByText(text);
  const row = cell.closest("tr");
  if (!row) throw new Error(`No row found for ${text}`);
  return row;
}

// ─── Tests ───────────────────────────────────────────────────────────────────

describe("FleetTracking — ?asset= deep-link focus", () => {
  it("selects the referenced asset and reports it up so the map focuses it", async () => {
    seedFleet([
      truckFixture({ id: "TRK-1", plateNumber: "PLATE-1" }),
      truckFixture({ id: "TRK-2", plateNumber: "PLATE-2", name: "Truck Two" }),
    ]);
    const onTruckSelect = jest.fn();

    render(
      <FleetTracking onTruckSelect={onTruckSelect} focusAssetId="TRK-2" />,
    );

    await waitFor(() =>
      expect(onTruckSelect).toHaveBeenCalledWith(
        expect.objectContaining({ id: "TRK-2" }),
      ),
    );
    // Selection is visible in the table, not just reported upward.
    expect(rowFor("PLATE-2")).toHaveClass("bg-info-light");
    expect(rowFor("PLATE-1")).not.toHaveClass("bg-info-light");
    expect(
      screen.queryByTestId("focus-asset-not-found"),
    ).not.toBeInTheDocument();
  });

  it("shows every asset by default (status filter All), parked ones included", async () => {
    seedFleet([
      truckFixture({ id: "TRK-1", plateNumber: "PLATE-1", status: "on_time" }),
      truckFixture({
        id: "TRK-PARKED",
        plateNumber: "PLATE-PARKED",
        name: "Parked Truck",
        status: "maintenance",
      }),
    ]);
    const onTruckSelect = jest.fn();

    render(
      <FleetTracking onTruckSelect={onTruckSelect} focusAssetId="TRK-PARKED" />,
    );

    expect(await screen.findByText("PLATE-PARKED")).toBeInTheDocument();
    expect(rowFor("PLATE-PARKED")).toHaveClass("bg-info-light");
    expect(rowFor("PLATE-1")).toBeInTheDocument();
    expect(onTruckSelect).toHaveBeenCalledWith(
      expect.objectContaining({ id: "TRK-PARKED" }),
    );
    expect(screen.getByRole("button", { name: /All\s*2/ })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.queryByLabelText(/in transit only/i)).toBeNull();
  });

  it("filters by status chips with counts and badges carry a label", async () => {
    seedFleet([
      truckFixture({ id: "TRK-1", plateNumber: "PLATE-1", status: "on_time" }),
      truckFixture({ id: "TRK-2", plateNumber: "PLATE-2", status: "delayed" }),
      truckFixture({ id: "TRK-3", plateNumber: "PLATE-3", status: "delayed" }),
    ]);
    render(<FleetTracking />);
    await screen.findByText("PLATE-1");
    expect(rowFor("PLATE-2")).toHaveTextContent("Delayed");
    fireEvent.click(screen.getByRole("button", { name: /Delayed\s*2/ }));
    await waitFor(() => expect(screen.queryByText("PLATE-1")).toBeNull());
    expect(screen.getByText("PLATE-3")).toBeInTheDocument();
  });

  it("applies focus once and does not hijack a later manual row click", async () => {
    seedFleet([
      truckFixture({ id: "TRK-1", plateNumber: "PLATE-1" }),
      truckFixture({ id: "TRK-2", plateNumber: "PLATE-2", name: "Truck Two" }),
    ]);
    const onTruckSelect = jest.fn();

    render(
      <FleetTracking onTruckSelect={onTruckSelect} focusAssetId="TRK-2" />,
    );
    await waitFor(() => expect(onTruckSelect).toHaveBeenCalled());

    fireEvent.click(rowFor("PLATE-1"));

    await waitFor(() => expect(rowFor("PLATE-1")).toHaveClass("bg-info-light"));
    expect(rowFor("PLATE-2")).not.toHaveClass("bg-info-light");
    expect(onTruckSelect).toHaveBeenLastCalledWith(
      expect.objectContaining({ id: "TRK-1" }),
    );
  });

  it("shows a dismissible notice when the referenced asset is not in this view", async () => {
    seedFleet([truckFixture({ id: "TRK-1", plateNumber: "PLATE-1" })]);
    const onTruckSelect = jest.fn();

    render(
      <FleetTracking onTruckSelect={onTruckSelect} focusAssetId="VESSEL-9" />,
    );

    const notice = await screen.findByTestId("focus-asset-not-found");
    expect(notice).toHaveTextContent("VESSEL-9");
    expect(onTruckSelect).not.toHaveBeenCalled();

    fireEvent.click(
      screen.getByRole("button", { name: /dismiss asset not found notice/i }),
    );
    expect(
      screen.queryByTestId("focus-asset-not-found"),
    ).not.toBeInTheDocument();
  });

  it("does not select anything when no asset is referenced", async () => {
    seedFleet([truckFixture({ id: "TRK-1", plateNumber: "PLATE-1" })]);
    const onTruckSelect = jest.fn();

    render(<FleetTracking onTruckSelect={onTruckSelect} />);

    expect(await screen.findByText("PLATE-1")).toBeInTheDocument();
    expect(onTruckSelect).not.toHaveBeenCalled();
    expect(
      screen.queryByTestId("focus-asset-not-found"),
    ).not.toBeInTheDocument();
  });
});

describe("FleetTracking — Add asset FormDialog and Compartments drawer", () => {
  const mockCreate = apiService.createAsset as jest.MockedFunction<
    typeof apiService.createAsset
  >;

  it("validates, posts the asset with its plate number and reloads", async () => {
    seedFleet([truckFixture()]);
    mockCreate.mockResolvedValue({
      data: truckFixture({ id: "QA-TNK-9" }),
      success: true,
      timestamp: "",
    } as never);
    render(<FleetTracking />);
    await screen.findByText("PLATE-1");
    fireEvent.click(screen.getByRole("button", { name: "Add asset" }));
    const dialog = await screen.findByRole("dialog", { name: "Add asset" });
    fireEvent.click(screen.getByRole("button", { name: "Create asset" }));
    expect(await screen.findByText("Enter an asset ID.")).toBeInTheDocument();
    expect(screen.getByText("Enter the plate number.")).toBeInTheDocument();
    expect(mockCreate).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText(/Asset ID/), {
      target: { value: "QA-TNK-9" },
    });
    fireEvent.change(screen.getByLabelText(/^Name/), {
      target: { value: "QA Tanker" },
    });
    fireEvent.change(screen.getByLabelText(/Plate number/), {
      target: { value: "QA-123" },
    });
    fireEvent.change(screen.getByLabelText(/Latitude/), {
      target: { value: "29.76043" },
    });
    fireEvent.change(screen.getByLabelText(/Longitude/), {
      target: { value: "-95.3698" },
    });
    const loadsBefore = mockGetTrucks.mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: "Create asset" }));
    await waitFor(() =>
      expect(mockCreate).toHaveBeenCalledWith(
        expect.objectContaining({
          asset_id: "QA-TNK-9",
          name: "QA Tanker",
          asset_type: "vehicle",
          asset_subtype: "fuel_truck",
          plate_number: "QA-123",
          current_location: expect.objectContaining({
            coordinates: { lat: 29.76043, lon: -95.3698 },
          }),
        }),
      ),
    );
    await waitFor(() => expect(dialog).not.toBeInTheDocument());
    await waitFor(() =>
      expect(mockGetTrucks.mock.calls.length).toBeGreaterThan(loadsBefore),
    );
  });

  it("keeps the dialog open with the API message on failure", async () => {
    seedFleet([truckFixture()]);
    mockCreate.mockRejectedValue(new Error("Asset QA-TNK-9 already exists"));
    render(<FleetTracking />);
    await screen.findByText("PLATE-1");
    fireEvent.click(screen.getByRole("button", { name: "Add asset" }));
    await screen.findByRole("dialog", { name: "Add asset" });
    fireEvent.change(screen.getByLabelText(/Asset ID/), {
      target: { value: "QA-TNK-9" },
    });
    fireEvent.change(screen.getByLabelText(/^Name/), {
      target: { value: "QA Tanker" },
    });
    fireEvent.change(screen.getByLabelText(/Plate number/), {
      target: { value: "QA-123" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create asset" }));
    expect(
      await screen.findByText("Asset QA-TNK-9 already exists"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("dialog", { name: "Add asset" }),
    ).toBeInTheDocument();
  });

  it("opens Compartments in a drawer from the row menu", async () => {
    seedFleet([truckFixture()]);
    render(<FleetTracking />);
    await screen.findByText("PLATE-1");
    fireEvent.click(
      screen.getByRole("button", { name: "Actions for PLATE-1" }),
    );
    fireEvent.click(screen.getByRole("menuitem", { name: "Compartments" }));
    expect(
      await screen.findByRole("dialog", { name: "Compartments · PLATE-1" }),
    ).toBeInTheDocument();
  });
});
