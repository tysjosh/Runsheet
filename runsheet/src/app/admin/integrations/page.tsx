"use client";

/**
 * Integration Marketplace page (``/admin/integrations``).
 *
 * Surfaces the catalog of available third-party integrations grouped
 * by category with status badges (Req 5.6.1), drives per-provider
 * connect flows (Req 5.6.3), and exposes the enable / disable /
 * sync-now / disconnect controls (Req 5.6.5). Wires straight into
 * ``/api/integrations/*`` via the typed client in
 * :file:`runsheet/src/services/integrationsApi.ts`.
 *
 * Per-tenant feature-flag filtering (Req 5.6.6) is deferred to a
 * future task — the backend exposes ``effective_feature_flag_key``
 * on every catalog entry, but no tenant-flag client exists in the
 * frontend yet. Until that lands, the page surfaces every registered
 * provider; filtering will slot into :func:`filterProviders` without
 * schema changes.
 *
 * Validates: Requirements 5.6.1, 5.6.2, 5.6.3, 5.6.4, 5.6.5.
 */

import {
  AlertTriangle,
  Check,
  Link2,
  Loader2,
  RefreshCw,
  Search,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import IntegrationCard from "../../../components/admin/IntegrationCard";
import { ApiError } from "../../../services/api";
import {
  createIntegrationInstance,
  deleteIntegrationInstance,
  deriveMarketplaceStatus,
  disableIntegrationInstance,
  enableIntegrationInstance,
  findInstanceForProvider,
  INTEGRATION_CATEGORY_LABELS,
  INTEGRATION_CATEGORY_ORDER,
  type IntegrationCategory,
  type IntegrationInstance,
  listIntegrationInstances,
  listIntegrationProviders,
  listSyncRuns,
  type MarketplaceStatus,
  type ProviderCatalogEntry,
  type SyncRun,
  syncIntegrationNow,
} from "../../../services/integrationsApi";
import { filterProviders, groupProvidersByCategory } from "./helpers";

// ─── Constants ───────────────────────────────────────────────────────────────

/**
 * Number of recent :class:`SyncRun` records pulled per provider for
 * the card view. The backend caps at 50, and the Marketplace only
 * renders the latest one, so 10 is plenty and keeps the paint light.
 */
const SYNC_RUN_TAIL_SIZE = 10;

const STATUS_FILTER_OPTIONS: {
  value: MarketplaceStatus | "all";
  label: string;
}[] = [
  { value: "all", label: "All statuses" },
  { value: "available", label: "Available" },
  { value: "connected", label: "Connected" },
  { value: "pending", label: "Pending" },
  { value: "disabled", label: "Disabled" },
  { value: "error", label: "Error" },
];

// ─── Toasts (same shape as peer admin/ops pages) ─────────────────────────────

interface Toast {
  id: number;
  message: string;
  type: "success" | "error";
}

let toastIdCounter = 0;

function ToastContainer({
  toasts,
  onDismiss,
}: {
  toasts: Toast[];
  onDismiss: (id: number) => void;
}) {
  if (toasts.length === 0) return null;
  return (
    <div className="fixed top-4 right-4 z-[100] space-y-2">
      {toasts.map((toast) => (
        <div
          key={toast.id}
          className={`flex items-center gap-2 px-4 py-3 rounded-lg shadow-lg text-sm font-medium ${
            toast.type === "success"
              ? "bg-success text-white"
              : "bg-error text-white"
          }`}
        >
          {toast.type === "success" ? (
            <Check className="w-4 h-4" aria-hidden="true" />
          ) : (
            <AlertTriangle className="w-4 h-4" aria-hidden="true" />
          )}
          <span>{toast.message}</span>
          <button
            type="button"
            onClick={() => onDismiss(toast.id)}
            className="ml-2 p-0.5 hover:bg-white/20 rounded"
            aria-label="Dismiss notification"
          >
            <X className="w-3 h-3" />
          </button>
        </div>
      ))}
    </div>
  );
}

function useToasts() {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const addToast = useCallback((message: string, type: "success" | "error") => {
    const id = ++toastIdCounter;
    setToasts((prev) => [...prev, { id, message, type }]);
    setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id));
    }, 4000);
  }, []);

  const dismissToast = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  return { toasts, addToast, dismissToast };
}

