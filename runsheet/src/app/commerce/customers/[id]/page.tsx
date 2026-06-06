"use client";

import { use } from "react";
import CustomerDetailPage from "../../../../components/commerce/CustomerDetailPage";

/**
 * Commerce Customer Detail page — renders the CustomerDetailPage component.
 */
export default function CustomerPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  return <CustomerDetailPage customerId={id} />;
}
