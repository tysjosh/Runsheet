/**
 * Failed and Recovering publishes, one banner per publish group (R12.6,
 * R13.10, K8.4 Recovery, K14.7):
 *
 * - forward recovery (lanes read-only): assertive "{n} drivers can't see
 *   their routes until this publish completes", Retry resends the group;
 * - rolled back (lanes editable): "Your previous route is still with the
 *   driver", Retry reviews the group again;
 * - any other failure: the stage and whether anything was saved, and Retry.
 *
 * Orders the publish had to drop are named (drop rule, E29).
 */
import { AlertTriangle, RotateCcw } from "lucide-react";
import type { LaneView } from "../../../services/dispatchBoardApi";
import { useBoard } from "../BoardContext";
import { failureText, retryGroup, trucksText } from "./publishText";

interface GroupBanner {
  key: string;
  truckIds: string[];
  lead: LaneView;
}

function groups(lanes: LaneView[]): GroupBanner[] {
  const out = new Map<string, GroupBanner>();
  for (const lane of lanes) {
    if (lane.state !== "failed" && lane.state !== "recovering") continue;
    const truckIds = retryGroup(lane).sort();
    const key = truckIds.join(",");
    if (!out.has(key)) out.set(key, { key, truckIds, lead: lane });
  }
  return [...out.values()];
}

export function PublishBanners({ lanes }: { lanes: LaneView[] }) {
  const api = useBoard();
  const banners = groups(lanes);
  if (banners.length === 0) return null;
  return (
    <div className="space-y-1 px-4 pt-2">
      {banners.map(({ key, truckIds, lead }) => {
        const result = lead.publish.last_result;
        const recovering = lead.state === "recovering";
        const forward = recovering || result?.recovery === "forward";
        const dropped = truckIds.flatMap(
          (t) => api.lanesById[t]?.publish.last_result?.dropped_orders ?? [],
        );
        let text: string;
        if (forward) {
          const n =
            result?.drivers_without_routes.length ||
            new Set(
              truckIds.map((t) => api.lanesById[t]?.driver_id).filter(Boolean),
            ).size;
          text = `${n} ${n === 1 ? "driver can't" : "drivers can't"} see their routes until this publish completes (${trucksText(truckIds)}). Retry to finish it.`;
        } else if (result?.rolled_back) {
          text = `Publish rolled back on ${trucksText(truckIds)}. Your previous route is still with the driver. You can edit and retry.`;
        } else {
          text = failureText(truckIds, result);
        }
        return (
          <div
            key={key}
            role={forward ? "alert" : "status"}
            className={`flex items-start gap-2 rounded-md border px-3 py-2 text-sm ${
              forward
                ? "border-error bg-error-light text-error-dark"
                : "border-warning bg-warning-light text-warning-dark"
            }`}
          >
            <AlertTriangle
              className="mt-0.5 h-4 w-4 shrink-0"
              aria-hidden="true"
            />
            <div className="min-w-0 flex-1">
              <p>{text}</p>
              {dropped.length > 0 && (
                <p className="text-xs">
                  Removed from the route because they can't be delivered:{" "}
                  {dropped.join(", ")}.
                </p>
              )}
            </div>
            {!api.readOnly && (
              <button
                type="button"
                onClick={() => api.retryPublish(lead.truck_id)}
                aria-label={`Retry publish on ${trucksText(truckIds)}`}
                className="inline-flex min-h-8 shrink-0 items-center gap-1 rounded-md border border-current px-2 text-xs font-medium"
              >
                <RotateCcw className="h-3.5 w-3.5" aria-hidden="true" />
                Retry
              </button>
            )}
          </div>
        );
      })}
    </div>
  );
}

export default PublishBanners;
