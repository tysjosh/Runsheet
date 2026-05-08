/**
 * Tests for :file:`IntegrationCard.tsx`.
 *
 * Covers the rendering and interaction pathways the Marketplace page
 * depends on most:
 *
 *   - Status badge selection (available → connect CTA, error →
 *     error banner, disabled → Enable button).
 *   - Connect flow: required-field validation + payload forwarding.
 *   - Disconnect confirmation: no-op on cancel, invokes handler on
 *     confirm.
 *   - OAuth consent anchor only rendered for oauth2 providers with a
 *     supplied URL.
 *   - Sync-run summary rendering (and empty state).
 *
 * Validates: Requirements 5.6.1, 5.6.3, 5.6.4, 5.6.5.
 */

import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import type {
  IntegrationInstance,
  ProviderCatalogEntry,
  SyncRun,
} from "../../services/integrationsApi";
import IntegrationCard, { formatProviderName } from "./IntegrationCard";

function providerFixture(
  overrides: Partial<ProviderCatalogEntry> = {},
): ProviderCatalogEntry {
  return {
    provider_name: "quickbooks_online",
    category: "accounting",
    description: "QuickBooks Online — accounting sync.",
    required_credential_fields: ["client_id", "client_secret", "refresh_token"],
    doc_url: "https://example.com/qbo",
    auth_mode: "oauth2",
    feature_flag_key: null,
    effective_feature_flag_key: "overlay.integration.quickbooks_online",
    ...overrides,
  };
}

function instanceFixture(
  overrides: Partial<IntegrationInstance> = {},
): IntegrationInstance {
  return {
    instance_id: "inst-1",
    tenant_id: "tenant-a",
    provider_name: "quickbooks_online",
    category: "accounting",
    status: "connected",
    enabled: true,
    credentials_ref: "cred:tenant-a:qbo:abc",
    credentials_status: "valid",
    schedule_cron: "0 */1 * * *",
    config: {},
    last_sync_at: "2024-01-15T12:00:00Z",
    last_error: null,
    retry_count: 0,
    updated_at: "2024-01-15T12:00:00Z",
    created_at: "2024-01-10T09:00:00Z",
    ...overrides,
  };
}

function syncRunFixture(overrides: Partial<SyncRun> = {}): SyncRun {
  return {
    run_id: "run-1",
    tenant_id: "tenant-a",
    instance_id: "inst-1",
    provider_name: "quickbooks_online",
    operation: "pull",
    started_at: "2024-01-15T11:00:00Z",
    finished_at: "2024-01-15T11:00:05Z",
    status: "success",
    record_counts: { invoices: 4, payments: 2 },
    error_details: null,
    duration_ms: 5000,
    ...overrides,
  };
}

const handlers = () => ({
  onConnect: jest.fn().mockResolvedValue(undefined),
  onEnable: jest.fn().mockResolvedValue(undefined),
  onDisable: jest.fn().mockResolvedValue(undefined),
  onSyncNow: jest.fn().mockResolvedValue(undefined),
  onDisconnect: jest.fn().mockResolvedValue(undefined),
});

describe("formatProviderName", () => {
  it("title-cases snake-case provider names", () => {
    expect(formatProviderName("quickbooks_online")).toBe("Quickbooks Online");
    expect(formatProviderName("veeder_root")).toBe("Veeder Root");
    expect(formatProviderName("stripe")).toBe("Stripe");
  });
});

