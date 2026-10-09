/**
 * Drawer Checks tab (R8.7, R9.3, R9.4, R9.6, R10.5). Every check for the
 * selected lane, load or stop, grouped by outcome, each with its reason and
 * the record that fixes it:
 *
 * - driver, certification and order records open their pages; a blocking HOS
 *   gate links to the HOS override flow, the only way to lift it (R9.6);
 * - compartment and terminal fixes open the Compartments tab;
 * - "Move it back" sends the stop to the load named by the fix (K8.5 rule 11e);
 * - `order_identity_changed` offers "Remove and re-add" (R10.5).
 *
 * Open warnings take an acknowledgement with a 3–500 character reason,
 * inline (R9.3, R9.4: never a dialog).
 */
import { useEffect, useId, useRef, useState } from "react";
import type {
  Check,
  CheckOutcome,
  LaneView,
} from "../../../services/dispatchBoardApi";
import { entityHref } from "../../ui";
import { type DrawerTab, type DrawerTarget, useBoard } from "../BoardContext";
import { CheckChip } from "../CheckChip";

export const REASON_MIN = 3;
export const REASON_MAX = 500;

/** A reason is complete at 3–500 characters after trimming (R9.3). */
export function reasonComplete(reason: string | undefined): boolean {
  const n = (reason ?? "").trim().length;
  return n >= REASON_MIN && n <= REASON_MAX;
}

const GROUPS: { outcome: CheckOutcome; title: string }[] = [
  { outcome: "block", title: "Blocking" },
  { outcome: "warn", title: "Warnings" },
  { outcome: "info", title: "Information" },
  { outcome: "pass", title: "Passed" },
];

/** Checks in the drawer's scope: the whole lane, one load, or one stop. */
export function checksInScope(lane: LaneView, target: DrawerTarget): Check[] {
  if (target.orderId) {
    const load = lane.loads.find((l) =>
      l.stops.some((s) => s.order_id === target.orderId),
    );
    return lane.checks.filter(
      (c) =>
        c.scope.order_id === target.orderId ||
        (!c.scope.order_id &&
          (!c.scope.load_id || c.scope.load_id === load?.load_id)),
    );
  }
  if (target.loadId) {
    const ids = new Set(
      lane.loads
        .find((l) => l.load_id === target.loadId)
        ?.stops.map((s) => s.order_id) ?? [],
    );
    return lane.checks.filter(
      (c) =>
        c.scope.load_id === target.loadId ||
        (c.scope.order_id ? ids.has(c.scope.order_id) : !c.scope.load_id),
    );
  }
  return lane.checks;
}

function AckForm({
  lane,
  check,
  onDone,
}: {
  lane: LaneView;
  check: Check;
  onDone: () => void;
}) {
  const api = useBoard();
  const id = useId();
  const [reason, setReason] = useState("");
  const [saving, setSaving] = useState(false);
  const field = useRef<HTMLTextAreaElement>(null);
  useEffect(() => field.current?.focus(), []);
  const length = reason.trim().length;
  const ok = reasonComplete(reason);
  const save = async () => {
    if (!ok || !check.warning_id) return;
    setSaving(true);
    const outcome = await api.command({
      type: "acknowledge_warning",
      truck_id: lane.truck_id,
      warning_id: check.warning_id,
      reason: reason.trim(),
    });
    setSaving(false);
    if (outcome?.kind === "committed") onDone();
  };
  return (
    <div className="mt-2 rounded-md border border-gray-200 bg-gray-50 p-2">
      <label htmlFor={id} className="block text-xs font-medium text-gray-800">
        Reason for accepting this warning
      </label>
      <textarea
        ref={field}
        id={id}
        value={reason}
        maxLength={REASON_MAX}
        rows={2}
        aria-describedby={`${id}-hint`}
        onChange={(e) => setReason(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            e.stopPropagation();
            onDone();
          }
        }}
        className="mt-1 w-full rounded-md border border-gray-300 p-1.5 text-sm"
      />
      <div className="mt-1 flex items-center justify-between gap-2">
        <span id={`${id}-hint`} className="text-[11px] text-gray-600">
          {REASON_MIN}–{REASON_MAX} characters ({length})
        </span>
        <div className="flex gap-1">
          <button
            type="button"
            onClick={onDone}
            className="min-h-7 rounded-md px-2 text-xs text-gray-700 hover:bg-gray-100"
          >
            Cancel
          </button>
          <button
            type="button"
            disabled={!ok || saving}
            onClick={() => void save()}
            className="min-h-7 rounded-md bg-primary px-2 text-xs font-medium text-white disabled:cursor-not-allowed disabled:opacity-50"
          >
            {saving ? "Saving…" : "Acknowledge"}
          </button>
        </div>
      </div>
    </div>
  );
}

