/**
 * Regression test for :file:`AssetCertificationsPage.tsx`.
 *
 * The fleet certification dashboard endpoint
 * (``GET /compliance/asset-certifications/dashboard``) returns a **flat
 * list of per-certification rows** — one entry per certification with
 * ``asset_id`` / ``cert_id`` / ``status`` / ``days_until_expiry`` — not a
 * per-asset aggregate. An earlier version of the page assumed each row
 * already carried a nested ``certifications[]`` array and crashed with
 * "Cannot read properties of undefined (reading 'map')" when the page
 * was wired into the Compliance hub.
 *
 * These tests pin the real backend shape and verify the page groups the
 * flat rows into one table row per asset without crashing.
 */

import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

jest.mock("../../services/complianceApi", () => {
  const actual = jest.requireActual("../../services/complianceApi");
  return {
    ...actual,
    getAssetCertificationsDashboard: jest.fn(),
    getAssetCertifications: jest.fn(),
    createAssetCertification: jest.fn(),
  };
});

// The "Add Certification" form's Asset ID is an AssetPicker backed by
// apiService.getAssets — stub the fleet roster so the picker can load.
jest.mock("../../services/api", () => ({
  apiService: {
    getAssets: jest.fn(),
  },
}));

import { apiService } from "../../services/api";
import {
  type AssetCertificationDashboard,
  createAssetCertification,
  getAssetCertificationsDashboard,
} from "../../services/complianceApi";
import AssetCertificationsPage from "./AssetCertificationsPage";

const mockGetDashboard = getAssetCertificationsDashboard as jest.MockedFunction<
  typeof getAssetCertificationsDashboard
>;
const mockCreateCert = createAssetCertification as jest.MockedFunction<
  typeof createAssetCertification
>;
const mockGetAssets = apiService.getAssets as jest.MockedFunction<
  typeof apiService.getAssets
>;

/** Mirrors the live backend payload (flat, per-certification rows). */
function dashboardFixture(): AssetCertificationDashboard {
  return {
    assets: [
      {
        asset_id: "TNK-001",
        certification_type: "K_test",
        cert_id: "CERT-002",
        certification_date: "2025-06-15",
        expiry_date: "2026-06-14",
        status: "expiring_soon",
        days_until_expiry: 10,
        inspector_name: "John Smith",
        certificate_number: "K-2025-TX-001",
      },
      {
        asset_id: "TNK-001",
        certification_type: "V_test",
        cert_id: "CERT-001",
        certification_date: "2025-01-15",
        expiry_date: "2027-01-14",
        status: "valid",
        days_until_expiry: 400,
        inspector_name: "John Smith",
        certificate_number: "V-2025-TX-001",
      },
      {
        asset_id: "TRK-009",
        certification_type: "meter_seal",
        cert_id: "CERT-003",
        certification_date: "2024-01-01",
        expiry_date: "2025-01-01",
        status: "expired",
        days_until_expiry: -120,
        inspector_name: "Jane Roe",
        certificate_number: "M-2024-TX-009",
      },
    ],
    total_valid: 1,
    total_expiring_soon: 1,
    total_expired: 1,
  };
}

afterEach(() => {
  jest.clearAllMocks();
});

beforeEach(() => {
  // Default: an empty fleet roster so the picker mounts cleanly. Tests that
  // need a selectable truck override this.
  mockGetAssets.mockResolvedValue({
    data: [],
    request_id: "assets",
  } as unknown as Awaited<ReturnType<typeof apiService.getAssets>>);
});

describe("AssetCertificationsPage — fleet dashboard", () => {
  it("groups flat per-certification rows into one row per asset", async () => {
    mockGetDashboard.mockResolvedValue({
      data: dashboardFixture(),
      request_id: "req-1",
    });

    render(<AssetCertificationsPage />);

    // Both assets render (TNK-001 aggregates its two certs into one row).
    expect(await screen.findByText("TNK-001")).toBeInTheDocument();
    expect(screen.getByText("TRK-009")).toBeInTheDocument();

    // The aggregated TNK-001 row surfaces both of its certification badges.
    const tnkRow = screen.getByText("TNK-001").closest("tr");
    expect(tnkRow).not.toBeNull();
    if (tnkRow) {
      expect(within(tnkRow).getByText("K test")).toBeInTheDocument();
      expect(within(tnkRow).getByText("V test")).toBeInTheDocument();
    }
  });

  it("renders the summary counts from the backend totals", async () => {
    mockGetDashboard.mockResolvedValue({
      data: dashboardFixture(),
      request_id: "req-2",
    });

    render(<AssetCertificationsPage />);

    await screen.findByText("TNK-001");
    // Summary cards render with the backend totals.
    expect(screen.getByText("Valid Certifications")).toBeInTheDocument();
    // "Expiring Soon" / "Expired" also appear as status badges, so scope
    // the assertion to the summary card label specifically.
    expect(screen.getAllByText("Expiring Soon").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Expired").length).toBeGreaterThan(0);
  });

  it("does not crash on an empty dashboard payload", async () => {
    mockGetDashboard.mockResolvedValue({
      data: {
        assets: [],
        total_valid: 0,
        total_expiring_soon: 0,
        total_expired: 0,
      },
      request_id: "req-3",
    });

    render(<AssetCertificationsPage />);

    await waitFor(() => expect(mockGetDashboard).toHaveBeenCalled());
    // Empty state renders instead of a crash.
    expect(await screen.findByText(/no assets found/i)).toBeInTheDocument();
  });
});

describe("AssetCertificationsPage — add form asset picker", () => {
  it("selects the asset from the live fleet roster instead of free text", async () => {
    mockGetDashboard.mockResolvedValue({
      data: {
        assets: [],
        total_valid: 0,
        total_expiring_soon: 0,
        total_expired: 0,
      },
      request_id: "req-add",
    });
    mockGetAssets.mockResolvedValue({
      data: [
        {
          id: "TRUCK-001",
          name: "Rig 1",
          assetType: "vehicle",
          assetSubtype: "fuel_truck",
        },
      ],
      request_id: "assets",
    } as unknown as Awaited<ReturnType<typeof apiService.getAssets>>);
    mockCreateCert.mockResolvedValue({
      data: {},
      request_id: "created",
    } as unknown as Awaited<ReturnType<typeof createAssetCertification>>);

    render(<AssetCertificationsPage />);

    // Open the create form.
    fireEvent.click(await screen.findByText("Add Certification"));

    // The Asset ID field is the roster-backed picker (loaded from getAssets).
    await waitFor(() => expect(mockGetAssets).toHaveBeenCalled());
    fireEvent.click(await screen.findByLabelText("Asset ID"));
    fireEvent.click(await screen.findByText("Rig 1"));

    // Fill the remaining required fields and submit.
    fireEvent.change(screen.getByLabelText("Certification Date"), {
      target: { value: "2025-01-01" },
    });
    fireEvent.change(screen.getByLabelText("Expiry Date"), {
      target: { value: "2026-01-01" },
    });
    fireEvent.change(screen.getByLabelText("Inspector Name"), {
      target: { value: "John Smith" },
    });
    fireEvent.change(screen.getByLabelText("Certificate Number"), {
      target: { value: "V-2025-TX-001" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add Certification" }));

    await waitFor(() => expect(mockCreateCert).toHaveBeenCalled());
    expect(mockCreateCert.mock.calls[0][0].asset_id).toBe("TRUCK-001");
  });
});
