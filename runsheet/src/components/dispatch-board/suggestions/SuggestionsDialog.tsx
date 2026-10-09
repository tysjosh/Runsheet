/**
 * Agent suggestions review (K9; R16.1–R16.4, R16.7). Suggestions grouped by
 * truck, each with why (priority, runout or window, fill, agent) and the
 * diff against the lane's draft in the `ReplanDiff` vocabulary.
 *
 * - Accept per suggestion, per truck, or all: each is an `accept_suggestion`
 *   through the normal command path and validation; accepting never
 *   approves the agent's approval entry (R16.7).
 * - A refused accept marks the suggestion with its first blocking reason.
 *   When some of its loads pass, the dispatcher can confirm accepting just
 *   those (`load_ids`, R16.3).
 * - Reject takes an optional reason (≤ 500); the server also rejects a
 *   pending approval with it (R16.4).
 */
import { useId, useState } from "react";
import {
  type BoardSuggestion,
  type Check,
  rejectSuggestion,
} from "../../../services/dispatchBoardApi";
import { ReplanDiffBody } from "../../ops/ReplanDiffBody";
import { Button, Modal } from "../../ui";
import { useBoard } from "../BoardContext";
import { REASON_MAX } from "../drawer/ChecksTab";
import { announceBlocked } from "../state/announce";
import {
  diffOf,
  isAccepted,
  passingLoads,
  type SuggestionLoad,
  suggestionLoads,
  whyText,
} from "./suggestionModel";

export interface SuggestionsDialogProps {
  truckId?: string;
  suggestionId?: string;
  onClose: () => void;
  /** Toast + polite announcement. */
  notify: (text: string, kind: "success" | "error") => void;
  /** Refetch the snapshot (after a reject). */
  refresh: () => void;
}

interface Blocked {
  checks: Check[];
  passing: SuggestionLoad[];
}

