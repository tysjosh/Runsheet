/**
 * Tests for AccountDetailPage component.
 *
 * Covers:
 * - Happy path: account + aging data render
 * - Credit override drawer open/close
 * - Credit override submission
 * - Expire override action
 * - Aging bucket cards display
 * - Error state rendering
 * - Loading state rendering
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";

jest.mock("../../../services/commerceApi", () => ({
  getAccount: jest.fn(),
  getAccountAging: jest.fn(),
  applyCreditOverride: jest.fn(),
  deleteCreditOverride: jest.fn(),
}));

import {
  getAccount,
  getAccountAging,
  applyCreditOverride,
  deleteCreditOverride,
} from "../../../services/commerceApi";
import AccountDetailPage from "../AccountDetailPage";

const mockGetAccount = getAccount as jest.MockedFunction<typeof getAccount>;
const mockGetAccountAging = getAccountAging as jest.MockedFunction<typeof getAccountAging>;
const mockApplyCreditOverride = applyCreditOverride as jest.MockedFunction<typeof applyCreditOverride>;
const mockDeleteCreditOverride = deleteCreditOverride as jest.MockedFunction<typeof deleteCreditOverride>;

// ─── Fixtures ────────────────────────────────────────────────────────────────

function accountFixture(overrides: Record<string, unknown> = {}) {
  return {
    account_id: "acc_001",
    tenant_id: "tenant-a",
    customer_id: "cust_001",
    display_name: "Acme Main Account",
    status: "active",
    tier: "enterprise",
    credit_state: "ok",
    credit_limit_cents: 5000000,
    open_balance_cents: 1250000,
    available_credit_cents: 3750000,
    credit_balance_cents: 0,
    net_terms_days: 30,
    billing_address: {
      line1: "123 Main St",
      city: "Houston",
      state: "TX",
      zip: "77001",
      country: "US",
    },
    credit_override_expires_at: null,
    payment_method_preference: "invoice",
    created_at: "2024-01-15T10:00:00Z",
    updated_at: "2024-06-01T08:00:00Z",
    external_refs: {},
    ...overrides,
  };
}

function agingFixture(overrides: Record<string, unknown> = {}) {
  return {
    bucket_0_30_cents: 500000,
    bucket_31_60_cents: 300000,
    bucket_61_90_cents: 200000,
    bucket_90_plus_cents: 150000,
    total_open_cents: 1150000,
    ...overrides,
  };
}

// ─── Tests ───────────────────────────────────────────────────────────────────

describe("AccountDetailPage", () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  it("renders account details and aging buckets on successful fetch", async () => {
    mockGetAccount.mockResolvedValue({ data: accountFixture(), request_id: "r1" } as any);
    mockGetAccountAging.mockResolvedValue({ data: agingFixture(), request_id: "r2" } as any);

    render(<AccountDetailPage accountId="acc_001" />);

    await waitFor(() => {
      expect(screen.getByText("Acme Main Account")).toBeInTheDocument();
    });

    // Credit summary
    expect(screen.getByText("$50,000.00")).toBeInTheDocument(); // credit limit

    // Aging buckets
    expect(screen.getByText("$5,000.00")).toBeInTheDocument(); // 0-30
    expect(screen.getByText("$3,000.00")).toBeInTheDocument(); // 31-60
    expect(screen.getByText("$2,000.00")).toBeInTheDocument(); // 61-90
    expect(screen.getByText("$1,500.00")).toBeInTheDocument(); // 90+
  });

  it("shows loading state initially", () => {
    mockGetAccount.mockReturnValue(new Promise(() => {}));
    mockGetAccountAging.mockReturnValue(new Promise(() => {}));

    render(<AccountDetailPage accountId="acc_001" />);
    expect(screen.getByRole("status")).toBeInTheDocument();
  });

  it("shows error state on fetch failure", async () => {
    mockGetAccount.mockRejectedValue(new Error("Server error"));
    mockGetAccountAging.mockRejectedValue(new Error("Server error"));

    render(<AccountDetailPage accountId="acc_001" />);

    await waitFor(() => {
      expect(screen.getByRole("alert")).toBeInTheDocument();
      expect(screen.getByText(/Server error/)).toBeInTheDocument();
    });
  });

  it("opens credit override drawer when button is clicked", async () => {
    mockGetAccount.mockResolvedValue({ data: accountFixture(), request_id: "r1" } as any);
    mockGetAccountAging.mockResolvedValue({ data: agingFixture(), request_id: "r2" } as any);

    render(<AccountDetailPage accountId="acc_001" />);

    await waitFor(() => {
      expect(screen.getByText("Acme Main Account")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: /Credit Override/i }));

    await waitFor(() => {
      expect(screen.getByRole("dialog")).toBeInTheDocument();
      expect(screen.getByText("Apply Credit Override")).toBeInTheDocument();
    });
  });

  it("submits credit override form", async () => {
    const updatedAccount = accountFixture({
      credit_state: "override",
      credit_override_expires_at: "2024-07-01T00:00:00Z",
    });
    mockGetAccount.mockResolvedValue({ data: accountFixture(), request_id: "r1" } as any);
    mockGetAccountAging.mockResolvedValue({ data: agingFixture(), request_id: "r2" } as any);
    mockApplyCreditOverride.mockResolvedValue({ data: updatedAccount, request_id: "r3" } as any);

    render(<AccountDetailPage accountId="acc_001" />);

    await waitFor(() => {
      expect(screen.getByText("Acme Main Account")).toBeInTheDocument();
    });

    // Open drawer
    fireEvent.click(screen.getByRole("button", { name: /Credit Override/i }));

    await waitFor(() => {
      expect(screen.getByRole("dialog")).toBeInTheDocument();
    });

    // Fill form
    const reasonInput = screen.getByLabelText("Reason");
    fireEvent.change(reasonInput, { target: { value: "VIP customer" } });

    // Submit
    fireEvent.click(screen.getByRole("button", { name: /Apply Override/i }));

    await waitFor(() => {
      expect(mockApplyCreditOverride).toHaveBeenCalledWith(
        "acc_001",
        expect.objectContaining({ reason: "VIP customer" }),
      );
    });
  });

  it("expires credit override when Expire Now is clicked", async () => {
    const accountWithOverride = accountFixture({
      credit_state: "override",
      credit_override_expires_at: "2024-07-01T00:00:00Z",
    });
    const accountAfterExpire = accountFixture({ credit_state: "ok" });

    mockGetAccount.mockResolvedValue({ data: accountWithOverride, request_id: "r1" } as any);
    mockGetAccountAging.mockResolvedValue({ data: agingFixture(), request_id: "r2" } as any);
    mockDeleteCreditOverride.mockResolvedValue({ data: accountAfterExpire, request_id: "r3" } as any);

    render(<AccountDetailPage accountId="acc_001" />);

    await waitFor(() => {
      expect(screen.getByText(/Credit override active/)).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: /Expire Now/i }));

    await waitFor(() => {
      expect(mockDeleteCreditOverride).toHaveBeenCalledWith("acc_001");
    });
  });

  it("calls onViewCustomer when parent customer link is clicked", async () => {
    mockGetAccount.mockResolvedValue({ data: accountFixture(), request_id: "r1" } as any);
    mockGetAccountAging.mockResolvedValue({ data: agingFixture(), request_id: "r2" } as any);

    const onViewCustomer = jest.fn();
    render(<AccountDetailPage accountId="acc_001" onViewCustomer={onViewCustomer} />);

    await waitFor(() => {
      expect(screen.getByText("Acme Main Account")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: /View Parent Customer/i }));
    expect(onViewCustomer).toHaveBeenCalledWith("cust_001");
  });
});
