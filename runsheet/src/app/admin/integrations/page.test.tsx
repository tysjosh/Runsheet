/**
 * Tests for the Integration Marketplace page helpers
 * (:file:`/admin/integrations/page.tsx`).
 *
 * The page itself orchestrates several network calls and React state
 * transitions. The lightweight pieces we exercise here are the pure
 * helpers the page exports —
 * :func:`groupProvidersByCategory` and :func:`filterProviders` — which
 * encode the grouping and status-filter behaviour mandated by
 * Requirement 5.6.1.
 *
 * A thicker integration-level test that mounts the page with a
 * stubbed fetch is tracked for Task 11.10's WebSocket hook coverage;
 * the helpers below are the minimum needed to pin the contract.
 *
 * Validates: Requirements 5.6.1, 5.6.2.
 */

import type {
  IntegrationInstance,
  ProviderCatalogEntry,
} from "../../../services/integrationsApi";

jest.mock("../../../utils/auth", () => ({
  getAuthToken: jest.fn(),
}));

import { filterProviders, groupProvidersByCategory } from "./helpers";

function providerFixture(
  overrides: Partial<ProviderCatalogEntry> = {},
): ProviderCatalogEntry {
  return {
    provider_name: "quickbooks_online",
    category: "accounting",
    description: "QuickBooks Online.",
    required_credential_fields: ["client_id", "client_secret"],
    doc_url: null,
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

describe("groupProvidersByCategory", () => {
  it("emits known categories in INTEGRATION_CATEGORY_ORDER regardless of input order", () => {
    const providers: ProviderCatalogEntry[] = [
      providerFixture({ provider_name: "stripe", category: "payment" }),
      providerFixture({
        provider_name: "veeder_root",
        category: "tank_monitor",
      }),
      providerFixture({
        provider_name: "quickbooks_online",
        category: "accounting",
      }),
    ];
    const groups = groupProvidersByCategory(providers);
    expect(groups.map((g) => g.category)).toEqual([
      "accounting",
      "payment",
      "tank_monitor",
    ]);
  });

  it("sorts providers within a category by provider_name", () => {
    const providers: ProviderCatalogEntry[] = [
      providerFixture({ provider_name: "zqbo_delta", category: "accounting" }),
      providerFixture({ provider_name: "alpha_acct", category: "accounting" }),
      providerFixture({ provider_name: "middle", category: "accounting" }),
    ];
    const [group] = groupProvidersByCategory(providers);
    expect(group.providers.map((p) => p.provider_name)).toEqual([
      "alpha_acct",
      "middle",
      "zqbo_delta",
    ]);
  });

  it("appends unknown categories after known ones in insertion order", () => {
    const providers: ProviderCatalogEntry[] = [
      providerFixture({ provider_name: "a", category: "custom_one" }),
      providerFixture({ provider_name: "b", category: "accounting" }),
      providerFixture({ provider_name: "c", category: "custom_two" }),
    ];
    const groups = groupProvidersByCategory(providers);
    expect(groups.map((g) => g.category)).toEqual([
      "accounting",
      "custom_one",
      "custom_two",
    ]);
  });
});

describe("filterProviders", () => {
  const catalog: ProviderCatalogEntry[] = [
    providerFixture({
      provider_name: "quickbooks_online",
      category: "accounting",
    }),
    providerFixture({
      provider_name: "stripe",
      category: "payment",
      description: "Stripe payments.",
    }),
    providerFixture({
      provider_name: "veeder_root",
      category: "tank_monitor",
      description: "Veeder-Root ATG.",
    }),
  ];

  it("returns the full list when query is empty and status='all'", () => {
    expect(filterProviders(catalog, new Map(), "", "all")).toEqual(catalog);
  });

  it("filters by case-insensitive substring against name / description / category", () => {
    expect(
      filterProviders(catalog, new Map(), "VEEDER", "all").map(
        (p) => p.provider_name,
      ),
    ).toEqual(["veeder_root"]);
    expect(
      filterProviders(catalog, new Map(), "payments", "all").map(
        (p) => p.provider_name,
      ),
    ).toEqual(["stripe"]);
    expect(
      filterProviders(catalog, new Map(), "tank_monitor", "all").map(
        (p) => p.provider_name,
      ),
    ).toEqual(["veeder_root"]);
  });

  it("filters by derived marketplace status using the instance map", () => {
    const instances = new Map<string, IntegrationInstance>([
      [
        "quickbooks_online",
        instanceFixture({ provider_name: "quickbooks_online" }),
      ],
      [
        "stripe",
        instanceFixture({
          provider_name: "stripe",
          status: "error",
          last_error: "boom",
        }),
      ],
    ]);

    expect(
      filterProviders(catalog, instances, "", "connected").map(
        (p) => p.provider_name,
      ),
    ).toEqual(["quickbooks_online"]);
    expect(
      filterProviders(catalog, instances, "", "error").map(
        (p) => p.provider_name,
      ),
    ).toEqual(["stripe"]);
    expect(
      filterProviders(catalog, instances, "", "available").map(
        (p) => p.provider_name,
      ),
    ).toEqual(["veeder_root"]);
  });

  it("combines text query and status filter with AND semantics", () => {
    const instances = new Map<string, IntegrationInstance>([
      [
        "quickbooks_online",
        instanceFixture({ provider_name: "quickbooks_online" }),
      ],
    ]);
    expect(
      filterProviders(catalog, instances, "stripe", "connected"),
    ).toHaveLength(0);
  });
});
