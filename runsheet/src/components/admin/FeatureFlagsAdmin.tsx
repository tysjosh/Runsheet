"use client";

/**
 * Settings → Data → Feature flags (UI revamp task 3.8): the tenant in the
 * toolbar, the flags as a DataTable with a state badge, and "Change state" as
 * an sm FormDialog (design.md §5) holding the four states and the rollout
 * guidance that used to sit below the form.
 */
import { RefreshCw, SlidersHorizontal } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import {
  type FeatureFlagState,
  getOrderIntakePipelineState,
  setOrderIntakePipelineState,
} from "../../services/adminApi";
import type { StatusKey } from "../../styles/tokens";
import {
  type Column,
  DataTable,
  FormDialog,
  IconButton,
  StatusBadge,
  Toolbar,
  usePageChrome,
} from "../ui";
import { PageTitle } from "../ui/PageHeader";
import { notify } from "../ui/toast/notify";

const STATES: FeatureFlagState[] = [
  "disabled",
  "shadow",
  "active_gated",
  "active_auto",
];

const STATE_STYLE: Record<
  FeatureFlagState,
  { status: StatusKey; label: string; description: string }
> = {
  disabled: {
    status: "draft",
    label: "Disabled",
    description:
      "Completely off. All orders route through the legacy pipeline. Use this to roll back.",
  },
  shadow: {
    status: "planned",
    label: "Shadow mode",
    description:
      "The new pipeline processes orders but only logs results (nothing is persisted). Start here.",
  },
  active_gated: {
    status: "warning",
    label: "Active (gated)",
    description:
      "The new pipeline processes real orders and a dispatcher approves each one.",
  },
  active_auto: {
    status: "ok",
    label: "Active (auto)",
    description:
      "Fully on. Orders are processed automatically without approval.",
  },
};

export function FlagStateBadge({ state }: { state: FeatureFlagState }) {
  const s = STATE_STYLE[state] ?? {
    status: "draft" as StatusKey,
    label: String(state),
  };
  return <StatusBadge status={s.status} label={s.label} />;
}

interface FlagRow {
  id: "order_intake_pipeline";
  name: string;
  description: string;
  state: FeatureFlagState | null;
}

