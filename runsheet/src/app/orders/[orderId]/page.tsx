"use client";

/**
 * Standalone route at ``/orders/:orderId``. Reads the path param and renders
 * the shared {@link OrderDetailView}; the dashboard shell renders the same view
 * in-shell with its own back handler, so order detail lives in one place.
 */

import { useParams, useRouter } from "next/navigation";
import OrderDetailView from "../../../components/orders/OrderDetailView";

export default function OrderDetailPage() {
  const params = useParams();
  const router = useRouter();
  const orderId = (params?.orderId as string) ?? "";
  return <OrderDetailView orderId={orderId} onBack={() => router.back()} />;
}
