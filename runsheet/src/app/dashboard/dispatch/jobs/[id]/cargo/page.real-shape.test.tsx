/**
 * R-1 regression: the cargo page with the real backend envelope.
 *
 * GET /scheduling/jobs/{id} returns ``{ data: { job, events } }``. The page
 * used to store ``data`` as the job, so ``job.status`` was undefined and the
 * status formatter threw. This test does not mock ``schedulingApi``; it mocks
 * ``fetch`` with the real wire shape so the service-layer unwrap is exercised
 * end to end.
 */
import { render, screen } from "@testing-library/react";

jest.mock("next/navigation", () => ({
  useParams: () => ({ id: "JOB_6" }),
  useRouter: () => ({ push: jest.fn(), back: jest.fn() }),
}));
jest.mock("../../../../../../hooks/useSchedulingWebSocket", () => ({
  useSchedulingWebSocket: jest.fn(),
}));
jest.mock("../../../../../../components/ops/CargoManifestEditor", () => ({
  __esModule: true,
  default: (props: { items: unknown[] }) => (
    <div data-testid="cargo-editor">{props.items.length} items</div>
  ),
}));

import CargoTrackingPage from "./page";

const job = {
  job_id: "JOB_6",
  job_type: "fuel_delivery",
  status: "in_progress",
  origin: "Depot A",
  destination: "Station 7",
  asset_assigned: "TRUCK_01",
  scheduled_time: "2026-10-04T19:00:00Z",
  tenant_id: "demo-tenant",
};

function jsonResponse(body: unknown) {
  return { ok: true, status: 200, json: async () => body };
}

describe("CargoTrackingPage with the real job envelope (R-1)", () => {
  beforeEach(() => {
    global.fetch = jest.fn(async (url: string) => {
      if (url.endsWith("/scheduling/jobs/JOB_6/cargo")) {
        return jsonResponse({ data: [{ item_id: "I-1" }], request_id: "c" });
      }
      if (url.endsWith("/scheduling/jobs/JOB_6")) {
        return jsonResponse({
          data: { job, events: [{ event_id: "E-1" }] },
          request_id: "j",
        });
      }
      throw new Error(`unexpected fetch ${url}`);
    }) as unknown as typeof fetch;
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it("renders the job header and editor instead of crashing", async () => {
    render(<CargoTrackingPage />);

    expect(await screen.findByTestId("cargo-editor")).toHaveTextContent(
      "1 items",
    );
    expect(screen.getByText("In Progress")).toBeInTheDocument();
    expect(screen.getByText("Depot A")).toBeInTheDocument();
    expect(screen.getByText("Station 7")).toBeInTheDocument();
    expect(screen.getByText("TRUCK_01")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
