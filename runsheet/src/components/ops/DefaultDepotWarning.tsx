"use client";

/**
 * Default-depot misconfiguration warning for the Operations Control view.
 *
 * The Route_Planning_Agent resolves a route's start position from
 * `truck.assigned_depot_id → tenant.default_depot_id → an is_default active
 * depot`. When none of those resolves, the agent skips the loading plan
 * (no_depot_configured) rather than routing from a meaningless origin — which
 * is correct, but fails *silently*. This banner surfaces that misconfiguration
 * where routing is supervised so a dispatcher notices before plans quietly
 * stop generating.
 *
 * Heuristic (UI-observable): warn when the tenant has no active depot, or has
 * active depots but none flagged default. This mirrors the dispatcher-facing
 * "set as default" control (which writes `is_default`). A tenant-level
 * `default_depot_id` set out-of-band (migration) is not visible here, so the
 * copy is advisory and links to where the default is set.
 */

import { AlertTriangle } from "lucide-react";
import { useEffect, useState } from "react";
import { listDepots } from "../../services/fuelApi";

type State = "checking" | "ok" | "no_depots" | "no_default" | "error";

export default function DefaultDepotWarning() {
  const [state, setState] = useState<State>("checking");

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const resp = await listDepots({ status: "active", size: 100 });
        if (cancelled) return;
        const active = resp.items ?? [];
        if (active.length === 0) setState("no_depots");
        else if (!active.some((d) => d.is_default)) setState("no_default");
        else setState("ok");
      } catch {
        if (!cancelled) setState("error");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Stay quiet when configured correctly, still checking, or unable to tell.
  if (state === "ok" || state === "checking" || state === "error") return null;

  const message =
    state === "no_depots"
      ? "No active depot is configured. Route planning will skip until a depot is added and set as the default."
      : "No default depot is set. Route planning will skip for trucks without telemetry until you mark a depot as default.";

  return (
    <div
      className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border border-warning-light bg-warning-light px-4 py-2.5"
      role="alert"
    >
      <span className="inline-flex items-center gap-2 font-semibold text-warning-dark">
        <AlertTriangle className="h-4 w-4 flex-shrink-0" />
        Routing not fully configured
      </span>
      <span className="text-sm text-warning-dark opacity-90">{message}</span>
      <a
        href="/dashboard/setup"
        className="ml-auto text-xs font-medium text-warning-dark underline hover:opacity-80"
      >
        Configure in Setup → Depots
      </a>
    </div>
  );
}
