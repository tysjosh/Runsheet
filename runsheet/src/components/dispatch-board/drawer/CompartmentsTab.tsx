/**
 * Drawer Compartments tab (R5.5, R5.6). Per load:
 *
 * - the compartment gauge and the solver's allocation;
 * - an override editor per order: gallons per compartment, saved as
 *   `set_allocation` (validated like any command), or reset to automatic;
 * - the terminal picker, with sourcing recommendations, the terminal's wait
 *   and the supply contracts' lift status from the existing `/api/fuel` reads.
 *
 * Volumes are shown in US gallons (R2.7); the command carries litres.
 */
import { useEffect, useId, useState } from "react";
import type {
  CompartmentShare,
  LaneView,
  Load,
} from "../../../services/dispatchBoardApi";
import {
  getSourcingRecommendations,
  getTerminalWaitSummary,
  listSupplierContracts,
  type SourcingTerminalCandidate,
  type SupplierContractResponse,
  type TerminalWaitSummary,
} from "../../../services/fuelApi";
import { type DrawerTarget, useBoard } from "../BoardContext";
import { formatGallons, gallonsToLiters, litersToGallons } from "../boardTime";
import { CompartmentGauge } from "../CompartmentGauge";
import type { TerminalIndex } from "./useTerminalIndex";

const button =
  "min-h-7 rounded-md border border-gray-300 px-2 text-xs font-medium text-gray-800 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50";
const primary =
  "min-h-7 rounded-md bg-primary px-2 text-xs font-medium text-white disabled:cursor-not-allowed disabled:opacity-50";

/** Loads in the drawer's scope. */
export function loadsInScope(lane: LaneView, target: DrawerTarget): Load[] {
  if (target.orderId) {
    return lane.loads.filter((l) =>
      l.stops.some((s) => s.order_id === target.orderId),
    );
  }
  if (target.loadId)
    return lane.loads.filter((l) => l.load_id === target.loadId);
  return lane.loads;
}

function compartmentName(lane: LaneView, id: string): string {
  const c = lane.compartments.find((x) => x.compartment_id === id);
  return c ? `Compartment ${c.position_index + 1}` : `Compartment ${id}`;
}

// ─── Allocation override editor (R5.5) ──────────────────────────────────────

function OverrideEditor({
  lane,
  load,
  orderId,
  onDone,
}: {
  lane: LaneView;
  load: Load;
  orderId: string;
  onDone: () => void;
}) {
  const api = useBoard();
  const id = useId();
  const current = load.allocation_overrides[orderId] ?? [];
  const fromAlloc = load.allocations.filter((a) => a.order_id === orderId);
  const initial: Record<string, string> = {};
  for (const c of lane.compartments) {
    const liters =
      current.find((s) => s.compartment_id === c.compartment_id)?.liters ??
      fromAlloc
        .filter((a) => a.compartment_id === c.compartment_id)
        .reduce((s, a) => s + a.liters, 0);
    initial[c.compartment_id] = liters
      ? String(Math.round(litersToGallons(liters)))
      : "";
  }
  const [values, setValues] = useState(initial);
  const [saving, setSaving] = useState(false);
  const shares: CompartmentShare[] = Object.entries(values)
    .map(([compartment_id, v]) => ({
      compartment_id,
      gallons: Number(v),
    }))
    .filter((s) => Number.isFinite(s.gallons) && s.gallons > 0)
    .map((s) => ({
      compartment_id: s.compartment_id,
      liters: Math.round(gallonsToLiters(s.gallons) * 10) / 10,
    }));
  const total = Object.values(values).reduce(
    (s, v) => s + (Number(v) > 0 ? Number(v) : 0),
    0,
  );
  const stop = load.stops.find((s) => s.order_id === orderId);
  const requested = stop?.snapshot.gallons_requested ?? null;
  const invalid = Object.values(values).some(
    (v) => v !== "" && !(Number(v) >= 0),
  );

  const save = async () => {
    setSaving(true);
    const outcome = await api.command({
      type: "set_allocation",
      load_id: load.load_id,
      order_id: orderId,
      shares,
    });
    setSaving(false);
    if (outcome?.kind === "committed") onDone();
  };

  return (
    <fieldset className="mt-2 rounded-md border border-gray-200 bg-gray-50 p-2">
      <legend className="px-1 text-xs font-medium text-gray-800">
        Compartment split for Order {orderId}
      </legend>
      <div className="grid grid-cols-2 gap-2">
        {lane.compartments.map((c) => {
          const accepts =
            c.accepts_products.length === 0 ||
            !stop?.snapshot.product_code ||
            c.accepts_products.includes(stop.snapshot.product_code);
          return (
            <label
              key={c.compartment_id}
              htmlFor={`${id}-${c.compartment_id}`}
              className="text-xs text-gray-700"
            >
              Compartment {c.position_index + 1} (
              {formatGallons(litersToGallons(c.capacity_l))})
              {!accepts && (
                <span className="block text-[11px] text-error-dark">
                  Not rated for {stop?.snapshot.product_code}
                </span>
              )}
              <input
                id={`${id}-${c.compartment_id}`}
                type="number"
                inputMode="numeric"
                min={0}
                step={1}
                value={values[c.compartment_id] ?? ""}
                onChange={(e) =>
                  setValues((v) => ({
                    ...v,
                    [c.compartment_id]: e.target.value,
                  }))
                }
                className="mt-0.5 w-full rounded-md border border-gray-300 px-1.5 py-1 text-sm"
                aria-describedby={`${id}-total`}
              />
            </label>
          );
        })}
      </div>
      <p id={`${id}-total`} className="mt-1 text-[11px] text-gray-600">
        Total {formatGallons(total)}
        {requested !== null ? ` of ${formatGallons(requested)} ordered` : ""}
      </p>
      <div className="mt-1 flex justify-end gap-1">
        <button type="button" className={button} onClick={onDone}>
          Cancel
        </button>
        <button
          type="button"
          className={primary}
          disabled={saving || invalid || shares.length === 0}
          onClick={() => void save()}
        >
          {saving ? "Saving…" : "Save split"}
        </button>
      </div>
    </fieldset>
  );
}

