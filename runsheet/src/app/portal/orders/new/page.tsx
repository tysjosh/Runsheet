"use client";

/**
 * `/portal/orders/new[?tank=<id>]` (D24): the Orders list with the request
 * dialog open over it, the tank preselected. Bookmarks and the keyboard e2e
 * keep working; closing the dialog lands on `/portal/orders`.
 */
import { useSearchParams } from "next/navigation";
import { Suspense } from "react";
import PortalOrdersView from "../../../../components/portal/PortalOrdersView";

function NewOrder() {
  const params = useSearchParams();
  const tankId = params?.get("tank") ?? null;
  return <PortalOrdersView initialDialog={{ open: true, tankId }} />;
}

export default function PortalNewOrderPage() {
  return (
    <Suspense fallback={null}>
      <NewOrder />
    </Suspense>
  );
}
