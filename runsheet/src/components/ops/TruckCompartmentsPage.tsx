"use client";

/**
 * Truck compartment-state UI (Task 11.9).
 *
 * Surfaces the Capability 7 compartment lifecycle that the Fuel Ops
 * Hardening spec introduced so dispatchers can:
 *
 *  * Pick a truck (by ``truck_id``) and see every configured
 *    compartment with a state badge (``clean`` / ``loaded`` /
 *    ``needs_cleaning``) alongside the last loaded product,
 *    capacity, allowed grades, and last-cleaned timestamp.
 *    Backs onto ``GET /api/fuel/mvp/trucks/{truck_id}/compartments``.
 *  * Record a Cleaning_Event for any compartment via a modal form
 *    with method (flush/purge/sanitize), actor id, notes, and
 *    optional evidence photos. Evidence photos are uploaded through
 *    the presigned-upload contract (``POST /api/driver/pod/uploads/presign``
 *    → ``PUT <upload_url>``) before the cleaning event is POSTed so
 *    the backend only persists validated ``file_ref`` references.
 *    Posts to ``POST /api/fuel/mvp/compartments/{id}/cleaning-events``.
 *
 * UI revamp task 3.1: the configure, cleaning and eligibility flows are
 * FormDialog / Modal (``compartmentDialogs.tsx``); capacities and dates go
 * through ``lib/format``; products show as RP 1637 caps with names.
 *
 * Validates: Requirement 7.1.4.
 */

import { Plus, RefreshCw, Search, Sparkles } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import {
  Button,
  type Column,
  DataTable,
  EntityLink,
  IconButton,
  ProductCap,
  ProductChip,
  StatusBadge,
} from "@/components/ui";
import { dateTime, gallons } from "../../lib/format";
import type {
  CleaningEvent,
  CompartmentLifecycleState,
  CompartmentTruckSummary,
  TruckCompartmentState,
} from "../../services/fuelApi";
import {
  getTruckCompartmentCapacityGallons,
  listCompartmentTrucks,
  listTruckCompartments,
} from "../../services/fuelApi";
import type { StatusKey } from "../../styles/tokens";
import { PageTitle } from "../ui/PageHeader";
import { notify } from "../ui/toast/notify";
import {
  CleaningEventDialog,
  ConfigureCompartmentsDialog,
  LoadEligibilityDialog,
} from "./compartmentDialogs";

// ─── State badge ─────────────────────────────────────────────────────────────

/**
 * Compartment lifecycle state → StatusBadge style and label (icon + text,
 * never colour alone). Exported so tests can assert the mapping.
 */
export const STATE_BADGE_CONFIG: Record<
  CompartmentLifecycleState,
  { label: string; status: StatusKey; title: string }
> = {
  clean: {
    label: "Clean",
    status: "ok",
    title: "Compartment is empty and safe to load any allowed grade.",
  },
  loaded: {
    label: "Loaded",
    status: "in_transit",
    title:
      "Compartment currently holds a product from the most recent loading plan.",
  },
  needs_cleaning: {
    label: "Needs cleaning",
    status: "critical",
    title:
      "Cross-contamination rule triggered: record a cleaning event before the next load.",
  },
};

// ─── Formatters ──────────────────────────────────────────────────────────────

/** Whole gallons through `lib/format` ("5,283 gal"), never a raw float. */
export function formatCapacity(
  compartment: TruckCompartmentState | null | undefined,
): string {
  if (!compartment) return "—";
  const g = getTruckCompartmentCapacityGallons(compartment);
  if (Number.isNaN(g)) return "—";
  return gallons(g);
}

/** Date and time in the tenant time zone ("Wed 8 Oct, 08:30"). */
export function formatTimestamp(iso: string | null | undefined): string {
  return dateTime(iso);
}

export function CompartmentStateBadge({
  state,
}: {
  state: CompartmentLifecycleState;
}) {
  const config = STATE_BADGE_CONFIG[state] ?? STATE_BADGE_CONFIG.clean;
  return (
    <span title={config.title} data-testid={`compartment-state-badge-${state}`}>
      <StatusBadge status={config.status} label={config.label} />
    </span>
  );
}

