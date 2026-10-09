/**
 * Publish review (design K7.1; R12.1–R12.3, R12.5, R13.7). Built from the
 * server's dry run (`previewPublish`), which makes no writes:
 *
 * - called when the dialog opens and when the lane selection changes,
 *   debounced 400 ms; a selection change aborts the earlier call, so only
 *   the last completed response for the current selection is ever shown;
 * - never called while a reason is typed: a warning is satisfied once its
 *   reason is 3–500 characters after trimming, and the real publish checks
 *   every reason again;
 * - per lane: driver to notify, loads (with "may already be loading"), the
 *   orders that become dispatched, not-ready reasons, lanes added to a
 *   re-publish group, what each driver will be told, and each open warning
 *   with its reason field. Publish stays disabled until all are satisfied.
 *
 * After Publish the dialog shows per-lane progress from the parent (socket
 * and polling) until the user closes it.
 */
import { useEffect, useId, useMemo, useRef, useState } from "react";
import {
  boardErrorCode,
  isAbortError,
  newClientId,
  type PublishAccepted,
  type PublishLaneRef,
  type PublishPreview,
  type PublishStatus,
  previewPublish,
  publishBoard,
} from "../../../services/dispatchBoardApi";
import { Button, Modal } from "../../ui";
import { useBoard } from "../BoardContext";
import { REASON_MAX, reasonComplete } from "../drawer/ChecksTab";
import { LANE_STATE_LABEL } from "../grid/LaneHeader";
import {
  LOAD_CLASS_TEXT,
  LOAD_INFO_TEXT,
  notReadyText,
  PUBLISHABLE_STATES,
} from "./publishText";

export const PREVIEW_DEBOUNCE_MS = 400;

const ALREADY_DISPATCHED = new Set([
  "dispatched",
  "in_transit",
  "delivered",
  "failed",
]);

export interface PublishDialogProps {
  initialTruckIds: string[];
  onClose: () => void;
  /** The 202: the parent tracks progress (socket + polling). */
  onAccepted: (accepted: PublishAccepted) => void;
  /** Progress of the accepted publish. */
  progress: PublishStatus | null;
}

interface Preview {
  key: string;
  lanes: PublishLaneRef[];
  data: PublishPreview;
}

const STATE_TEXT: Record<string, string> = {
  queued: "Queued",
  publishing: "Publishing…",
  published: "Published",
  failed: "Publish failed",
  recovering: "Recovering",
  already_published: "Already published",
};