function AllocationSection({ lane, load }: { lane: LaneView; load: Load }) {
  const api = useBoard();
  const [editing, setEditing] = useState<string | null>(null);
  const editable = !api.readOnly && !api.laneLocked(lane.truck_id);
  return (
    <div>
      <CompartmentGauge
        compartments={lane.compartments}
        allocations={load.allocations}
        preview={null}
        compact={false}
      />
      {load.allocations.length > 0 && (
        <table className="mt-2 w-full text-left text-xs">
          <caption className="sr-only">Compartment allocation</caption>
          <thead>
            <tr className="text-gray-600">
              <th scope="col" className="font-medium">
                Compartment
              </th>
              <th scope="col" className="font-medium">
                Order
              </th>
              <th scope="col" className="font-medium">
                Product
              </th>
              <th scope="col" className="text-right font-medium">
                Gallons
              </th>
              <th scope="col" className="text-right font-medium">
                Full
              </th>
            </tr>
          </thead>
          <tbody>
            {load.allocations.map((a, i) => (
              <tr key={`${a.compartment_id}-${a.order_id ?? ""}-${i}`}>
                <td>{compartmentName(lane, a.compartment_id)}</td>
                <td>{a.order_id ?? "—"}</td>
                <td>{a.product_code ?? "—"}</td>
                <td className="text-right">
                  {formatGallons(litersToGallons(a.liters))}
                </td>
                <td className="text-right">
                  {a.capacity_liters > 0
                    ? `${Math.round((a.liters / a.capacity_liters) * 100)}%`
                    : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <ul className="mt-2 space-y-1">
        {load.stops.map((s) => {
          const override = load.allocation_overrides[s.order_id];
          return (
            <li key={s.order_id} className="text-xs text-gray-800">
              <div className="flex items-center justify-between gap-2">
                <span>
                  Order {s.order_id}:{" "}
                  {override ? "your split" : "automatic split"}
                </span>
                {editable && (
                  <span className="flex gap-1">
                    <button
                      type="button"
                      className={button}
                      aria-label={`Edit the compartment split for Order ${s.order_id}`}
                      onClick={() => setEditing(s.order_id)}
                    >
                      Edit split
                    </button>
                    {override && (
                      <button
                        type="button"
                        className={button}
                        aria-label={`Reset Order ${s.order_id} to the automatic split`}
                        onClick={() =>
                          void api.command({
                            type: "set_allocation",
                            load_id: load.load_id,
                            order_id: s.order_id,
                            shares: null,
                          })
                        }
                      >
                        Reset
                      </button>
                    )}
                  </span>
                )}
              </div>
              {editing === s.order_id && (
                <OverrideEditor
                  lane={lane}
                  load={load}
                  orderId={s.order_id}
                  onDone={() => setEditing(null)}
                />
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

// ─── Terminal, sourcing, wait and contracts (R5.6) ──────────────────────────

/** The product with the most gallons on the load, and the load's gallons. */
function loadDemand(load: Load): { product: string | null; gallons: number } {
  const byProduct = new Map<string, number>();
  let gallons = 0;
  for (const s of load.stops) {
    const g = s.snapshot.gallons_requested ?? 0;
    gallons += g;
    const p = s.snapshot.product_code;
    if (p) byProduct.set(p, (byProduct.get(p) ?? 0) + g);
  }
  let product: string | null = null;
  let best = -1;
  for (const [p, g] of byProduct) {
    if (g > best) {
      best = g;
      product = p;
    }
  }
  return { product, gallons };
}

function WaitAndContracts({ terminalId }: { terminalId: string }) {
  const [wait, setWait] = useState<TerminalWaitSummary | null | "failed">(null);
  const [contracts, setContracts] = useState<
    SupplierContractResponse[] | null | "failed"
  >(null);
  useEffect(() => {
    let cancelled = false;
    setWait(null);
    setContracts(null);
    getTerminalWaitSummary(terminalId)
      .then((w) => !cancelled && setWait(w))
      .catch(() => !cancelled && setWait("failed"));
    listSupplierContracts({
      preferred_terminal_id: terminalId,
      status: "active",
    })
      .then((r) => !cancelled && setContracts(r.items))
      .catch(() => !cancelled && setContracts("failed"));
    return () => {
      cancelled = true;
    };
  }, [terminalId]);
  return (
    <div className="mt-2 space-y-1 text-xs text-gray-700">
      <p>
        {wait === null
          ? "Loading terminal wait…"
          : wait === "failed"
            ? "Terminal wait unavailable."
            : `Average wait ${Math.round(wait.avg_wait_minutes)} min over the last ${Math.round(wait.window_minutes / 60)} h${
                wait.wait_warning_exceeded
                  ? `, above the ${wait.wait_warning_threshold_minutes} min warning`
                  : ""
              }.`}
      </p>
      {contracts === null ? (
        <p>Loading supply contracts…</p>
      ) : contracts === "failed" ? (
        <p>Supply contracts unavailable.</p>
      ) : contracts.length === 0 ? (
        <p>No active supply contract for this terminal.</p>
      ) : (
        <ul aria-label="Supply contracts" className="space-y-0.5">
          {contracts.map(({ contract, lift_summary: lift }) => (
            <li key={contract.contract_id}>
              {contract.supplier_name}, {contract.product_code}:{" "}
              {formatGallons(lift.gallons_lifted_this_month)} lifted this month
              {lift.minimum_lift_gallons_per_month
                ? ` of ${formatGallons(lift.minimum_lift_gallons_per_month)} minimum`
                : ""}
              {lift.below_minimum ? " (below minimum)" : ""}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function TerminalSection({
  lane,
  load,
  terminals,
}: {
  lane: LaneView;
  load: Load;
  terminals: TerminalIndex;
}) {
  const api = useBoard();
  const id = useId();
  const [choice, setChoice] = useState(load.terminal_id ?? "");
  const [sourcing, setSourcing] = useState<
    SourcingTerminalCandidate[] | "loading" | "failed" | null
  >(null);
  useEffect(() => setChoice(load.terminal_id ?? ""), [load.terminal_id]);
  const editable = !api.readOnly && !api.laneLocked(lane.truck_id);
  const { product, gallons } = loadDemand(load);
  const origin = load.stops.find((s) => s.location)?.location ?? null;
  const nameOf = (tid: string) =>
    terminals.terminals.find((t) => t.terminal_id === tid)?.name ?? tid;

  const setTerminal = (terminalId: string | null) =>
    void api.command({
      type: "set_terminal",
      load_id: load.load_id,
      terminal_id: terminalId,
    });

  const recommend = () => {
    if (!product || !origin || gallons <= 0) return;
    setSourcing("loading");
    getSourcingRecommendations({
      product_code: product,
      volume_gallons: Math.round(gallons),
      origin_lat: origin.lat,
      origin_lon: origin.lon,
      truck_id: lane.truck_id,
    })
      .then((r) => setSourcing(r.candidates ?? []))
      .catch(() => setSourcing("failed"));
  };

  const sourcingReason = !product
    ? "Needs a product on the load"
    : !origin
      ? "Needs a stop with a location"
      : gallons <= 0
        ? "Needs gallons on the load"
        : null;

  return (
    <div className="mt-3">
      <div className="flex items-end gap-2">
        <label htmlFor={id} className="flex-1 text-xs text-gray-700">
          Terminal
          <select
            id={id}
            value={choice}
            disabled={!editable}
            onChange={(e) => setChoice(e.target.value)}
            className="mt-0.5 block w-full rounded-md border border-gray-300 px-1.5 py-1 text-sm"
          >
            <option value="">No terminal</option>
            {load.terminal_id &&
              !terminals.terminals.some(
                (t) => t.terminal_id === load.terminal_id,
              ) && <option value={load.terminal_id}>{load.terminal_id}</option>}
            {terminals.terminals.map((t) => (
              <option key={t.terminal_id} value={t.terminal_id}>
                {t.name}
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          className={primary}
          disabled={!editable || choice === (load.terminal_id ?? "")}
          onClick={() => setTerminal(choice || null)}
        >
          Set terminal
        </button>
      </div>
      {terminals.failed && (
        <p className="mt-1 text-[11px] text-gray-600">
          Terminal list unavailable.
        </p>
      )}
      {load.terminal_id && <WaitAndContracts terminalId={load.terminal_id} />}
      <div className="mt-2">
        <button
          type="button"
          className={button}
          disabled={Boolean(sourcingReason) || sourcing === "loading"}
          onClick={recommend}
          aria-describedby={sourcingReason ? `${id}-why` : undefined}
        >
          {sourcing === "loading"
            ? "Ranking terminals…"
            : "Recommend a terminal"}
        </button>
        {sourcingReason && (
          <span id={`${id}-why`} className="ml-2 text-[11px] text-gray-600">
            {sourcingReason}
          </span>
        )}
        {sourcing === "failed" && (
          <p className="mt-1 text-xs text-gray-600">
            Sourcing recommendations unavailable.
          </p>
        )}
        {Array.isArray(sourcing) &&
          (sourcing.length === 0 ? (
            <p className="mt-1 text-xs text-gray-600">
              No terminal can supply this load.
            </p>
          ) : (
            <ol aria-label="Recommended terminals" className="mt-1 space-y-1">
              {sourcing.map((c) => (
                <li
                  key={c.terminal_id}
                  className="flex items-start justify-between gap-2 text-xs"
                >
                  <span>
                    <span className="font-medium">{nameOf(c.terminal_id)}</span>
                    {` · $${c.price_per_gallon_usd.toFixed(3)}/gal · wait ${Math.round(c.avg_wait_minutes)} min · ${Math.round(c.distance_km_from_start)} km`}
                    {c.wait_warning ? " · long wait" : ""}
                    {c.reasons.length > 0 && (
                      <span className="block text-gray-600">
                        {c.reasons.join(". ")}
                      </span>
                    )}
                  </span>
                  {editable && c.terminal_id !== load.terminal_id && (
                    <button
                      type="button"
                      className={button}
                      aria-label={`Use ${nameOf(c.terminal_id)}`}
                      onClick={() => setTerminal(c.terminal_id)}
                    >
                      Use
                    </button>
                  )}
                </li>
              ))}
            </ol>
          ))}
      </div>
    </div>
  );
}

export function CompartmentsTab({
  lane,
  target,
  terminals,
}: {
  lane: LaneView;
  target: DrawerTarget;
  terminals: TerminalIndex;
}) {
  const loads = loadsInScope(lane, target);
  if (loads.length === 0) {
    return (
      <p className="text-sm text-gray-600">
        No loads yet. Add an order to plan compartments.
      </p>
    );
  }
  return (
    <div className="space-y-4">
      {loads.map((load) => {
        const n = lane.loads.indexOf(load) + 1;
        return (
          <section
            key={load.load_id}
            aria-label={`Load ${n}`}
            className="rounded-md border border-gray-200 p-2"
          >
            <h3 className="mb-1 text-sm font-semibold text-gray-900">
              Load {n}
            </h3>
            <AllocationSection lane={lane} load={load} />
            <TerminalSection lane={lane} load={load} terminals={terminals} />
          </section>
        );
      })}
    </div>
  );
}

export default CompartmentsTab;