function SuggestionCard({
  s,
  expanded,
  blocked,
  busy,
  onAccept,
  onReject,
}: {
  s: BoardSuggestion;
  expanded: boolean;
  blocked: Blocked | undefined;
  busy: boolean;
  onAccept: (loadKeys?: string[]) => void;
  onReject: (reason: string) => void;
}) {
  const api = useBoard();
  const id = useId();
  const [open, setOpen] = useState(expanded);
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const accepted = isAccepted(s, api.lanesById);
  const loads = suggestionLoads(s);
  const stops = loads.reduce((n, l) => n + l.orderIds.length, 0);
  const why =
    s.why && typeof s.why === "object" && !Array.isArray(s.why) ? s.why : null;
  const diff = diffOf(s);
  const disabled = api.readOnly || busy;
  const partial =
    blocked &&
    blocked.passing.length > 0 &&
    blocked.passing.length < loads.length;
  return (
    <li className="rounded-md border border-gray-200 p-3">
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium text-gray-900">
            {loads.length === 1 ? "1 load" : `${loads.length} loads`}, {stops}{" "}
            {stops === 1 ? "stop" : "stops"}
            <span className="font-normal text-gray-600">
              {" "}
              · {s.agent_name}
              {s.route_agent_name ? ` and ${s.route_agent_name}` : ""}
            </span>
          </p>
          <p className="text-sm text-gray-700">{whyText(s)}</p>
          {accepted && (
            <p className="text-xs font-medium text-success-dark">
              Accepted onto the board.
            </p>
          )}
          {blocked && (
            <p className="text-xs font-medium text-error-dark">
              {announceBlocked(blocked.checks)}
            </p>
          )}
        </div>
        <button
          type="button"
          aria-expanded={open}
          aria-controls={`${id}-details`}
          onClick={() => setOpen((o) => !o)}
          className="min-h-7 shrink-0 rounded-md px-2 text-xs font-medium text-primary hover:bg-gray-50"
        >
          {open ? "Hide details" : "Why and changes"}
        </button>
      </div>
      {open && (
        <div id={`${id}-details`} className="mt-2 space-y-2">
          {why && (
            <dl className="grid grid-cols-[9rem_1fr] gap-x-2 text-xs">
              {why.priority_bucket != null || why.priority_score != null ? (
                <>
                  <dt className="text-gray-600">Priority</dt>
                  <dd>
                    {String(why.priority_bucket ?? "").replace(/_/g, " ")}
                    {typeof why.priority_score === "number"
                      ? ` (${Math.round(why.priority_score)})`
                      : ""}
                  </dd>
                </>
              ) : null}
              {typeof why.runout_hours === "number" && (
                <>
                  <dt className="text-gray-600">Tank runs out in</dt>
                  <dd>{Math.round(why.runout_hours)} h</dd>
                </>
              )}
              {typeof why.fill_pct === "number" && (
                <>
                  <dt className="text-gray-600">Truck fill</dt>
                  <dd>{Math.round(why.fill_pct)}%</dd>
                </>
              )}
              <dt className="text-gray-600">Orders</dt>
              <dd>{loads.flatMap((l) => l.orderIds).join(", ") || "None"}</dd>
            </dl>
          )}
          {diff && (
            <section aria-label="Changes to the board">
              <h4 className="mb-1 text-xs font-semibold text-gray-700">
                Changes to Truck {s.truck_id}
              </h4>
              <ReplanDiffBody diff={diff} />
            </section>
          )}
        </div>
      )}
      {!accepted && (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          {partial && blocked ? (
            <>
              <span className="text-xs text-gray-700">
                Accept only the {blocked.passing.length}{" "}
                {blocked.passing.length === 1
                  ? "load that passes"
                  : "loads that pass"}
                ?
              </span>
              <Button
                size="sm"
                variant="primary"
                disabled={disabled}
                onClick={() => onAccept(blocked.passing.map((l) => l.loadKey))}
              >
                Accept {blocked.passing.length}{" "}
                {blocked.passing.length === 1 ? "load" : "loads"}
              </Button>
            </>
          ) : (
            <Button
              size="sm"
              variant="primary"
              disabled={disabled || Boolean(blocked)}
              onClick={() => onAccept()}
              aria-label={`Accept the suggestion for Truck ${s.truck_id}`}
            >
              Accept
            </Button>
          )}
          {!rejecting ? (
            <Button
              size="sm"
              variant="ghost"
              disabled={disabled}
              onClick={() => setRejecting(true)}
              aria-label={`Reject the suggestion for Truck ${s.truck_id}`}
            >
              Reject…
            </Button>
          ) : (
            <div className="flex w-full items-end gap-2">
              <label
                htmlFor={`${id}-reason`}
                className="flex-1 text-xs text-gray-700"
              >
                Reason (optional)
                <input
                  id={`${id}-reason`}
                  type="text"
                  maxLength={REASON_MAX}
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  className="mt-0.5 block w-full rounded-md border border-gray-300 px-1.5 py-1 text-sm"
                />
              </label>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => setRejecting(false)}
              >
                Cancel
              </Button>
              <Button
                size="sm"
                variant="danger"
                disabled={disabled}
                onClick={() => onReject(reason)}
              >
                Reject
              </Button>
            </div>
          )}
        </div>
      )}
    </li>
  );
}

