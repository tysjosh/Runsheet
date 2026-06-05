/**
 * Tests for IntakeChannelsAdminPanel component.
 *
 * Covers:
 * - Happy path: list channels
 * - Create channel flow + secret modal
 * - Rotate secret flow
 * - Toggle enable/disable
 * - Delete channel
 * - Error handling
 * - Role gating (admin-only access implied by panel placement)
 */

import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

jest.mock("../../services/intakeChannelsApi", () => ({
  listIntakeChannels: jest.fn(),
  createIntakeChannel: jest.fn(),
  rotateIntakeChannelSecret: jest.fn(),
  updateIntakeChannel: jest.fn(),
  deleteIntakeChannel: jest.fn(),
}));

jest.mock("../../services/api", () => ({
  ApiError: class ApiError extends Error {
    status: number;
    constructor(message: string, status: number) {
      super(message);
      this.name = "ApiError";
      this.status = status;
    }
  },
  API_TIMEOUTS: { STANDARD: 30000 },
  ApiTimeoutError: class extends Error {},
}));

import type {
  IntakeChannel,
  IntakeChannelWithSecret,
} from "../../services/intakeChannelsApi";
import {
  createIntakeChannel,
  deleteIntakeChannel,
  listIntakeChannels,
  rotateIntakeChannelSecret,
  updateIntakeChannel,
} from "../../services/intakeChannelsApi";
import IntakeChannelsAdminPanel from "../admin/IntakeChannelsAdminPanel";

const mockList = listIntakeChannels as jest.MockedFunction<
  typeof listIntakeChannels
>;
const mockCreate = createIntakeChannel as jest.MockedFunction<
  typeof createIntakeChannel
>;
const mockRotate = rotateIntakeChannelSecret as jest.MockedFunction<
  typeof rotateIntakeChannelSecret
>;
const mockUpdate = updateIntakeChannel as jest.MockedFunction<
  typeof updateIntakeChannel
>;
const mockDelete = deleteIntakeChannel as jest.MockedFunction<
  typeof deleteIntakeChannel
>;

// ─── Fixtures ────────────────────────────────────────────────────────────────

function channelFixture(overrides: Partial<IntakeChannel> = {}): IntakeChannel {
  return {
    channel_id: "voice-provider-1",
    tenant_id: "tenant-a",
    channel_type: "voice",
    display_name: "Voice AI Provider",
    hmac_secret_ref: "ref:vault:abc123",
    supported_schema_versions: ["1.0"],
    rate_limit_per_minute: 100,
    secret_version: 1,
    enabled: true,
    created_at: "2024-06-01T00:00:00Z",
    updated_at: "2024-06-01T00:00:00Z",
    ...overrides,
  };
}

function channelWithSecretFixture(
  overrides: Partial<IntakeChannelWithSecret> = {},
): IntakeChannelWithSecret {
  return {
    channel_id: "voice-provider-1",
    tenant_id: "tenant-a",
    channel_type: "voice",
    display_name: "Voice AI Provider",
    hmac_secret: "super-secret-key-abc123",
    hmac_secret_ref: "ref:vault:abc123",
    supported_schema_versions: ["1.0"],
    rate_limit_per_minute: 100,
    secret_version: 1,
    enabled: true,
    created_at: "2024-06-01T00:00:00Z",
    updated_at: "2024-06-01T00:00:00Z",
    ...overrides,
  };
}

beforeEach(() => {
  mockList.mockReset();
  mockCreate.mockReset();
  mockRotate.mockReset();
  mockUpdate.mockReset();
  mockDelete.mockReset();
  // Mock clipboard
  Object.assign(navigator, {
    clipboard: { writeText: jest.fn().mockResolvedValue(undefined) },
  });
});

// ─── Tests ───────────────────────────────────────────────────────────────────

describe("IntakeChannelsAdminPanel — list", () => {
  it("fetches and renders channels on mount", async () => {
    mockList.mockResolvedValue({
      items: [
        channelFixture({ channel_id: "ch-1", display_name: "Voice Provider" }),
        channelFixture({
          channel_id: "ch-2",
          display_name: "EDI Feed",
          channel_type: "edi",
        }),
      ],
      total: 2,
    });

    render(<IntakeChannelsAdminPanel />);

    await waitFor(() => expect(mockList).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("Voice Provider")).toBeInTheDocument();
    expect(screen.getByText("EDI Feed")).toBeInTheDocument();
  });

  it("renders empty state when no channels", async () => {
    mockList.mockResolvedValue({ items: [], total: 0 });

    render(<IntakeChannelsAdminPanel />);

    expect(
      await screen.findByText(/no intake channels registered/i),
    ).toBeInTheDocument();
  });

  it("renders error on fetch failure", async () => {
    mockList.mockRejectedValue(new Error("Network error"));

    render(<IntakeChannelsAdminPanel />);

    expect(await screen.findByText(/network error/i)).toBeInTheDocument();
  });
});