describe("IntegrationCard — status + controls", () => {
  it("renders 'Available' badge and Connect CTA when no instance exists", () => {
    const h = handlers();
    render(
      <IntegrationCard
        provider={providerFixture()}
        instance={null}
        syncRuns={[]}
        {...h}
      />,
    );
    expect(screen.getByTestId("status-badge-available")).toBeInTheDocument();
    expect(screen.getByTestId("connect-button")).toBeInTheDocument();
    expect(screen.queryByTestId("disconnect-button")).not.toBeInTheDocument();
    expect(screen.queryByTestId("sync-now-button")).not.toBeInTheDocument();
  });

  it("renders Enable button when the instance is disabled", () => {
    const h = handlers();
    render(
      <IntegrationCard
        provider={providerFixture()}
        instance={instanceFixture({ enabled: false })}
        syncRuns={[]}
        {...h}
      />,
    );
    expect(screen.getByTestId("status-badge-disabled")).toBeInTheDocument();
    expect(screen.getByTestId("enable-button")).toBeInTheDocument();
    expect(screen.queryByTestId("disable-button")).not.toBeInTheDocument();
  });

  it("renders the Disable + Sync now + Rotate + Disconnect controls for a connected instance", () => {
    const h = handlers();
    render(
      <IntegrationCard
        provider={providerFixture()}
        instance={instanceFixture()}
        syncRuns={[syncRunFixture()]}
        {...h}
      />,
    );
    expect(screen.getByTestId("status-badge-connected")).toBeInTheDocument();
    expect(screen.getByTestId("disable-button")).toBeInTheDocument();
    expect(screen.getByTestId("sync-now-button")).toBeInTheDocument();
    expect(screen.getByTestId("rotate-credentials-button")).toBeInTheDocument();
    expect(screen.getByTestId("disconnect-button")).toBeInTheDocument();
  });

  it("surfaces last_error when the instance is in error state", () => {
    const h = handlers();
    render(
      <IntegrationCard
        provider={providerFixture()}
        instance={instanceFixture({
          status: "error",
          last_error: "credentials_expired",
        })}
        syncRuns={[]}
        {...h}
      />,
    );
    expect(screen.getByTestId("status-badge-error")).toBeInTheDocument();
    expect(screen.getByTestId("integration-error")).toHaveTextContent(
      "credentials_expired",
    );
  });

  it("renders Sync now but marks it disabled when the instance is disabled", () => {
    const h = handlers();
    render(
      <IntegrationCard
        provider={providerFixture()}
        instance={instanceFixture({ enabled: false })}
        syncRuns={[]}
        {...h}
      />,
    );
    // Sync-now is still visible so operators can see the control;
    // the button is disabled until the instance is enabled.
    const syncNow = screen.getByTestId("sync-now-button");
    expect(syncNow).toBeDisabled();
    // Clicking a disabled button should never invoke the handler.
    fireEvent.click(syncNow);
    expect(h.onSyncNow).not.toHaveBeenCalled();
  });

  it("renders an OAuth consent link only when a URL is supplied and auth_mode is oauth2", () => {
    const h = handlers();
    const { rerender } = render(
      <IntegrationCard
        provider={providerFixture()}
        instance={null}
        syncRuns={[]}
        oauthAuthorizationUrl="https://example.com/oauth"
        {...h}
      />,
    );
    expect(screen.getByTestId("oauth-consent-link")).toHaveAttribute(
      "href",
      "https://example.com/oauth",
    );

    // API-key provider (auth_mode: "api_key") should never surface
    // the OAuth anchor, even if one is passed accidentally.
    rerender(
      <IntegrationCard
        provider={providerFixture({ auth_mode: "api_key" })}
        instance={null}
        syncRuns={[]}
        oauthAuthorizationUrl="https://example.com/oauth"
        {...handlers()}
      />,
    );
    expect(screen.queryByTestId("oauth-consent-link")).not.toBeInTheDocument();
  });
});

