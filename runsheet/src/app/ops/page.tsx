"use client";

import { useRouter } from "next/navigation";
import OrdersPage from "../../components/ops/OrdersPage";

/**
 * Operations overview page.
 *
 * Renders the fuel orders board as the primary ops view. This replaces
 * the legacy Dinee shipment board that was removed during the US fuel
 * distribution pivot.
 *
 * Clicking an order row routes to the order detail page at
 * ``/orders/{orderId}``; without this wiring the detail route was
 * unreachable from the UI.
 */
export default function OpsPage() {
  const router = useRouter();
  return (
    <OrdersPage
      onOrderClick={(orderId) =>
        router.push(`/orders/${encodeURIComponent(orderId)}`)
      }
    />
  );
}
