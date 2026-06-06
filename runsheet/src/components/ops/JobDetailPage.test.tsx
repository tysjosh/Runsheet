/**
 * Tests for JobDetailPage cross-module linkage (cross-module-entity-linkage
 * task 3.4):
 * - Linked order / customer rendered as navigation from the resolver `links`
 *   payload (Req 13.1).
 * - Unresolved references render an explicit "Unlinked" affordance (Req 13.3).
 * - The reassign control is an asset picker backed by /fleet/assets that only
 *   offers type-compatible assets — not a free-text id box (Req 3.3).
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { Job } from "../../types/api";

// Render next/link as a plain anchor so we can assert on hrefs.
jest.mock("next/link", () => ({
  __esModule: true,
  default: ({
    href,
    children,
    ...rest
  }: {
    href: string;
    children: React.ReactNode;
  }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));

jest.mock("../../services/schedulingApi", () => ({
  getJob: jest.fn(),
  getCargo: jest.fn(),
  getJobEta: jest.fn(),
  reassignAsset: jest.fn(),
  transitionStatus: jest.fn(),
}));

jest.mock("../../services/api", () => ({
  apiService: { getAssets: jest.fn() },
}));

import { apiService } from "../../services/api";
import {
  getCargo,
  getJob,
  getJobEta,
  reassignAsset,
} from "../../services/schedulingApi";

const mockGetJob = getJob as jest.MockedFunction<typeof getJob>;
const mockGetCargo = getCargo as jest.MockedFunction<typeof getCargo>;
const mockGetJobEta = getJobEta as jest.MockedFunction<typeof getJobEta>;
const mockReassign = reassignAsset as jest.MockedFunction<typeof reassignAsset>;
const mockGetAssets = apiService.getAssets as jest.MockedFunction<
  typeof apiService.getAssets
>;

import JobDetailPage from "./JobDetailPage";

function jobFixture(overrides: Partial<Job> = {}): Job {
  return {
    job_id: "JOB-1",
    job_type: "cargo_transport",
    status: "assigned",
    tenant_id: "tenant-1",
    asset_assigned: "TRK-001",
    order_id: "ORD-9",
    customer_id: "CUST-7",
    driver_id: "DRV-3",
    origin: "Depot A",
    destination: "Site B",
    scheduled_time: "2026-01-01T08:00:00Z",
    created_at: "2026-01-01T07:00:00Z",
    updated_at: "2026-01-01T07:30:00Z",
    priority: "normal",
    delayed: false,
    ...overrides,
  };
}

function renderPage() {
  return render(
    <JobDetailPage jobId="JOB-1" onBack={jest.fn()} onTransition={jest.fn()} />,
  );
}

beforeEach(() => {
  mockGetJob.mockReset();
  mockGetCargo.mockReset();
  mockGetJobEta.mockReset();
  mockReassign.mockReset();
  mockGetAssets.mockReset();
  mockGetCargo.mockResolvedValue({ data: [], request_id: "c" });
  mockGetJobEta.mockRejectedValue(new Error("no eta"));
  mockGetAssets.mockResolvedValue({ data: [], success: true } as never);
});

describe("JobDetailPage — linked records", () => {
  it("renders resolved order and customer links from the resolver payload", async () => {
    mockGetJob.mockResolvedValue({
      data: {
        ...jobFixture(),
        links: {
          order: { status: "resolved", id: "ORD-9", summary: {} },
          customer: {
            status: "resolved",
            id: "CUST-7",
            summary: { display_name: "Acme Fuels" },
          },
        },
      },
      request_id: "j",
    } as never);

    renderPage();

    await waitFor(() => expect(mockGetJob).toHaveBeenCalled());

    // Order link → /orders/ORD-9
    const orderLink = await screen.findByRole("link", { name: /ORD-9/ });
    expect(orderLink).toHaveAttribute("href", "/orders/ORD-9");

    // Customer link shows resolved display name → /commerce/customers/CUST-7
    const customerLink = screen.getByRole("link", { name: /Acme Fuels/ });
    expect(customerLink).toHaveAttribute("href", "/commerce/customers/CUST-7");
  });

  it("requests the resolver expand for order/customer/asset/driver", async () => {
    mockGetJob.mockResolvedValue({
      data: { ...jobFixture(), links: {} },
      request_id: "j",
    } as never);

    renderPage();

    await waitFor(() =>
      expect(mockGetJob).toHaveBeenCalledWith("JOB-1", {
        expand: ["order", "customer", "asset", "driver"],
      }),
    );
  });

  it("shows an Unlinked affordance for an unresolved reference", async () => {
    mockGetJob.mockResolvedValue({
      data: {
        ...jobFixture({ customer_id: "CUST-MISSING" }),
        links: {
          customer: { status: "unresolved", id: "CUST-MISSING" },
        },
      },
      request_id: "j",
    } as never);

    renderPage();

    await waitFor(() => expect(mockGetJob).toHaveBeenCalled());
    expect(await screen.findByText("Unlinked")).toBeInTheDocument();
  });
});

describe("JobDetailPage — asset reassign picker", () => {
  it("offers a type-compatible asset picker (not a free-text box)", async () => {
    mockGetJob.mockResolvedValue({
      data: { ...jobFixture(), links: {} },
      request_id: "j",
    } as never);
    mockGetAssets.mockResolvedValue({
      data: [
        {
          id: "TRK-005",
          name: "Tanker 5",
          assetType: "vehicle",
          status: "available",
        },
      ],
      success: true,
    } as never);

    renderPage();
    await waitFor(() => expect(mockGetJob).toHaveBeenCalled());

    // Open the reassign form (wait for the loaded job to render the button).
    fireEvent.click(await screen.findByRole("button", { name: /reassign/i }));

    // The picker is a select, loaded with the compatible vehicle asset.
    const select = await screen.findByLabelText(/select replacement asset/i);
    expect(select.tagName).toBe("SELECT");

    // getAssets was filtered to the compatible asset type for this job.
    await waitFor(() =>
      expect(mockGetAssets).toHaveBeenCalledWith({ asset_type: "vehicle" }),
    );
    expect(await screen.findByText(/Tanker 5 \(TRK-005\)/)).toBeInTheDocument();

    // No free-text "New asset ID" input remains.
    expect(
      screen.queryByPlaceholderText(/new asset id/i),
    ).not.toBeInTheDocument();
  });

  it("reassigns the job to the picked asset id", async () => {
    mockGetJob.mockResolvedValue({
      data: { ...jobFixture(), links: {} },
      request_id: "j",
    } as never);
    mockGetAssets.mockResolvedValue({
      data: [{ id: "TRK-005", name: "Tanker 5", assetType: "vehicle" }],
      success: true,
    } as never);
    mockReassign.mockResolvedValue({
      data: jobFixture({ asset_assigned: "TRK-005" }),
      request_id: "r",
    } as never);

    renderPage();
    await waitFor(() => expect(mockGetJob).toHaveBeenCalled());

    fireEvent.click(await screen.findByRole("button", { name: /reassign/i }));
    const select = await screen.findByLabelText(/select replacement asset/i);
    await screen.findByText(/Tanker 5/);

    fireEvent.change(select, { target: { value: "TRK-005" } });
    fireEvent.click(screen.getByRole("button", { name: /confirm/i }));

    await waitFor(() =>
      expect(mockReassign).toHaveBeenCalledWith("JOB-1", "TRK-005"),
    );
  });
});
