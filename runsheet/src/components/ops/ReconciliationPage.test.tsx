/**
 * Component + helper tests for the reconciliation dashboard page (Task 11.5).
 *
 * Coverage is intentionally focused on the behaviors the spec calls out:
 *
 *  1. **Pure helpers** — variance formatters, row-alert decision, and
 *     cell-color mapping are exercised directly so the UI tests can
 *     stay simple.
 *  2. **4-way variance table rendering** — rows with variances beyond
 *     the threshold render with the ``bg-error-light`` row class (alert
 *     highlighting, Req 4.4.3 visualization) and safe rows do not.
 *  3. **BOL download link from POD detail** — opening a row fetches
 *     the BOL via :func:`getPodBol`; the ``generated`` state exposes a
 *     download link (Req 4.3.4) while ``pending_regeneration`` and the
 *     404 / not-found path surface inline status chips instead.
 *
 * All backend calls are mocked — these tests must not depend on an ES
 * instance or a running FastAPI server.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";

jest.mock("../../services/fuelApi", () => {
  const actual = jest.requireActual("../../services/fuelApi");
  return {
    ...actual,
    listReconciliationRecords: jest.fn(),
    getPodBol: jest.fn(),
    getPodHashProof: jest.fn(),
    verifyPodHashChain: jest.fn(),
  };
});

import type {
  BOLDownloadResponse,
  HashChainVerifyResponse,
  HashProofResponse,
  ReconciliationListResponse,
  ReconciliationRecord,
} from "../../services/fuelApi";
import {
  getPodBol,
  getPodHashProof,
  listReconciliationRecords,
  verifyPodHashChain,
} from "../../services/fuelApi";
import ReconciliationPage, {
  formatGallons,
  formatVariancePct,
  isAlertedRow,
  varianceCellClass,
} from "./ReconciliationPage";

const mockList = listReconciliationRecords as jest.MockedFunction<
  typeof listReconciliationRecords
>;
const mockBol = getPodBol as jest.MockedFunction<typeof getPodBol>;
const mockHashProof = getPodHashProof as jest.MockedFunction<
  typeof getPodHashProof
>;
const mockVerifyChain = verifyPodHashChain as jest.MockedFunction<
  typeof verifyPodHashChain
>;

// ─── Fixtures ────────────────────────────────────────────────────────────────

function makeRecord(
  overrides: Partial<ReconciliationRecord> = {},
): ReconciliationRecord {
  return {
    reconciliation_id: "rec-1",
    tenant_id: "t-1",
    order_id: "ord-1",
    plan_id: "plan-1",
    pod_id: "pod-1",
    invoice_id: null,
    ordered_gallons: 1000,
    loaded_gallons: 990,
    delivered_gallons: 980,
    invoiced_gallons: null,
    variance_load_vs_order_pct: 1.0,
    variance_delivered_vs_loaded_pct: 1.01,
    variance_invoiced_vs_delivered_pct: null,
    alert_flags: [],
    generated_at: "2024-06-01T12:00:00Z",
    ...overrides,
  };
}

function makeListResponse(
  items: ReconciliationRecord[],
  overrides: Partial<ReconciliationListResponse> = {},
): ReconciliationListResponse {
  return {
    items,
    total: items.length,
    page: 1,
    page_size: 25,
    has_next: false,
    ...overrides,
  };
}

beforeEach(() => {
  jest.clearAllMocks();
});

// ─── Pure helper tests ───────────────────────────────────────────────────────

describe("formatGallons", () => {
  it("renders an em-dash for null / NaN", () => {
    expect(formatGallons(null)).toBe("—");
    expect(formatGallons(undefined)).toBe("—");
    expect(formatGallons(Number.NaN)).toBe("—");
  });

  it("compacts values ≥ 10,000 to the 'K' suffix", () => {
    expect(formatGallons(12_345)).toBe("12.3K");
  });

  it("renders small values with at most one decimal", () => {
    expect(formatGallons(987.654)).toBe("987.7");
    expect(formatGallons(100)).toBe("100");
  });
});

describe("formatVariancePct", () => {
  it("renders em-dash for null / NaN", () => {
    expect(formatVariancePct(null)).toBe("—");
    expect(formatVariancePct(undefined)).toBe("—");
    expect(formatVariancePct(Number.NaN)).toBe("—");
  });

  it("formats to two decimals with a % suffix", () => {
    expect(formatVariancePct(3.2)).toBe("3.20%");
    expect(formatVariancePct(0)).toBe("0.00%");
  });
});

describe("varianceCellClass", () => {
  it("returns neutral color for null / NaN", () => {
    expect(varianceCellClass(null)).toContain("text-gray-400");
    expect(varianceCellClass(undefined)).toContain("text-gray-400");
  });

  it("returns red styling at or above threshold", () => {
    expect(varianceCellClass(3.0)).toContain("text-error-dark");
    expect(varianceCellClass(-5.0)).toContain("text-error-dark");
  });

  it("returns yellow styling between half and full threshold", () => {
    expect(varianceCellClass(1.6)).toContain("text-warning-dark");
  });

  it("returns default styling for small variances", () => {
    expect(varianceCellClass(0.5)).toContain("text-gray-700");
  });
});

describe("isAlertedRow", () => {
  it("returns true when variance_exceeds_threshold is in alert_flags", () => {
    const record = makeRecord({
      variance_load_vs_order_pct: 0.1,
      variance_delivered_vs_loaded_pct: 0.1,
      alert_flags: ["variance_exceeds_threshold"],
    });
    expect(isAlertedRow(record)).toBe(true);
  });

  it("returns true when any variance crosses the default threshold", () => {
    const record = makeRecord({
      variance_load_vs_order_pct: 0.1,
      variance_delivered_vs_loaded_pct: 0.1,
      variance_invoiced_vs_delivered_pct: 4.2,
    });
    expect(isAlertedRow(record)).toBe(true);
  });

  it("returns false for clean rows", () => {
    const record = makeRecord({
      variance_load_vs_order_pct: 0.1,
      variance_delivered_vs_loaded_pct: 0.2,
      variance_invoiced_vs_delivered_pct: null,
      alert_flags: [],
    });
    expect(isAlertedRow(record)).toBe(false);
  });
});

// ─── Page rendering tests ────────────────────────────────────────────────────

describe("ReconciliationPage", () => {
  it("renders rows returned by listReconciliationRecords", async () => {
    const records = [
      makeRecord({ reconciliation_id: "rec-1", pod_id: "pod-1" }),
      makeRecord({ reconciliation_id: "rec-2", pod_id: "pod-2" }),
    ];
    mockList.mockResolvedValue(makeListResponse(records));

    render(<ReconciliationPage />);

    await waitFor(() => expect(mockList).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("pod-1")).toBeInTheDocument();
    expect(screen.getByText("pod-2")).toBeInTheDocument();
  });

  it("highlights rows where any variance crosses the threshold", async () => {
    const highVariance = makeRecord({
      reconciliation_id: "rec-high",
      pod_id: "pod-high",
      variance_delivered_vs_loaded_pct: 4.5,
    });
    const clean = makeRecord({
      reconciliation_id: "rec-clean",
      pod_id: "pod-clean",
      variance_load_vs_order_pct: 0.1,
      variance_delivered_vs_loaded_pct: 0.2,
    });
    mockList.mockResolvedValue(makeListResponse([highVariance, clean]));

    const { container } = render(<ReconciliationPage />);

    await waitFor(() => expect(mockList).toHaveBeenCalled());
    const highRow = await waitFor(
      () =>
        container.querySelector(
          '[data-testid="reconciliation-row-rec-high"]',
        ) as HTMLTableRowElement | null,
    );
    const cleanRow = container.querySelector(
      '[data-testid="reconciliation-row-rec-clean"]',
    ) as HTMLTableRowElement | null;

    expect(highRow).not.toBeNull();
    expect(cleanRow).not.toBeNull();
    expect(highRow?.className).toContain("bg-error-light");
    expect(cleanRow?.className).not.toContain("bg-error-light");
  });

  it("renders an empty-state message when no records come back", async () => {
    mockList.mockResolvedValue(makeListResponse([]));

    render(<ReconciliationPage />);

    expect(
      await screen.findByText(
        /no reconciliation records match the current filters/i,
      ),
    ).toBeInTheDocument();
  });

  it("shows a BOL download link in the POD drawer when the BOL is generated", async () => {
    const record = makeRecord({
      reconciliation_id: "rec-bol",
      pod_id: "pod-bol",
    });
    mockList.mockResolvedValue(makeListResponse([record]));

    const bol: BOLDownloadResponse = {
      bol_id: "bol-1",
      pod_id: "pod-bol",
      status: "generated",
      hash: "abc123",
      generated_at: "2024-06-01T12:05:00Z",
      file_ref: "tenants/t/bol/2024/06/01/xyz.pdf",
      download_url: "https://s3.example.com/bol-signed-url",
      expires_at: "2024-06-01T12:20:00Z",
      tenant_id: "t-1",
    };
    mockBol.mockResolvedValue(bol);

    render(<ReconciliationPage />);
    const openButton = await screen.findByRole("button", {
      name: /open pod pod-bol/i,
    });
    fireEvent.click(openButton);

    await waitFor(() => expect(mockBol).toHaveBeenCalledWith("pod-bol"));

    const link = (await screen.findByRole("link", {
      name: /download bol pdf for pod pod-bol/i,
    })) as HTMLAnchorElement;
    expect(link.href).toBe("https://s3.example.com/bol-signed-url");
    expect(link.target).toBe("_blank");
  });

  it("renders the pending-regeneration state instead of a download link", async () => {
    const record = makeRecord({ pod_id: "pod-pending" });
    mockList.mockResolvedValue(makeListResponse([record]));
    mockBol.mockResolvedValue({
      bol_id: "bol-pending",
      pod_id: "pod-pending",
      status: "pending_regeneration",
      hash: "",
      generated_at: null,
      file_ref: null,
      download_url: null,
      expires_at: null,
      tenant_id: "t-1",
    });

    render(<ReconciliationPage />);
    const openButton = await screen.findByRole("button", {
      name: /open pod pod-pending/i,
    });
    fireEvent.click(openButton);

    expect(
      await screen.findByText(/BOL is queued for regeneration/i),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: /download bol pdf/i }),
    ).not.toBeInTheDocument();
  });

  it("renders the not-found state when the POD has no BOL", async () => {
    const record = makeRecord({ pod_id: "pod-missing" });
    mockList.mockResolvedValue(makeListResponse([record]));
    mockBol.mockRejectedValue(new Error("bol_not_found: no BOL record exists"));

    render(<ReconciliationPage />);
    const openButton = await screen.findByRole("button", {
      name: /open pod pod-missing/i,
    });
    fireEvent.click(openButton);

    expect(
      await screen.findByText(/no bol has been generated for this pod/i),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: /download bol pdf/i }),
    ).not.toBeInTheDocument();
  });
});

// ─── Tamper evidence (hash-proof + chain verify) ─────────────────────────────

async function openTamperEvidence(podId: string) {
  const openButton = await screen.findByRole("button", {
    name: new RegExp(`open pod ${podId}`, "i"),
  });
  fireEvent.click(openButton);
  const tamperToggle = await screen.findByRole("button", {
    name: /tamper evidence/i,
  });
  fireEvent.click(tamperToggle);
}

describe("ReconciliationPage — tamper evidence panel", () => {
  beforeEach(() => {
    // BOL fetch is triggered on drawer open; satisfy the Promise so
    // the tamper-evidence tests don't leak unhandled rejections.
    mockBol.mockResolvedValue({
      bol_id: "bol-x",
      pod_id: "pod-tamper",
      status: "generated",
      hash: "h",
      generated_at: "2024-06-01T12:05:00Z",
      file_ref: "tenants/t/bol/2024/06/01/xyz.pdf",
      download_url: "https://s3.example.com/x",
      expires_at: "2024-06-01T12:20:00Z",
      tenant_id: "t-1",
    });
  });

  it("calls getPodHashProof with the drawer pod_id and renders hashes", async () => {
    const record = makeRecord({ pod_id: "pod-tamper" });
    mockList.mockResolvedValue(makeListResponse([record]));
    const proof: HashProofResponse = {
      pod_id: "pod-tamper",
      tenant_id: "t-1",
      pod_hash: "hash-current-abcdef",
      previous_pod_hash: "hash-prev-012345",
      canonical_payload: {
        pod_id: "pod-tamper",
        chain_sequence: 7,
        delivered_gallons: 500,
      },
      canonical_payload_bytes: '{"pod_id":"pod-tamper"}',
    };
    mockHashProof.mockResolvedValue(proof);

    render(<ReconciliationPage />);
    await openTamperEvidence("pod-tamper");

    fireEvent.click(screen.getByRole("button", { name: /show hash proof/i }));

    await waitFor(() =>
      expect(mockHashProof).toHaveBeenCalledWith("pod-tamper"),
    );
    expect(await screen.findByTestId("tamper-pod-hash")).toHaveTextContent(
      "hash-current-abcdef",
    );
    expect(screen.getByTestId("tamper-previous-pod-hash")).toHaveTextContent(
      "hash-prev-012345",
    );
  });

  it("verifies the chain with the default single-POD range", async () => {
    const record = makeRecord({ pod_id: "pod-chain" });
    mockList.mockResolvedValue(makeListResponse([record]));
    const verify: HashChainVerifyResponse = {
      tenant_id: "t-1",
      verified_count: 1,
      total_requested: 1,
      valid: true,
      first_mismatch: null,
      pod_ids_checked: ["pod-chain"],
    };
    mockVerifyChain.mockResolvedValue(verify);

    render(<ReconciliationPage />);
    await openTamperEvidence("pod-chain");

    fireEvent.click(screen.getByRole("button", { name: /verify chain/i }));
    // The form exposes from_pod_id prefilled with the drawer pod_id.
    expect(
      (screen.getByLabelText(/from_pod_id/i) as HTMLInputElement).value,
    ).toBe("pod-chain");
    fireEvent.click(screen.getByRole("button", { name: /^verify$/i }));

    await waitFor(() =>
      expect(mockVerifyChain).toHaveBeenCalledWith({
        from_pod_id: "pod-chain",
      }),
    );
    expect(await screen.findByTestId("chain-intact-badge")).toHaveTextContent(
      /chain intact/i,
    );
    expect(screen.getByTestId("chain-intact-badge")).toHaveTextContent(
      /1 pod verified/i,
    );
  });

  it("renders the first-mismatch card when valid is false", async () => {
    const record = makeRecord({ pod_id: "pod-bad" });
    mockList.mockResolvedValue(makeListResponse([record]));
    const verify: HashChainVerifyResponse = {
      tenant_id: "t-1",
      verified_count: 2,
      total_requested: 3,
      valid: false,
      first_mismatch: {
        pod_id: "pod-bad-3",
        reason: "stored_hash_mismatch",
        expected_hash: "expected-abc",
        stored_hash: "actual-xyz",
        computed_hash: "computed-xyz",
        message: "stored hash differs from recomputed hash",
      },
      pod_ids_checked: ["pod-bad-1", "pod-bad-2", "pod-bad-3"],
    };
    mockVerifyChain.mockResolvedValue(verify);

    render(<ReconciliationPage />);
    await openTamperEvidence("pod-bad");

    fireEvent.click(screen.getByRole("button", { name: /verify chain/i }));
    fireEvent.click(screen.getByRole("button", { name: /^verify$/i }));

    expect(await screen.findByTestId("chain-mismatch-card")).toHaveTextContent(
      /tamper detected/i,
    );
    expect(screen.getByTestId("mismatch-pod-id")).toHaveTextContent(
      "pod-bad-3",
    );
    expect(screen.getByTestId("mismatch-expected-hash")).toHaveTextContent(
      "expected-abc",
    );
    expect(screen.getByTestId("mismatch-actual-hash")).toHaveTextContent(
      "actual-xyz",
    );
  });
});