export function SuggestionsDialog({
  truckId,
  suggestionId,
  onClose,
  notify,
  refresh,
}: SuggestionsDialogProps) {
  const api = useBoard();
  const [blocked, setBlocked] = useState<Record<string, Blocked>>({});
  const [busy, setBusy] = useState(false);
  const all = api.snapshot.suggestions;
  const shown = truckId ? all.filter((s) => s.truck_id === truckId) : all;
  const byTruck = new Map<string, BoardSuggestion[]>();
  for (const s of shown) {
    byTruck.set(s.truck_id, [...(byTruck.get(s.truck_id) ?? []), s]);
  }
  const open = (list: BoardSuggestion[]) =>
    list.filter(
      (s) => !isAccepted(s, api.lanesById) && !blocked[s.suggestion_id],
    );

  /** One accept; returns whether it committed. */
  const acceptOne = async (s: BoardSuggestion, loadKeys?: string[]) => {
    const outcome = await api.command(
      {
        type: "accept_suggestion",
        suggestion_id: s.suggestion_id,
        load_ids: loadKeys ?? null,
      },
      "suggestion",
    );
    if (outcome?.kind === "committed") {
      setBlocked((b) => {
        const { [s.suggestion_id]: _gone, ...rest } = b;
        return rest;
      });
      return true;
    }
    if (outcome?.kind === "blocked") {
      setBlocked((b) => ({
        ...b,
        [s.suggestion_id]: {
          checks: outcome.checks,
          passing: loadKeys ? [] : passingLoads(s, outcome.checks),
        },
      }));
    }
    return false;
  };

  const acceptMany = async (list: BoardSuggestion[]) => {
    setBusy(true);
    let ok = 0;
    for (const s of list) {
      if (await acceptOne(s)) ok += 1;
    }
    setBusy(false);
    const refused = list.length - ok;
    notify(
      refused === 0
        ? `Accepted ${ok} ${ok === 1 ? "suggestion" : "suggestions"}.`
        : `Accepted ${ok} of ${list.length} suggestions. ${refused} ${refused === 1 ? "is" : "are"} blocked and marked.`,
      refused === 0 ? "success" : "error",
    );
  };

  const reject = async (s: BoardSuggestion, reason: string) => {
    setBusy(true);
    try {
      await rejectSuggestion(api.serviceDate, s.plan_id, reason);
      notify(`Suggestion for Truck ${s.truck_id} rejected.`, "success");
      refresh();
    } catch {
      notify(
        `Couldn't reject the suggestion for Truck ${s.truck_id}.`,
        "error",
      );
    } finally {
      setBusy(false);
    }
  };

  const pendingAll = open(shown);
  return (
    <Modal
      isOpen
      onClose={onClose}
      title={truckId ? `Suggestions for Truck ${truckId}` : "Agent suggestions"}
      size="xl"
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Close
          </Button>
          {!truckId && (
            <Button
              variant="primary"
              disabled={api.readOnly || busy || pendingAll.length === 0}
              onClick={() => void acceptMany(pendingAll)}
            >
              Accept all ({pendingAll.length})
            </Button>
          )}
        </>
      }
    >
      {shown.length === 0 ? (
        <p className="text-sm text-gray-600">
          No open suggestions for this day. Use Generate plan to ask the agents
          for one.
        </p>
      ) : (
        <div className="space-y-4">
          {[...byTruck.entries()].map(([truck, list]) => {
            const pending = open(list);
            return (
              <section key={truck} aria-label={`Truck ${truck} suggestions`}>
                <div className="mb-1 flex items-center justify-between gap-2">
                  <h3 className="text-sm font-semibold text-gray-900">
                    Truck {truck}
                    {!api.lanesById[truck] && (
                      <span className="ml-1 text-xs font-normal text-gray-600">
                        (not on the board yet; accepting adds it)
                      </span>
                    )}
                  </h3>
                  {list.length > 1 && (
                    <Button
                      size="sm"
                      variant="secondary"
                      disabled={api.readOnly || busy || pending.length === 0}
                      onClick={() => void acceptMany(pending)}
                    >
                      Accept all for Truck {truck}
                    </Button>
                  )}
                </div>
                <ul className="space-y-2">
                  {list.map((s) => (
                    <SuggestionCard
                      key={s.suggestion_id}
                      s={s}
                      expanded={s.suggestion_id === suggestionId}
                      blocked={blocked[s.suggestion_id]}
                      busy={busy}
                      onAccept={(keys) => {
                        setBusy(true);
                        void acceptOne(s, keys).finally(() => setBusy(false));
                      }}
                      onReject={(reason) => void reject(s, reason)}
                    />
                  ))}
                </ul>
              </section>
            );
          })}
        </div>
      )}
    </Modal>
  );
}

export default SuggestionsDialog;