function FixAction({
  lane,
  check,
  onTab,
}: {
  lane: LaneView;
  check: Check;
  onTab: (tab: DrawerTab) => void;
}) {
  const api = useBoard();
  const link = "text-xs font-medium text-primary underline";
  const button =
    "min-h-7 rounded-md border border-gray-300 px-2 text-xs font-medium text-gray-800 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50";
  const orderId = check.scope.order_id ?? null;

  if (check.reason_code === "order_identity_changed" && orderId) {
    const reAdd = async () => {
      const load = lane.loads.find((l) =>
        l.stops.some((s) => s.order_id === orderId),
      );
      const removed = await api.command({
        type: "unassign_orders",
        order_ids: [orderId],
      });
      if (removed?.kind !== "committed") return;
      const after = removed.response.lanes.find(
        (l) => l.truck_id === lane.truck_id,
      );
      const loadStays = after?.loads.some((l) => l.load_id === load?.load_id);
      await api.command({
        type: "assign_orders",
        order_ids: [orderId],
        truck_id: lane.truck_id,
        target: { load_id: loadStays ? (load?.load_id ?? null) : null },
      });
    };
    return (
      <button
        type="button"
        className={button}
        disabled={api.readOnly}
        onClick={() => void reAdd()}
      >
        Remove and re-add
      </button>
    );
  }

  const fix = check.fix_link;
  if (!fix) return null;
  switch (fix.kind) {
    case "driver":
      return (
        <a className={link} href={entityHref("driver", fix.id)}>
          Open driver profile
        </a>
      );
    case "asset":
      return (
        <a className={link} href={entityHref("asset", fix.id)}>
          Open truck certification
        </a>
      );
    case "order":
      return (
        <a className={link} href={entityHref("order", fix.id)}>
          Open order
        </a>
      );
    case "hos_override":
      // The existing HOS override flow is on the driver's record (R9.6).
      return (
        <a className={link} href={entityHref("driver", fix.id)}>
          Request an HOS override
        </a>
      );
    case "compartment":
      return (
        <button
          type="button"
          className={button}
          onClick={() => onTab("compartments")}
        >
          Edit compartment split
        </button>
      );
    case "terminal":
      return (
        <button
          type="button"
          className={button}
          onClick={() => onTab("compartments")}
        >
          Choose a terminal
        </button>
      );
    case "move_back": {
      const id = orderId ?? fix.id;
      const to = fix.truck_id ?? lane.truck_id;
      return (
        <button
          type="button"
          className={button}
          disabled={api.readOnly || !fix.load_id}
          onClick={() =>
            fix.load_id &&
            api.perform(
              { kind: "stop", ids: [id], fromTruckId: lane.truck_id },
              { target: "load", truckId: to, loadId: fix.load_id, index: null },
              "menu",
            )
          }
        >
          Move it back to Truck {to}
        </button>
      );
    }
    default:
      return null;
  }
}

function CheckRow({
  lane,
  check,
  onTab,
}: {
  lane: LaneView;
  check: Check;
  onTab: (tab: DrawerTab) => void;
}) {
  const api = useBoard();
  const [acking, setAcking] = useState(false);
  const ack = check.warning_id
    ? api.snapshot.acknowledged[check.warning_id]
    : undefined;
  const canAck =
    check.outcome === "warn" &&
    Boolean(check.warning_id) &&
    !ack &&
    !api.readOnly &&
    !api.laneLocked(lane.truck_id);
  const where = check.scope.order_id ? `Order ${check.scope.order_id}` : null;
  return (
    <li className="py-2">
      <div className="flex items-start gap-2">
        <CheckChip outcome={check.outcome} label={shortOutcome(check)} />
        <div className="min-w-0 flex-1">
          <p className="text-sm text-gray-900">
            {where && <span className="font-medium">{where}: </span>}
            {check.message}
          </p>
          {ack && (
            <p className="mt-0.5 text-xs text-gray-600">
              Acknowledged: {ack.reason}
            </p>
          )}
          <div className="mt-1 flex flex-wrap items-center gap-2">
            <FixAction lane={lane} check={check} onTab={onTab} />
            {canAck && !acking && (
              <button
                type="button"
                onClick={() => setAcking(true)}
                className="min-h-7 rounded-md border border-warning px-2 text-xs font-medium text-warning-dark hover:bg-warning-light"
              >
                Acknowledge…
              </button>
            )}
          </div>
          {acking && (
            <AckForm
              lane={lane}
              check={check}
              onDone={() => setAcking(false)}
            />
          )}
        </div>
      </div>
    </li>
  );
}

function shortOutcome(check: Check): string {
  switch (check.outcome) {
    case "block":
      return "Blocked";
    case "warn":
      return "Warning";
    case "info":
      return "Info";
    default:
      return "OK";
  }
}

export function ChecksTab({
  lane,
  target,
  onTab,
}: {
  lane: LaneView;
  target: DrawerTarget;
  onTab: (tab: DrawerTab) => void;
}) {
  const checks = checksInScope(lane, target);
  if (checks.length === 0) {
    return (
      <p className="text-sm text-gray-600">
        No checks yet. They appear once this truck has work.
      </p>
    );
  }
  return (
    <div className="space-y-3">
      {lane.checks_stale && (
        <p className="text-xs text-gray-600">
          Something changed outside the board. Checks refresh on the next
          change.
        </p>
      )}
      {GROUPS.map(({ outcome, title }) => {
        const group = checks.filter((c) => c.outcome === outcome);
        if (group.length === 0) return null;
        return (
          <section key={outcome} aria-label={`${title} (${group.length})`}>
            <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-600">
              {title} ({group.length})
            </h3>
            <ul className="divide-y divide-gray-100">
              {group.map((c, i) => (
                <CheckRow
                  key={`${c.check}-${c.reason_code}-${c.scope.order_id ?? ""}-${i}`}
                  lane={lane}
                  check={c}
                  onTab={onTab}
                />
              ))}
            </ul>
          </section>
        );
      })}
    </div>
  );
}

export default ChecksTab;