export type {
  CleaningFormErrors,
  CleaningFormValues,
} from "./compartmentDialogs";
export {
  ELIGIBILITY_DECISION_CONFIG,
  validateCleaningForm,
} from "./compartmentDialogs";

// ─── Page ────────────────────────────────────────────────────────────────────

export default function TruckCompartmentsPage({
  truckId,
  embedded = false,
}: {
  /** When provided, lock the page to one truck and hide the picker. */
  truckId?: string;
  /** Drop the standalone page chrome (padding/card) for slide-over hosting. */
  embedded?: boolean;
} = {}) {
  const [truckIdInput, setTruckIdInput] = useState("");
  const [activeTruckId, setActiveTruckId] = useState<string | null>(null);
  const [items, setItems] = useState<TruckCompartmentState[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [truckOptions, setTruckOptions] = useState<CompartmentTruckSummary[]>(
    [],
  );
  const [truckOptionsLoaded, setTruckOptionsLoaded] = useState(false);
  const [modalCompartment, setModalCompartment] =
    useState<TruckCompartmentState | null>(null);
  const [eligibilityCompartment, setEligibilityCompartment] =
    useState<TruckCompartmentState | null>(null);
  // Configure-compartments modal: null = closed. `lock` pins the truck id
  // (edit an existing tanker); unset = create/define a new one.
  const [configureState, setConfigureState] = useState<{
    truckId?: string;
    lock: boolean;
    items?: TruckCompartmentState[];
  } | null>(null);

  const fetchCompartments = useCallback(async (id: string) => {
    setLoading(true);
    setError("");
    try {
      const resp = await listTruckCompartments(id);
      setItems(resp.items);
      setActiveTruckId(resp.truck_id);
    } catch (err) {
      setItems([]);
      setError(
        err instanceof Error ? err.message : "Failed to load compartments.",
      );
    } finally {
      setLoading(false);
    }
  }, []);

  // When locked to a single truck (drill-down from the fleet table), load it
  // directly and skip the picker entirely. Otherwise load the list of trucks
  // with compartments so the dispatcher can pick one, auto-selecting the first.
  useEffect(() => {
    let cancelled = false;
    if (truckId) {
      setTruckOptionsLoaded(true);
      void fetchCompartments(truckId);
      return () => {
        cancelled = true;
      };
    }
    listCompartmentTrucks()
      .then((resp) => {
        if (cancelled) return;
        setTruckOptions(resp.items);
        setTruckOptionsLoaded(true);
        if (resp.items.length > 0) {
          const first = resp.items[0].truck_id;
          setTruckIdInput(first);
          void fetchCompartments(first);
        }
      })
      .catch(() => {
        if (!cancelled) setTruckOptionsLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [truckId, fetchCompartments]);

  const handleLookup = (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = truckIdInput.trim();
    if (!trimmed) {
      setError("Truck ID is required.");
      return;
    }
    void fetchCompartments(trimmed);
  };

  const handleRefresh = () => {
    if (activeTruckId) void fetchCompartments(activeTruckId);
  };

  const handleCleaningSuccess = (event: CleaningEvent) => {
    notify({
      type: "success",
      message: `Cleaning event recorded (${event.method}) for ${event.compartment_id}.`,
    });
    if (activeTruckId) void fetchCompartments(activeTruckId);
  };

  const handleConfigureSuccess = (savedTruckId: string, count: number) => {
    setConfigureState(null);
    notify({
      type: "success",
      message: `Saved ${count} compartment${count === 1 ? "" : "s"} for ${savedTruckId}.`,
    });
    setTruckIdInput(savedTruckId);
    void fetchCompartments(savedTruckId);
    // Refresh the tanker picker so a newly-defined truck appears in the list.
    if (!truckId) {
      listCompartmentTrucks()
        .then((resp) => setTruckOptions(resp.items))
        .catch(() => {});
    }
  };

  const compartmentColumns: Column<TruckCompartmentState>[] = [
    {
      key: "position",
      header: "#",
      width: 40,
      className: "tabular-nums text-slate-700",
      cell: (c) => (
        <span data-testid={`compartment-row-${c.compartment_id}`}>
          {c.position_index}
        </span>
      ),
    },
    {
      key: "compartment",
      header: "Compartment",
      className: "font-mono text-xs font-medium text-text",
      cell: (c) => c.compartment_id,
    },
    {
      key: "state",
      header: "State",
      width: 140,
      cell: (c) => <CompartmentStateBadge state={c.state} />,
    },
    {
      key: "capacity",
      header: "Capacity",
      align: "right",
      width: 100,
      className: "tabular-nums text-slate-700",
      cell: (c) => formatCapacity(c),
    },
    {
      key: "last_loaded",
      header: "Last loaded",
      cell: (c) =>
        c.last_loaded_product ? (
          <span className="block">
            <ProductChip code={c.last_loaded_product} variant="chip" />
            <span className="block text-xs text-text-muted">
              {formatTimestamp(c.last_loaded_at)}
            </span>
          </span>
        ) : (
          "—"
        ),
    },
    {
      key: "last_cleaned",
      header: "Last cleaned",
      className: "text-xs text-slate-700 whitespace-nowrap",
      cell: (c) => formatTimestamp(c.last_cleaned_at),
    },
    {
      key: "allowed_grades",
      header: "Allowed",
      cell: (c) =>
        c.allowed_grades.length > 0 ? (
          <span className="flex flex-wrap gap-0.5">
            {c.allowed_grades.map((g) => (
              <ProductCap key={g} code={g} />
            ))}
          </span>
        ) : (
          "—"
        ),
    },
    {
      key: "actions",
      header: <span className="sr-only">Actions</span>,
      align: "right",
      cell: (c) => (
        <div className="inline-flex items-center justify-end gap-1">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setEligibilityCompartment(c)}
            data-testid={`check-eligibility-${c.compartment_id}`}
            icon={<Search className="h-3 w-3" aria-hidden="true" />}
          >
            Check
          </Button>
          <Button
            variant={c.state === "needs_cleaning" ? "primary" : "secondary"}
            size="sm"
            onClick={() => setModalCompartment(c)}
            data-testid={`record-cleaning-${c.compartment_id}`}
            icon={<Sparkles className="h-3 w-3" aria-hidden="true" />}
          >
            Record cleaning
          </Button>
        </div>
      ),
    },
  ];

  const configureLabel =
    activeTruckId && items.length > 0
      ? "Edit compartments"
      : "Configure compartments";

  return (
    <div
      className={
        embedded ? "flex flex-col" : "flex flex-1 flex-col overflow-auto"
      }
    >
      {!embedded && (
        <div className="flex h-11 shrink-0 items-center border-b border-slate-200 px-4">
          <PageTitle className="text-base font-semibold text-text">
            Truck compartments
          </PageTitle>
        </div>
      )}
      <div className={embedded ? "w-full" : "w-full p-4"}>
        <div className="mb-3 flex flex-wrap items-center gap-2">
          {activeTruckId && (
            <p
              className="flex items-center gap-1.5 text-xs text-text-muted"
              data-testid="truck-fleet-link"
            >
              Fleet asset:{" "}
              <EntityLink type="asset" id={activeTruckId} className="text-xs" />
            </p>
          )}
          <div className="ml-auto flex items-center gap-1.5">
            {activeTruckId && (
              <IconButton
                label="Refresh compartments"
                size="sm"
                onClick={handleRefresh}
                disabled={loading}
                icon={
                  <RefreshCw
                    className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`}
                  />
                }
              />
            )}
            <Button
              size="sm"
              onClick={() =>
                setConfigureState(
                  activeTruckId
                    ? { truckId: activeTruckId, lock: true, items }
                    : { lock: false },
                )
              }
              data-testid="configure-compartments-btn"
              icon={<Plus className="h-3.5 w-3.5" aria-hidden="true" />}
            >
              {configureLabel}
            </Button>
          </div>
        </div>

        {!truckId && (
          <form
            className="mb-3 flex items-end gap-2"
            onSubmit={handleLookup}
            data-testid="truck-lookup-form"
          >
            <div className="flex-1">
              <label
                htmlFor="truck-id-input"
                className="mb-1 block text-xs font-medium text-slate-700"
              >
                Truck ID
              </label>
              <input
                id="truck-id-input"
                type="text"
                list="compartment-truck-options"
                className="h-8 w-full rounded-lg border border-slate-300 bg-surface px-2.5 text-sm text-slate-900 placeholder:text-slate-500 focus:outline-none focus-visible:ring-2 focus-visible:ring-focus"
                value={truckIdInput}
                onChange={(e) => setTruckIdInput(e.target.value)}
                placeholder={
                  truckOptions.length > 0
                    ? `e.g. ${truckOptions[0].truck_id}`
                    : "e.g. TNK-001"
                }
              />
              <datalist id="compartment-truck-options">
                {truckOptions.map((t) => (
                  <option key={t.truck_id} value={t.truck_id}>
                    {t.truck_id} ({t.compartment_count} compartments)
                  </option>
                ))}
              </datalist>
            </div>
            <Button
              type="submit"
              loading={loading}
              icon={<Search className="h-3.5 w-3.5" aria-hidden="true" />}
            >
              Load compartments
            </Button>
          </form>
        )}

        {truckOptions.length > 0 && !truckId && (
          <div
            role="group"
            aria-label="Tankers"
            className="mb-3 flex flex-wrap items-center gap-1.5"
          >
            {truckOptions.map((t) => (
              <button
                key={t.truck_id}
                type="button"
                aria-pressed={activeTruckId === t.truck_id}
                onClick={() => {
                  setTruckIdInput(t.truck_id);
                  void fetchCompartments(t.truck_id);
                }}
                className={`h-7 rounded-full border px-2.5 font-mono text-xs focus:outline-none focus-visible:ring-2 focus-visible:ring-focus ${
                  activeTruckId === t.truck_id
                    ? "border-primary bg-primary-soft text-brand-800"
                    : "border-slate-300 bg-surface text-slate-700 hover:bg-slate-50"
                }`}
              >
                {t.truck_id}
                <span className="ml-1 text-slate-600">
                  · {t.compartment_count}
                </span>
              </button>
            ))}
          </div>
        )}

        {error && (
          <p
            role="alert"
            className="mb-3 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-800"
          >
            {error}
          </p>
        )}

        {!activeTruckId && !error && !loading && !truckId && (
          <div className="py-12 text-center text-sm text-text-muted">
            {!truckOptionsLoaded ? (
              "Loading tankers…"
            ) : truckOptions.length === 0 ? (
              <span>No trucks have compartments configured yet.</span>
            ) : (
              "Select a tanker above to see its compartments."
            )}
          </div>
        )}

        {activeTruckId && !loading && items.length === 0 && !error && (
          <div className="rounded-lg border border-dashed border-slate-300 py-10 text-center">
            <p className="text-sm font-medium text-slate-800">
              No compartments configured for{" "}
              <span className="font-mono">{activeTruckId}</span>
            </p>
            <p className="mt-1 text-xs text-text-muted">
              Define this tanker's compartments to enable load planning.
            </p>
          </div>
        )}

        {(items.length > 0 || (loading && !!activeTruckId)) && (
          <DataTable<TruckCompartmentState>
            ariaLabel="Truck compartments"
            rowHeight="compact"
            columns={compartmentColumns}
            data={loading ? [] : items}
            loading={loading}
            getRowId={(c) => c.compartment_id}
          />
        )}
      </div>

      {modalCompartment && (
        <CleaningEventDialog
          compartment={modalCompartment}
          onClose={() => setModalCompartment(null)}
          onSuccess={handleCleaningSuccess}
        />
      )}
      {eligibilityCompartment && (
        <LoadEligibilityDialog
          compartment={eligibilityCompartment}
          onClose={() => setEligibilityCompartment(null)}
        />
      )}
      {configureState && (
        <ConfigureCompartmentsDialog
          initialTruckId={configureState.truckId}
          lockTruckId={configureState.lock}
          initialCompartments={configureState.items}
          onClose={() => setConfigureState(null)}
          onSuccess={handleConfigureSuccess}
        />
      )}
    </div>
  );
}
