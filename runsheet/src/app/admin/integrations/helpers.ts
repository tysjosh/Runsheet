import {
  deriveMarketplaceStatus,
  INTEGRATION_CATEGORY_ORDER,
  type IntegrationInstance,
  type MarketplaceStatus,
  type ProviderCatalogEntry,
} from "../../../services/integrationsApi";

/**
 * Build a stable, category-ordered list of providers from the catalog response.
 * Unknown categories fall through to the end so backend additions do not require
 * a frontend deploy before they render.
 */
export function groupProvidersByCategory(
  providers: ProviderCatalogEntry[],
): Array<{ category: string; providers: ProviderCatalogEntry[] }> {
  const groups = new Map<string, ProviderCatalogEntry[]>();
  for (const provider of providers) {
    const list = groups.get(provider.category) ?? [];
    list.push(provider);
    groups.set(provider.category, list);
  }

  const ordered: Array<{
    category: string;
    providers: ProviderCatalogEntry[];
  }> = [];

  for (const category of INTEGRATION_CATEGORY_ORDER) {
    const list = groups.get(category);
    if (list && list.length > 0) {
      ordered.push({ category, providers: sortByName(list) });
      groups.delete(category);
    }
  }

  for (const [category, list] of groups.entries()) {
    ordered.push({ category, providers: sortByName(list) });
  }

  return ordered;
}

function sortByName(providers: ProviderCatalogEntry[]): ProviderCatalogEntry[] {
  return [...providers].sort((a, b) =>
    a.provider_name.localeCompare(b.provider_name),
  );
}

/**
 * Filter the catalog by free-text search and derived marketplace status.
 */
export function filterProviders(
  providers: ProviderCatalogEntry[],
  instancesByProvider: Map<string, IntegrationInstance>,
  query: string,
  status: MarketplaceStatus | "all",
): ProviderCatalogEntry[] {
  const normalized = query.trim().toLowerCase();
  return providers.filter((provider) => {
    if (normalized) {
      const haystack =
        `${provider.provider_name} ${provider.description} ${provider.category}`.toLowerCase();
      if (!haystack.includes(normalized)) return false;
    }
    if (status !== "all") {
      const instance = instancesByProvider.get(provider.provider_name) ?? null;
      if (deriveMarketplaceStatus(instance) !== status) return false;
    }
    return true;
  });
}