function formatCategoryLabel(category: string): string {
  const known = INTEGRATION_CATEGORY_LABELS as Record<string, string>;
  return (
    known[category] ??
    category
      .split("_")
      .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
      .join(" ")
  );
}

// ─── Category Pill Row ───────────────────────────────────────────────────────

interface CategoryPillsProps {
  categories: Array<{ category: string; count: number }>;
  selected: string | null;
  onSelect: (category: string | null) => void;
}

function CategoryPills({ categories, selected, onSelect }: CategoryPillsProps) {
  const pillClass =
    "px-3 py-1.5 text-xs font-medium rounded-full border transition-colors";
  return (
    <div className="flex flex-wrap items-center gap-2">
      <button
        type="button"
        onClick={() => onSelect(null)}
        className={`${pillClass} ${
          selected === null
            ? "bg-primary text-white border-primary"
            : "bg-white text-gray-700 border-gray-200 hover:bg-gray-50"
        }`}
      >
        All categories
      </button>
      {categories.map(({ category, count }) => (
        <button
          key={category}
          type="button"
          onClick={() => onSelect(category === selected ? null : category)}
          className={`${pillClass} ${
            selected === category
              ? "bg-primary text-white border-primary"
              : "bg-white text-gray-700 border-gray-200 hover:bg-gray-50"
          }`}
        >
          {formatCategoryLabel(category)}
          <span className="ml-1 text-[10px] opacity-70">{count}</span>
        </button>
      ))}
    </div>
  );
}

// ─── Main Page ───────────────────────────────────────────────────────────────

