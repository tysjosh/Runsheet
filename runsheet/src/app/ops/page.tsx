"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import CreateOrderModal from "../../components/ops/CreateOrderModal";
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
 *
 * The "Create Order" affordance mounts the validated intake modal
 * (``CreateOrderModal``). Previously the board never received an
 * ``onCreateOrder`` handler, so the button was hidden and order intake had
 * no UI path at all — a dispatcher could not create an order. On success we
 * route to the new order's detail page.
 */
export default function OpsPage() {
  const router = useRouter();
  const [showCreateOrder, setShowCreateOrder] = useState(false);

  return (
    <>
      <OrdersPage
        onOrderClick={(orderId) =>
          router.push(`/orders/${encodeURIComponent(orderId)}`)
        }
        onCreateOrder={() => setShowCreateOrder(true)}
      />
      <CreateOrderModal
        isOpen={showCreateOrder}
        onClose={() => setShowCreateOrder(false)}
        onSuccess={(orderId) => {
          setShowCreateOrder(false);
          router.push(`/orders/${encodeURIComponent(orderId)}`);
        }}
      />
    </>
  );
}
