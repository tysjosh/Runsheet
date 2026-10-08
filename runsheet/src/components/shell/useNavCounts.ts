"use client";

/**
 * Sidebar count badges (R2.6).
 *
 * - Orders: placed + on hold (the intake queue a dispatcher clears).
 * - Live: delayed jobs + pending approvals. There is no list endpoint for
 *   driver exceptions yet, so "open exceptions" are the delayed jobs the
 *   scheduling API reports, the same source the dashboard feed ranks.
 *
 * Totals come from the API's `total`, not a capped page. Refreshed by the
 * existing orders, scheduling and agent WebSocket hooks (debounced) and a
 * slow poll as a fallback. Failures leave the previous count in place.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { useAgentWebSocket } from "../../hooks/useAgentWebSocket";
import { useOrdersWebSocket } from "../../hooks/useOrdersWebSocket";
import { useSchedulingWebSocket } from "../../hooks/useSchedulingWebSocket";
import { getApprovals } from "../../services/agentApi";
import { listOrders } from "../../services/ordersApi";
import { getDelayedJobs } from "../../services/schedulingApi";
import { getCurrentTenantId } from "../../services/tenant";
import type { NavCounts } from "./Sidebar";

const POLL_MS = 120_000;
const DEBOUNCE_MS = 1_500;

function totalOf(r: PromiseSettledResult<unknown>): number | null {
  if (r.status !== "fulfilled") return null;
  const v = r.value as { total?: number; data?: unknown[] } | unknown[] | null;
  if (Array.isArray(v)) return v.length;
  if (v && typeof v === "object") {
    if (typeof v.total === "number") return v.total;
    if (Array.isArray(v.data)) return v.data.length;
  }
  return null;
}

export function useNavCounts(enabled: boolean): NavCounts {
  const [counts, setCounts] = useState<NavCounts>({});
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = useCallback(async () => {
    const [placed, held, delayed, approvals] = await Promise.allSettled([
      listOrders({ status: "placed", size: 1 }),
      listOrders({ status: "on_hold", size: 1 }),
      getDelayedJobs(),
      getApprovals(getCurrentTenantId(), 1, 1),
    ]);
    setCounts((prev) => {
      const p = totalOf(placed);
      const h = totalOf(held);
      const d = totalOf(delayed);
      const a = totalOf(approvals);
      return {
        orders: p === null && h === null ? prev.orders : (p ?? 0) + (h ?? 0),
        live: d === null && a === null ? prev.live : (d ?? 0) + (a ?? 0),
      };
    });
  }, []);

  const schedule = useCallback(() => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => {
      timer.current = null;
      void load();
    }, DEBOUNCE_MS);
  }, [load]);

  useEffect(() => {
    if (!enabled) return;
    void load();
    const id = setInterval(() => void load(), POLL_MS);
    return () => {
      clearInterval(id);
      if (timer.current) clearTimeout(timer.current);
    };
  }, [enabled, load]);

  useOrdersWebSocket(getCurrentTenantId(), {
    subscriptions: ["order_placed", "order_status_changed"],
    onOrderPlaced: schedule,
    onOrderStatusChanged: schedule,
  });
  useSchedulingWebSocket({
    subscriptions: ["status_changed", "delay_alert"],
    onStatusChanged: schedule,
    onDelayAlert: schedule,
  });
  useAgentWebSocket({ onApprovalEvent: schedule });

  return counts;
}
