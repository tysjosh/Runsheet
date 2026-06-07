"use client";

/**
 * Standalone route at ``/ops/fuel/tanks/:id``. Reads the path param and renders
 * the shared {@link CustomerTankDetailPage}. This is the canonical owning-module
 * destination that {@link EntityLink} links a ``tank`` reference to.
 */

import { useRouter } from "next/navigation";
import { use } from "react";
import CustomerTankDetailPage from "../../../../../components/ops/CustomerTankDetailPage";

export default function TankPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const router = useRouter();
  return (
    <CustomerTankDetailPage customerTankId={id} onBack={() => router.back()} />
  );
}
