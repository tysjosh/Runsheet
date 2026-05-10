"use client";

import OrdersPage from "../../components/ops/OrdersPage";

/**
 * Operations overview page.
 *
 * Renders the fuel orders board as the primary ops view. This replaces
 * the legacy Dinee shipment board that was removed during the US fuel
 * distribution pivot.
 */
export default function OpsPage() {
  return <OrdersPage />;
}