export default function IntegrationMarketplacePage() {
  const { toasts, addToast, dismissToast } = useToasts();

  const [providers, setProviders] = useState<ProviderCatalogEntry[]>([]);
  const [instances, setInstances] = useState<IntegrationInstance[]>([]);
  const [syncRunsByInstance, setSyncRunsByInstance] = useState<
    Record<string, SyncRun[]>
  >({});
  const [syncRunsLoading, setSyncRunsLoading] = useState<Set<string>>(
    new Set(),
  );
  const [workingProvider, setWorkingProvider] = useState<string | null>(null);

  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [searchQuery, setSearchQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<MarketplaceStatus | "all">(
    "all",
  );
  const [categoryFilter, setCategoryFilter] = useState<string | null>(null);

  const instancesByProvider = useMemo(() => {
    const map = new Map<string, IntegrationInstance>();
    for (const instance of instances) {
      map.set(instance.provider_name, instance);
    }
    return map;
  }, [instances]);

  // ── Data loaders ──────────────────────────────────────────────────────────

  const fetchSyncRunsFor = useCallback(async (instanceId: string) => {
    setSyncRunsLoading((prev) => {
      const next = new Set(prev);
      next.add(instanceId);
      return next;
    });
    try {
      const response = await listSyncRuns(instanceId, SYNC_RUN_TAIL_SIZE);
      setSyncRunsByInstance((prev) => ({
        ...prev,
        [instanceId]: response.items,
      }));
    } catch (err) {
      // A sync-run fetch failure shouldn't block the card — log and
      // leave the "No sync runs" placeholder in place.
      console.warn(`Failed to load sync runs for instance ${instanceId}:`, err);
    } finally {
      setSyncRunsLoading((prev) => {
        const next = new Set(prev);
        next.delete(instanceId);
        return next;
      });
    }
  }, []);

  const loadCatalogAndInstances = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const [catalog, instanceList] = await Promise.all([
        listIntegrationProviders(),
        listIntegrationInstances({ page: 1, page_size: 200 }),
      ]);
      setProviders(catalog.items);
      setInstances(instanceList.items);

      // Fan-out sync-run loads for every configured instance. These
      // run in parallel and are isolated per-instance so a single
      // failure never blocks the catalog from rendering.
      for (const instance of instanceList.items) {
        void fetchSyncRunsFor(instance.instance_id);
      }
    } catch (err) {
      if (err instanceof ApiError) {
        setLoadError(err.message);
      } else {
        setLoadError(
          err instanceof Error ? err.message : "Failed to load integrations.",
        );
      }
    } finally {
      setLoading(false);
    }
  }, [fetchSyncRunsFor]);

  useEffect(() => {
    loadCatalogAndInstances();
  }, [loadCatalogAndInstances]);

  // ── Mutations ─────────────────────────────────────────────────────────────

  /**
   * Shared helper that coordinates the per-provider ``working`` flag,
   * surfaces a success / error toast, and re-fetches state after the
   * mutation completes. Keeping the wrapper here means every button
   * on the card inherits the same error handling without duplication.
   */
  const runMutation = useCallback(
    async (
      providerName: string,
      action: () => Promise<void>,
      successMsg: string,
      failurePrefix: string,
    ): Promise<void> => {
      setWorkingProvider(providerName);
      try {
        await action();
        addToast(successMsg, "success");
      } catch (err) {
        const message =
          err instanceof Error ? err.message : "Operation failed.";
        addToast(`${failurePrefix}: ${message}`, "error");
        // Re-throw so the card can leave its own local "submitting"
        // state in place (e.g. keep the Connect modal open after a
        // validation failure instead of silently closing it).
        throw err;
      } finally {
        setWorkingProvider(null);
      }
    },
    [addToast],
  );

  const handleConnect = useCallback(
    async (
      provider: ProviderCatalogEntry,
      credentials: Record<string, string>,
    ) => {
      const existing = instancesByProvider.get(provider.provider_name) ?? null;
      await runMutation(
        provider.provider_name,
        async () => {
          if (existing) {
            // Rotation path — re-use ``PATCH`` so the existing
            // credentials_ref is rotated in place. We import the
            // helper lazily to keep the initial bundle lean; the
            // typed client is the same module so there is no
            // network cost.
            const { updateIntegrationInstance } = await import(
              "../../../services/integrationsApi"
            );
            await updateIntegrationInstance(existing.instance_id, {
              credentials,
            });
          } else {
            await createIntegrationInstance({
              provider_name: provider.provider_name,
              category: provider.category as IntegrationCategory,
              enabled: true,
              credentials,
            });
          }
          await loadCatalogAndInstances();
        },
        existing ? "Credentials rotated." : "Integration connected.",
        existing ? "Rotation failed" : "Connect failed",
      );
    },
    [instancesByProvider, loadCatalogAndInstances, runMutation],
  );

  const handleEnable = useCallback(
    async (provider: ProviderCatalogEntry, instance: IntegrationInstance) => {
      await runMutation(
        provider.provider_name,
        async () => {
          const updated = await enableIntegrationInstance(instance.instance_id);
          setInstances((prev) =>
            prev.map((row) =>
              row.instance_id === updated.instance_id ? updated : row,
            ),
          );
        },
        "Integration enabled.",
        "Enable failed",
      );
    },
    [runMutation],
  );

  const handleDisable = useCallback(
    async (provider: ProviderCatalogEntry, instance: IntegrationInstance) => {
      await runMutation(
        provider.provider_name,
        async () => {
          const updated = await disableIntegrationInstance(
            instance.instance_id,
          );
          setInstances((prev) =>
            prev.map((row) =>
              row.instance_id === updated.instance_id ? updated : row,
            ),
          );
        },
        "Integration disabled.",
        "Disable failed",
      );
    },
    [runMutation],
  );

  const handleSyncNow = useCallback(
    async (provider: ProviderCatalogEntry, instance: IntegrationInstance) => {
      await runMutation(
        provider.provider_name,
        async () => {
          const run = await syncIntegrationNow(instance.instance_id);
          // Optimistically splice the new run to the front of the
          // recent-activity list so the card refreshes without a
          // follow-up round-trip.
          setSyncRunsByInstance((prev) => {
            const current = prev[instance.instance_id] ?? [];
            return {
              ...prev,
              [instance.instance_id]: [run, ...current].slice(
                0,
                SYNC_RUN_TAIL_SIZE,
              ),
            };
          });
          // Also re-fetch so last_sync_at + error_details land.
          void fetchSyncRunsFor(instance.instance_id);
        },
        "Sync started.",
        "Sync failed",
      );
    },
    [fetchSyncRunsFor, runMutation],
  );

  const handleDisconnect = useCallback(
    async (provider: ProviderCatalogEntry, instance: IntegrationInstance) => {
      await runMutation(
        provider.provider_name,
        async () => {
          await deleteIntegrationInstance(instance.instance_id);
          setInstances((prev) =>
            prev.filter((row) => row.instance_id !== instance.instance_id),
          );
          setSyncRunsByInstance((prev) => {
            const next = { ...prev };
            delete next[instance.instance_id];
            return next;
          });
        },
        "Integration disconnected.",
        "Disconnect failed",
      );
    },
    [runMutation],
  );

  // ── Derived view model ────────────────────────────────────────────────────

  const filteredProviders = useMemo(
    () =>
      filterProviders(
        providers,
        instancesByProvider,
        searchQuery,
        statusFilter,
      ),
    [providers, instancesByProvider, searchQuery, statusFilter],
  );

  const groupedProviders = useMemo(() => {
    const groups = groupProvidersByCategory(filteredProviders);
    if (!categoryFilter) return groups;
    return groups.filter((group) => group.category === categoryFilter);
  }, [filteredProviders, categoryFilter]);

  // Category pills count reflect the *status-filtered* catalog so
  // operators can see which groups are empty after narrowing by status
  // without also narrowing by category.
  const categoryPillCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const provider of filteredProviders) {
      counts.set(provider.category, (counts.get(provider.category) ?? 0) + 1);
    }
    const out: Array<{ category: string; count: number }> = [];
    for (const category of INTEGRATION_CATEGORY_ORDER) {
      const count = counts.get(category);
      if (count) {
        out.push({ category, count });
        counts.delete(category);
      }
    }
    for (const [category, count] of counts.entries()) {
      out.push({ category, count });
    }
    return out;
  }, [filteredProviders]);

  const totalProviderCount = providers.length;
  const connectedCount = useMemo(
    () =>
      instances.filter(
        (instance) => deriveMarketplaceStatus(instance) === "connected",
      ).length,
    [instances],
  );
  const errorCount = useMemo(
    () =>
      instances.filter(
        (instance) => deriveMarketplaceStatus(instance) === "error",
      ).length,
    [instances],
  );

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <div className="min-h-screen bg-gray-50">
      <ToastContainer toasts={toasts} onDismiss={dismissToast} />

      <div className="max-w-7xl mx-auto px-6 py-6 space-y-6">
        {/* Header */}
        <div className="flex items-start justify-between flex-wrap gap-4">
          <div>
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 rounded-xl flex items-center justify-center bg-primary">
                <Link2 className="w-5 h-5 text-white" aria-hidden="true" />
              </div>
              <div>
                <h1 className="text-xl font-semibold text-primary">
                  Integration Marketplace
                </h1>
                <p className="text-sm text-gray-500 mt-0.5">
                  Discover, connect, and manage third-party integrations for
                  accounting, tank monitors, GPS / ELD, and payments.
                </p>
              </div>
            </div>
          </div>

          <button
            type="button"
            onClick={loadCatalogAndInstances}
            disabled={loading}
            className="inline-flex items-center gap-1.5 px-3 py-2 text-sm text-gray-600 hover:text-gray-800 rounded-lg hover:bg-gray-50 border border-gray-200 disabled:opacity-50"
            aria-label="Refresh integrations"
          >
            <RefreshCw
              className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`}
              aria-hidden="true"
            />
            Refresh
          </button>
        </div>

        {/* Summary strip */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <SummaryTile label="Available providers" value={totalProviderCount} />
          <SummaryTile label="Configured" value={instances.length} />
          <SummaryTile
            label="Connected"
            value={connectedCount}
            tone={connectedCount > 0 ? "success" : "neutral"}
          />
          <SummaryTile
            label="Needing attention"
            value={errorCount}
            tone={errorCount > 0 ? "error" : "neutral"}
          />
        </div>

        {/* Filters */}
        <div className="bg-white rounded-xl shadow-sm border border-gray-100 p-4 space-y-3">
          <div className="flex flex-wrap items-center gap-3">
            <div className="relative flex-1 min-w-[200px]">
              <Search
                className="absolute left-2.5 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400"
                aria-hidden="true"
              />
              <input
                type="search"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder="Search providers..."
                className="w-full pl-8 pr-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300"
                aria-label="Search integrations"
              />
            </div>

            <select
              value={statusFilter}
              onChange={(e) =>
                setStatusFilter(e.target.value as MarketplaceStatus | "all")
              }
              className="px-3 py-2 text-sm border border-gray-200 rounded-lg bg-white focus:ring-2 focus:ring-gray-200 focus:border-gray-300"
              aria-label="Filter by status"
            >
              {STATUS_FILTER_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
          </div>

          <CategoryPills
            categories={categoryPillCounts}
            selected={categoryFilter}
            onSelect={setCategoryFilter}
          />
        </div>

        {/* Load error */}
        {loadError && !loading && (
          <div
            role="alert"
            className="flex items-start gap-3 rounded-xl border border-error-light bg-error-light px-4 py-3"
          >
            <AlertTriangle
              className="h-4 w-4 text-error-dark mt-0.5 shrink-0"
              aria-hidden="true"
            />
            <div className="flex-1 min-w-0">
              <p className="text-sm font-semibold text-error-dark">
                Could not load integrations
              </p>
              <p
                className="mt-1 text-xs text-error-dark truncate"
                title={loadError}
              >
                {loadError}
              </p>
            </div>
            <button
              type="button"
              onClick={loadCatalogAndInstances}
              className="shrink-0 px-3 py-1.5 text-xs font-medium text-white rounded-lg bg-primary hover:bg-primary-hover"
            >
              Retry
            </button>
          </div>
        )}

        {/* Loading skeleton */}
        {loading && (
          <div className="flex items-center justify-center py-12">
            <Loader2
              className="w-6 h-6 text-gray-400 animate-spin"
              aria-hidden="true"
            />
          </div>
        )}

        {/* Catalog */}
        {!loading && !loadError && (
          <div className="space-y-8">
            {groupedProviders.length === 0 && (
              <p className="text-sm text-gray-500 text-center py-12">
                No integrations match the current filters.
              </p>
            )}

            {groupedProviders.map(
              ({ category, providers: categoryProviders }) => (
                <section key={category} data-testid={`category-${category}`}>
                  <div className="flex items-baseline gap-2 mb-3">
                    <h2 className="text-sm font-semibold text-primary uppercase tracking-wide">
                      {formatCategoryLabel(category)}
                    </h2>
                    <span className="text-[11px] text-gray-400">
                      {categoryProviders.length} provider
                      {categoryProviders.length === 1 ? "" : "s"}
                    </span>
                  </div>
                  <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
                    {categoryProviders.map((provider) => {
                      const instance = findInstanceForProvider(
                        instances,
                        provider.provider_name,
                      );
                      const runs = instance
                        ? (syncRunsByInstance[instance.instance_id] ?? [])
                        : [];
                      return (
                        <IntegrationCard
                          key={provider.provider_name}
                          provider={provider}
                          instance={instance}
                          syncRuns={runs}
                          syncRunsLoading={
                            instance
                              ? syncRunsLoading.has(instance.instance_id)
                              : false
                          }
                          working={workingProvider === provider.provider_name}
                          onConnect={(creds) => handleConnect(provider, creds)}
                          onEnable={async () => {
                            if (instance)
                              await handleEnable(provider, instance);
                          }}
                          onDisable={async () => {
                            if (instance)
                              await handleDisable(provider, instance);
                          }}
                          onSyncNow={async () => {
                            if (instance)
                              await handleSyncNow(provider, instance);
                          }}
                          onDisconnect={async () => {
                            if (instance)
                              await handleDisconnect(provider, instance);
                          }}
                        />
                      );
                    })}
                  </div>
                </section>
              ),
            )}
          </div>
        )}
      </div>
    </div>
  );
}

// ─── Summary Tile ────────────────────────────────────────────────────────────

function SummaryTile({
  label,
  value,
  tone = "neutral",
}: {
  label: string;
  value: number;
  tone?: "neutral" | "success" | "error";
}) {
  const toneClasses =
    tone === "success"
      ? "text-success-dark"
      : tone === "error"
        ? "text-error-dark"
        : "text-primary";
  return (
    <div className="bg-white rounded-xl shadow-sm border border-gray-100 px-4 py-3">
      <p className="text-[10px] text-gray-400 uppercase tracking-wide">
        {label}
      </p>
      <p className={`text-2xl font-semibold mt-1 ${toneClasses}`}>{value}</p>
    </div>
  );
}