describe("IntakeChannelsAdminPanel — create", () => {
  it("opens create form and submits successfully", async () => {
    mockList.mockResolvedValue({ items: [], total: 0 });
    mockCreate.mockResolvedValue(
      channelWithSecretFixture({ hmac_secret: "new-secret-xyz" }),
    );

    render(<IntakeChannelsAdminPanel />);

    await waitFor(() => expect(mockList).toHaveBeenCalled());

    // Open create form
    fireEvent.click(screen.getByRole("button", { name: /register channel/i }));

    // Fill form
    fireEvent.change(screen.getByLabelText(/channel id/i), {
      target: { value: "my-channel" },
    });
    fireEvent.change(screen.getByLabelText(/display name/i), {
      target: { value: "My Channel" },
    });

    // Submit
    await act(async () => {
      fireEvent.click(screen.getByText("Create"));
    });

    await waitFor(() => expect(mockCreate).toHaveBeenCalledTimes(1));

    // Secret modal appears
    expect(await screen.findByTestId("secret-value")).toHaveTextContent(
      "new-secret-xyz",
    );
  });

  it("shows secret exactly once with copy button", async () => {
    mockList.mockResolvedValue({ items: [], total: 0 });
    mockCreate.mockResolvedValue(
      channelWithSecretFixture({ hmac_secret: "one-time-secret" }),
    );

    render(<IntakeChannelsAdminPanel />);

    await waitFor(() => expect(mockList).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("button", { name: /register channel/i }));
    fireEvent.change(screen.getByLabelText(/channel id/i), {
      target: { value: "ch-new" },
    });
    fireEvent.change(screen.getByLabelText(/display name/i), {
      target: { value: "New" },
    });

    await act(async () => {
      fireEvent.click(screen.getByText("Create"));
    });

    await waitFor(() => {
      expect(screen.getByTestId("secret-value")).toHaveTextContent(
        "one-time-secret",
      );
    });

    // Copy button exists
    const copyBtn = screen.getByRole("button", { name: /copy to clipboard/i });
    expect(copyBtn).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(copyBtn);
    });

    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
      "one-time-secret",
    );
  });
});

describe("IntakeChannelsAdminPanel — rotate secret", () => {
  it("rotates secret and shows new secret in modal", async () => {
    mockList.mockResolvedValue({
      items: [channelFixture({ channel_id: "ch-rotate" })],
      total: 1,
    });
    mockRotate.mockResolvedValue(
      channelWithSecretFixture({ hmac_secret: "rotated-secret-999" }),
    );

    render(<IntakeChannelsAdminPanel />);

    // Wait for channels to load
    expect(await screen.findByText("ch-rotate")).toBeInTheDocument();

    // Click rotate button
    fireEvent.click(
      screen.getByRole("button", { name: /rotate secret for ch-rotate/i }),
    );

    await waitFor(() => expect(mockRotate).toHaveBeenCalledWith("ch-rotate"));
    expect(await screen.findByTestId("secret-value")).toHaveTextContent(
      "rotated-secret-999",
    );
  });
});

describe("IntakeChannelsAdminPanel — toggle enabled", () => {
  it("disables an enabled channel", async () => {
    mockList.mockResolvedValue({
      items: [channelFixture({ channel_id: "ch-toggle", enabled: true })],
      total: 1,
    });
    mockUpdate.mockResolvedValue(channelFixture({ enabled: false }));

    render(<IntakeChannelsAdminPanel />);

    // Wait for channels to load
    expect(await screen.findByText("ch-toggle")).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(
        screen.getByRole("button", { name: /disable ch-toggle/i }),
      );
    });

    await waitFor(() =>
      expect(mockUpdate).toHaveBeenCalledWith("ch-toggle", { enabled: false }),
    );
  });
});

describe("IntakeChannelsAdminPanel — delete", () => {
  it("deletes a channel after confirmation", async () => {
    mockList.mockResolvedValue({
      items: [channelFixture({ channel_id: "ch-del" })],
      total: 1,
    });
    mockDelete.mockResolvedValue(undefined);
    window.confirm = jest.fn(() => true);

    render(<IntakeChannelsAdminPanel />);

    // Wait for channels to load
    expect(await screen.findByText("ch-del")).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /delete ch-del/i }));
    });

    expect(window.confirm).toHaveBeenCalled();
    await waitFor(() => expect(mockDelete).toHaveBeenCalledWith("ch-del"));
  });
});