export default function FeatureFlagsAdmin() {
  const [tenantId, setTenantId] = useState("demo-tenant");
  const [currentState, setCurrentState] = useState<FeatureFlagState | null>(
    null,
  );
  const [stateLoading, setStateLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [changing, setChanging] = useState(false);
  const [reload, setReload] = useState(0);

  // Load the tenant's current pipeline state on mount and whenever the
  // tenant changes (debounced while typing).
  const fetchState = useCallback(async () => {
    if (!tenantId.trim()) return;
    setStateLoading(true);
    setError(null);
    try {
      const response = await getOrderIntakePipelineState(tenantId.trim());
      setCurrentState(response.data.state);
    } catch (err) {
      setCurrentState(null);
      setError(
        err instanceof Error
          ? err.message
          : "Failed to load current feature flag state",
      );
    } finally {
      setStateLoading(false);
    }
    // `reload` refetches from the Refresh button.
  }, [tenantId, reload]);

  useEffect(() => {
    const t = setTimeout(() => {
      fetchState();
    }, 400);
    return () => clearTimeout(t);
  }, [fetchState]);

  const embedded = usePageChrome({});

  const rows: FlagRow[] = [
    {
      id: "order_intake_pipeline",
      name: "Order intake pipeline",
      description: "Rollout of the new order intake pipeline",
      state: currentState,
    },
  ];

  const columns: Column<FlagRow>[] = [
    {
      key: "name",
      header: "Flag",
      width: 220,
      className: "font-medium text-text",
      cell: (r) => r.name,
    },
    {
      key: "description",
      header: "What it controls",
      truncate: true,
      className: "text-slate-700",
      cell: (r) => r.description,
    },
    {
      key: "state",
      header: "State",
      width: 170,
      cell: (r) =>
        r.state ? (
          <FlagStateBadge state={r.state} />
        ) : stateLoading ? (
          <span className="text-text-muted">Loading…</span>
        ) : (
          "—"
        ),
    },
  ];

  return (
    <div className="flex h-full flex-col bg-surface">
      {!embedded && (
        <div className="flex h-11 items-center border-b border-slate-200 px-4">
          <PageTitle className="text-base font-semibold text-text">
            Feature Flags
          </PageTitle>
        </div>
      )}
      <Toolbar
        label="Feature flags"
        search={
          <input
            type="text"
            value={tenantId}
            onChange={(e) => setTenantId(e.target.value)}
            placeholder="demo-tenant"
            aria-label="Tenant ID"
            className="h-7 w-full rounded-lg border border-slate-300 bg-surface px-2.5 text-xs text-slate-900 placeholder:text-slate-500 focus:outline-none focus-visible:ring-2 focus-visible:ring-focus"
          />
        }
        filters={
          <span className="truncate text-xs text-text-muted">
            Changes take effect within 60 seconds and are broadcast to every
            open session.
          </span>
        }
        end={
          <IconButton
            label="Refresh"
            size="sm"
            onClick={() => setReload((n) => n + 1)}
            icon={
              <RefreshCw
                className={`h-3.5 w-3.5 ${stateLoading ? "animate-spin" : ""}`}
              />
            }
          />
        }
      />
      <div className="min-h-0 flex-1 overflow-auto">
        <DataTable<FlagRow>
          ariaLabel="Feature flags"
          columns={columns}
          data={rows}
          error={error ? { message: error, onRetry: fetchState } : null}
          getRowId={(r) => r.id}
          rowLabel={(r) => r.name}
          onRowClick={() => currentState && setChanging(true)}
          rowMenu={() => [
            {
              id: "change",
              label: "Change state",
              icon: <SlidersHorizontal className="h-3.5 w-3.5" />,
              disabled: !currentState,
              onSelect: () => setChanging(true),
            },
          ]}
        />
      </div>

      {changing && currentState && (
        <FormDialog<{ state: FeatureFlagState }, FeatureFlagState>
          open
          size="sm"
          title="Order intake pipeline"
          help={`Tenant ${tenantId.trim()}. Roll out shadow → gated → auto; set Disabled to roll back.`}
          submitLabel="Update flag"
          successMessage={null}
          initialValues={{ state: currentState }}
          validate={(v) => ({
            state:
              v.state === currentState
                ? "Pick a state other than the current one."
                : undefined,
          })}
          onSubmit={async (v) => {
            const response = await setOrderIntakePipelineState(
              tenantId.trim(),
              v.state,
            );
            return response.data.new_state;
          }}
          onSaved={(next) => {
            setCurrentState(next);
            notify({
              type: "success",
              message: `Order intake pipeline is now ${STATE_STYLE[next]?.label ?? next}.`,
            });
          }}
          onClose={() => setChanging(false)}
        >
          {({ values, set, errors }) => (
            <fieldset className="col-span-2">
              <legend className="mb-2 text-xs font-medium text-slate-700">
                State
              </legend>
              <div className="space-y-2">
                {STATES.map((state) => (
                  <label
                    key={state}
                    className={`flex cursor-pointer items-start gap-2 rounded-lg border p-2.5 ${
                      values.state === state
                        ? "border-primary bg-primary-soft"
                        : "border-slate-200 hover:bg-slate-50"
                    }`}
                  >
                    <input
                      type="radio"
                      name="flag-state"
                      value={state}
                      checked={values.state === state}
                      onChange={() => set("state", state)}
                      className="mt-1"
                    />
                    <span className="min-w-0">
                      <span className="flex items-center gap-2">
                        <FlagStateBadge state={state} />
                        {state === currentState && (
                          <span className="text-xs text-text-muted">
                            Current
                          </span>
                        )}
                      </span>
                      <span className="mt-1 block text-xs text-slate-700">
                        {STATE_STYLE[state].description}
                      </span>
                    </span>
                  </label>
                ))}
              </div>
              {errors.state && (
                <p role="alert" className="mt-1 text-xs text-red-800">
                  {errors.state}
                </p>
              )}
            </fieldset>
          )}
        </FormDialog>
      )}
    </div>
  );
}