export function PublishDialog({
  initialTruckIds,
  onClose,
  onAccepted,
  progress,
}: PublishDialogProps) {
  const api = useBoard();
  const id = useId();
  const [selected, setSelected] = useState<string[]>(initialTruckIds);
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const [preview, setPreview] = useState<Preview | null>(null);
  const [pending, setPending] = useState(true);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [accepted, setAccepted] = useState<PublishAccepted | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const lanesRef = useRef(api.lanesById);
  lanesRef.current = api.lanesById;

  const choices = useMemo(() => {
    const ids = new Set(initialTruckIds);
    for (const lane of Object.values(api.lanesById)) {
      if (PUBLISHABLE_STATES.has(lane.state)) ids.add(lane.truck_id);
    }
    return [...ids].sort();
  }, [api.lanesById, initialTruckIds]);

  const key = [...selected].sort().join(",");

  // Dry run on open and on selection change only (K7.1).
  useEffect(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    if (accepted) return;
    if (selected.length === 0) {
      setPending(false);
      return;
    }
    setPending(true);
    setPreviewError(null);
    const timer = setTimeout(() => {
      const controller = new AbortController();
      abortRef.current = controller;
      const lanes = [...selected].sort().map((t) => ({
        truck_id: t,
        expected_version: lanesRef.current[t]?.version ?? 0,
      }));
      previewPublish(api.serviceDate, { lanes }, controller.signal)
        .then((data) => {
          if (controller.signal.aborted) return;
          setPreview({ key, lanes, data });
          setPending(false);
        })
        .catch((err) => {
          if (isAbortError(err) || controller.signal.aborted) return;
          setPreviewError("The review couldn't be loaded. Try again.");
          setPending(false);
        });
    }, PREVIEW_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [key, refresh, accepted, api.serviceDate]);
  useEffect(() => () => abortRef.current?.abort(), []);

  const data = preview && preview.key === key ? preview.data : null;
  const warnings = data?.open_warnings ?? [];
  const missingReason = (truckId?: string) =>
    warnings.some(
      (w) =>
        (!truckId || w.truck_id === truckId) &&
        !reasonComplete(reasons[w.warning_id]),
    );
  const laneReasons = (truckId: string): string[] =>
    (data?.not_ready.find((n) => n.truck_id === truckId)?.reasons ?? []).filter(
      (r) => r !== "warning_unacknowledged" || missingReason(truckId),
    );
  const groupLanes = data
    ? [
        ...new Set([
          ...selected,
          ...data.groups.flatMap((g) => g.truck_ids),
          ...data.not_ready.map((n) => n.truck_id),
        ]),
      ].sort()
    : [];
  const blocked = groupLanes.filter((t) => laneReasons(t).length > 0);
  const toPublish = groupLanes.filter(
    (t) => !data?.already_published.includes(t),
  );
  const canPublish =
    Boolean(data) &&
    !pending &&
    !submitting &&
    blocked.length === 0 &&
    !missingReason() &&
    toPublish.length > 0;

  // One request id per reviewed body; a network retry of the same body reuses it.
  const bodyKey = `${key}|${JSON.stringify(reasons)}`;
  const requestId = useRef<{ body: string; id: string } | null>(null);

  const submit = async () => {
    if (!preview || !canPublish) return;
    if (requestId.current?.body !== bodyKey) {
      requestId.current = { body: bodyKey, id: newClientId() };
    }
    const warningReasons: Record<string, string> = {};
    for (const w of warnings)
      warningReasons[w.warning_id] = reasons[w.warning_id].trim();
    setSubmitting(true);
    setSubmitError(null);
    try {
      const res = await publishBoard(api.serviceDate, {
        client_request_id: requestId.current.id,
        lanes: preview.lanes,
        warning_reasons: warningReasons,
      });
      setAccepted(res);
      onAccepted(res);
    } catch (err) {
      const code = boardErrorCode(err);
      if (
        code === "BOARD_PUBLISH_NOT_READY" ||
        code === "BOARD_LANE_CONFLICT"
      ) {
        setSubmitError(
          "Something changed since this review. It has been refreshed; check it and publish again.",
        );
        requestId.current = null;
        setRefresh((n) => n + 1);
      } else {
        setSubmitError(
          err instanceof Error && err.message
            ? `Not published. ${err.message}`
            : "Not published. Check your connection and try again.",
        );
      }
    } finally {
      setSubmitting(false);
    }
  };

  const toggle = (truckId: string) =>
    setSelected((s) =>
      s.includes(truckId) ? s.filter((t) => t !== truckId) : [...s, truckId],
    );

  // ── Progress view ─────────────────────────────────────────────────────────
  if (accepted) {
    const live = progress?.publish_id === accepted.publish_id ? progress : null;
    const states: Record<string, string> = {};
    for (const l of accepted.lanes) states[l.truck_id] = l.state;
    for (const l of live?.lanes ?? []) states[l.truck_id] = l.state;
    const done =
      live?.done ??
      accepted.lanes.every((l) => l.state === "already_published");
    return (
      <Modal
        isOpen
        onClose={onClose}
        title={done ? "Publish finished" : "Publishing"}
        size="lg"
        footer={
          <Button variant="primary" onClick={onClose}>
            Close
          </Button>
        }
      >
        <ul aria-label="Publish progress" className="space-y-1 text-sm">
          {Object.entries(states).map(([truckId, state]) => {
            const result = live?.lanes.find(
              (l) => l.truck_id === truckId,
            )?.last_result;
            return (
              <li key={truckId} className="flex justify-between gap-2">
                <span>Truck {truckId}</span>
                <span
                  className={
                    state === "failed" || state === "recovering"
                      ? "font-medium text-error-dark"
                      : "text-gray-700"
                  }
                >
                  {STATE_TEXT[state] ?? state}
                  {result?.reason &&
                  (state === "failed" || state === "recovering")
                    ? ` (${result.reason.replace(/_/g, " ")})`
                    : ""}
                </span>
              </li>
            );
          })}
        </ul>
        {!done && (
          <p className="mt-2 text-xs text-gray-600">
            You can close this; the board keeps publishing and shows the result
            on each truck.
          </p>
        )}
      </Modal>
    );
  }

  // ── Review view ───────────────────────────────────────────────────────────
  return (
    <Modal
      isOpen
      onClose={onClose}
      title="Review and publish"
      size="xl"
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="primary"
            onClick={() => void submit()}
            disabled={!canPublish}
            loading={submitting}
          >
            {toPublish.length > 1
              ? `Publish ${toPublish.length} trucks`
              : "Publish"}
          </Button>
        </>
      }
    >
      <fieldset className="mb-3">
        <legend className="text-sm font-medium text-gray-900">
          Trucks to publish
        </legend>
        <div className="mt-1 flex flex-wrap gap-2">
          {choices.map((t) => {
            const lane = api.lanesById[t];
            return (
              <label
                key={t}
                className="inline-flex min-h-8 items-center gap-1.5 rounded-md border border-gray-300 px-2 text-sm"
              >
                <input
                  type="checkbox"
                  checked={selected.includes(t)}
                  onChange={() => toggle(t)}
                />
                Truck {t}
                {lane && (
                  <span className="text-xs text-gray-600">
                    {LANE_STATE_LABEL[lane.state].label}
                  </span>
                )}
              </label>
            );
          })}
        </div>
      </fieldset>

      <div aria-live="polite" className="text-sm">
        {selected.length === 0 ? (
          <p className="text-gray-600">Choose at least one truck.</p>
        ) : pending ? (
          <p role="status" className="text-gray-600">
            Checking what will be sent…
          </p>
        ) : previewError ? (
          <p className="text-error-dark">{previewError}</p>
        ) : null}
      </div>
      {submitError && (
        <p role="alert" className="mt-1 text-sm text-error-dark">
          {submitError}
        </p>
      )}

      {data && !pending && (
        <div className="mt-2 space-y-3">
          {blocked.length > 0 && (
            <p className="text-sm font-medium text-error-dark">
              {blocked.length === 1
                ? `Truck ${blocked[0]} isn't ready. Fix it or remove it from this publish.`
                : `${blocked.length} trucks aren't ready. Fix them or remove them from this publish.`}
            </p>
          )}
          {groupLanes.map((t) => {
            const lane = api.lanesById[t];
            const loads = data.loads.filter((l) => l.truck_id === t);
            const added = data.groups.some((g) => g.added_lanes.includes(t));
            const reasonsFor = laneReasons(t);
            const driver = lane?.driver?.name ?? lane?.driver_id ?? null;
            const orders = (lane?.loads ?? [])
              .flatMap((l) => l.stops)
              .filter((s) => !ALREADY_DISPATCHED.has(s.snapshot.status ?? ""))
              .map((s) => s.order_id);
            const laneWarnings = warnings.filter((w) => w.truck_id === t);
            const already = data.already_published.includes(t);
            return (
              <section
                key={t}
                aria-label={`Truck ${t}`}
                className="rounded-md border border-gray-200 p-3"
              >
                <h3 className="text-sm font-semibold text-gray-900">
                  Truck {t}
                </h3>
                {added && (
                  <p className="text-xs text-info-dark">
                    Added to this publish because a dispatched order moved to or
                    from it.
                  </p>
                )}
                {already ? (
                  <p className="text-xs text-gray-600">
                    Already published. Nothing to send.
                  </p>
                ) : (
                  <dl className="mt-1 grid grid-cols-[8rem_1fr] gap-x-2 gap-y-0.5 text-xs">
                    <dt className="text-gray-600">Driver to notify</dt>
                    <dd>{driver ?? "No driver"}</dd>
                    <dt className="text-gray-600">Orders to dispatch</dt>
                    <dd>{orders.length ? orders.join(", ") : "None"}</dd>
                  </dl>
                )}
                {loads.length > 0 && (
                  <ul
                    aria-label={`Loads on Truck ${t}`}
                    className="mt-1 space-y-0.5 text-xs"
                  >
                    {loads.map((l) => {
                      const n =
                        (lane?.loads.findIndex(
                          (x) => x.load_id === l.load_id,
                        ) ?? -1) + 1;
                      return (
                        <li key={l.load_id}>
                          <span className="font-medium">
                            {n > 0 ? `Load ${n}` : "Load"}
                          </span>
                          : {LOAD_CLASS_TEXT[l.class] ?? l.class}
                          {l.info.map((i) => (
                            <span
                              key={i}
                              className="mt-0.5 block text-warning-dark"
                            >
                              {LOAD_INFO_TEXT[i] ?? i.replace(/_/g, " ")}
                            </span>
                          ))}
                        </li>
                      );
                    })}
                  </ul>
                )}
                {reasonsFor.length > 0 && (
                  <ul
                    aria-label={`Why Truck ${t} isn't ready`}
                    className="mt-1 list-disc pl-4 text-xs text-error-dark"
                  >
                    {reasonsFor.map((r) => (
                      <li key={r}>{notReadyText(r, lane)}</li>
                    ))}
                  </ul>
                )}
                {laneWarnings.map((w) => {
                  const fieldId = `${id}-${w.warning_id}`;
                  const value = reasons[w.warning_id] ?? "";
                  return (
                    <div key={w.warning_id} className="mt-2">
                      <label
                        htmlFor={fieldId}
                        className="block text-xs font-medium text-gray-800"
                      >
                        Reason for accepting: {w.message}
                      </label>
                      <textarea
                        id={fieldId}
                        rows={2}
                        maxLength={REASON_MAX}
                        value={value}
                        onChange={(e) =>
                          setReasons((r) => ({
                            ...r,
                            [w.warning_id]: e.target.value,
                          }))
                        }
                        aria-invalid={value !== "" && !reasonComplete(value)}
                        aria-describedby={`${fieldId}-hint`}
                        className="mt-0.5 w-full rounded-md border border-gray-300 p-1.5 text-sm"
                      />
                      <span
                        id={`${fieldId}-hint`}
                        className="text-[11px] text-gray-600"
                      >
                        3–500 characters
                      </span>
                    </div>
                  );
                })}
              </section>
            );
          })}
          {data.notifications.length > 0 && (
            <section aria-label="What drivers will be told">
              <h3 className="text-sm font-semibold text-gray-900">
                What drivers will be told
              </h3>
              <ul className="mt-1 space-y-0.5 text-xs">
                {data.notifications.map((n) => {
                  const parts = [
                    n.assign_order_ids.length
                      ? `gets ${n.assign_order_ids.join(", ")}`
                      : "",
                    n.revoke_order_ids.length
                      ? `loses ${n.revoke_order_ids.join(", ")}`
                      : "",
                    n.route_updated ? "route updated" : "",
                  ].filter(Boolean);
                  return (
                    <li key={n.driver_id}>
                      <span className="font-medium">
                        {n.name ?? n.driver_id}
                      </span>
                      : {parts.length ? parts.join(", ") : "no change"}
                    </li>
                  );
                })}
              </ul>
            </section>
          )}
        </div>
      )}
    </Modal>
  );
}

export default PublishDialog;
