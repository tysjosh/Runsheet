"use client";

/**
 * Standalone route at ``/ops/fuel/depots/:id``. Reads the path param and renders
 * the shared {@link DepotDetailPage}. This is the canonical owning-module
 * destination that {@link EntityLink} links a ``depot`` reference to.
 */

import { useRouter } from "next/navigation";
import { use } from "react";
import DepotDetailPage from "../../../../../components/ops/DepotDetailPage";

export default function DepotPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const router = useRouter();
  return <DepotDetailPage depotId={id} onBack={() => router.back()} />;
}