describe("IntegrationCard — connect flow", () => {
  it("forwards trimmed credentials to onConnect when all fields are filled", async () => {
    const h = handlers();
    render(
      <IntegrationCard
        provider={providerFixture({
          auth_mode: "api_key",
          required_credential_fields: ["api_token", "security_code"],
          provider_name: "veeder_root",
        })}
        instance={null}
        syncRuns={[]}
        {...h}
      />,
    );

    fireEvent.click(screen.getByTestId("connect-button"));

    // Scope all queries to the dialog so the card's "Connect" CTA
    // button doesn't collide with the modal's submit button.
    const dialog = await screen.findByRole("dialog");
    const dialogQueries = within(dialog);
    const submitButton = dialogQueries.getByRole("button", {
      name: /^connect$/i,
    });

    fireEvent.change(
      dialogQueries.getByLabelText("api_token") as HTMLInputElement,
      {
        target: { value: "  tok-123  " },
      },
    );
    fireEvent.change(
      dialogQueries.getByLabelText("security_code") as HTMLInputElement,
      {
        target: { value: "secret-abc" },
      },
    );
    await act(async () => {
      fireEvent.click(submitButton);
    });

    await waitFor(() => expect(h.onConnect).toHaveBeenCalledTimes(1));
    // Surrounding whitespace is trimmed so leading/trailing spaces
    // from copy-paste don't propagate into the vault payload.
    expect(h.onConnect).toHaveBeenCalledWith({
      api_token: "tok-123",
      security_code: "secret-abc",
    });
  });

  it("rejects whitespace-only credentials with an in-form error banner", async () => {
    const h = handlers();
    render(
      <IntegrationCard
        provider={providerFixture({
          auth_mode: "api_key",
          required_credential_fields: ["api_token"],
          provider_name: "veeder_root",
        })}
        instance={null}
        syncRuns={[]}
        {...h}
      />,
    );

    fireEvent.click(screen.getByTestId("connect-button"));
    const dialog = await screen.findByRole("dialog");
    const dialogQueries = within(dialog);

    // A single-space value passes the HTML5 ``required`` check (a
    // space is a non-empty value) but the component's own trim-based
    // validation rejects it — which is the behaviour operators rely on
    // so a stray space doesn't get vaulted as a credential.
    fireEvent.change(
      dialogQueries.getByLabelText("api_token") as HTMLInputElement,
      { target: { value: "   " } },
    );
    await act(async () => {
      fireEvent.click(
        dialogQueries.getByRole("button", { name: /^connect$/i }),
      );
    });

    expect(await dialogQueries.findByRole("alert")).toHaveTextContent(
      /api_token is required/i,
    );
    expect(h.onConnect).not.toHaveBeenCalled();
  });

  it("renders password-type inputs for credential-like fields", () => {
    render(
      <IntegrationCard
        provider={providerFixture({
          required_credential_fields: [
            "client_id",
            "client_secret",
            "refresh_token",
          ],
        })}
        instance={null}
        syncRuns={[]}
        {...handlers()}
      />,
    );
    fireEvent.click(screen.getByTestId("connect-button"));

    const clientIdInput = screen.getByLabelText(
      "client_id",
    ) as HTMLInputElement;
    const clientSecretInput = screen.getByLabelText(
      "client_secret",
    ) as HTMLInputElement;
    const refreshTokenInput = screen.getByLabelText(
      "refresh_token",
    ) as HTMLInputElement;

    // client_id → plain text, the other two → obscured password
    // inputs so they don't show up in screen-shares or autofill.
    expect(clientIdInput.type).toBe("text");
    expect(clientSecretInput.type).toBe("password");
    expect(refreshTokenInput.type).toBe("password");
  });
});

describe("IntegrationCard — disconnect flow", () => {
  it("invokes onDisconnect only after confirmation", async () => {
    const h = handlers();
    render(
      <IntegrationCard
        provider={providerFixture()}
        instance={instanceFixture()}
        syncRuns={[]}
        {...h}
      />,
    );

    fireEvent.click(screen.getByTestId("disconnect-button"));
    // Confirmation modal appears — cancel dismisses it without firing.
    // Scope to the dialog so the card-level buttons don't collide.
    const cancelDialog = within(await screen.findByRole("dialog"));
    fireEvent.click(cancelDialog.getByRole("button", { name: /cancel/i }));
    expect(h.onDisconnect).not.toHaveBeenCalled();
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );

    // Re-open and confirm.
    fireEvent.click(screen.getByTestId("disconnect-button"));
    const confirmDialog = within(await screen.findByRole("dialog"));
    await act(async () => {
      fireEvent.click(
        confirmDialog.getByRole("button", { name: /^disconnect$/i }),
      );
    });
    await waitFor(() => expect(h.onDisconnect).toHaveBeenCalledTimes(1));
  });
});

describe("IntegrationCard — sync run summary", () => {
  it("shows an empty state when no runs are recorded yet", () => {
    render(
      <IntegrationCard
        provider={providerFixture()}
        instance={instanceFixture()}
        syncRuns={[]}
        {...handlers()}
      />,
    );
    expect(screen.queryByTestId("sync-run-summary")).not.toBeInTheDocument();
    expect(screen.getByText(/No sync runs recorded yet/i)).toBeInTheDocument();
  });

  it("renders the latest run with its operation, record counts, and relative time", () => {
    render(
      <IntegrationCard
        provider={providerFixture()}
        instance={instanceFixture()}
        syncRuns={[syncRunFixture()]}
        {...handlers()}
      />,
    );
    const summary = screen.getByTestId("sync-run-summary");
    expect(summary).toHaveTextContent(/pull/);
    expect(summary).toHaveTextContent(/invoices: 4/);
    expect(summary).toHaveTextContent(/payments: 2/);
  });

  it("surfaces the error_details string for failed runs", () => {
    render(
      <IntegrationCard
        provider={providerFixture()}
        instance={instanceFixture()}
        syncRuns={[
          syncRunFixture({
            status: "error",
            error_details: "HTTP 500 Internal Server Error",
          }),
        ]}
        {...handlers()}
      />,
    );
    expect(screen.getByTestId("sync-run-error")).toHaveTextContent(
      "HTTP 500 Internal Server Error",
    );
  });
});
